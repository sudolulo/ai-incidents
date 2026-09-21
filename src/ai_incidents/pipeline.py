"""One run: scan, gate, judge, write, commit, record.

The order is what makes it safe to run unattended:

1. **Scan** every configured source. Read-only. Clean sessions are recorded as judged straight
   away (they carry nothing a judge could file).
2. **Gate.** No candidates means no model call at all. A quiet night costs nothing.
3. **Judge** the candidates in one call. The answer must parse and validate, or the run fails.
4. **Write** the ledger, the run report, ``latest.md`` and ``index.json``, and nothing else.
5. **Commit** exactly those files, and push if configured.
6. **Record** the judged sessions as seen. Only now: if any earlier step failed, the next run sees
   the same candidates again.
"""

from __future__ import annotations

import fnmatch
import json
import os
import sqlite3
from dataclasses import dataclass, field

from . import gitops, judge, ledger, report
from .config import Config, ConfigError
from .envelope import WriteGuard
from .hooks import run_hook
from .prefilter import Candidate, Patterns, extract
from .redact import Redactor
from .sources import SourceError, iter_source
from .state import RunLock, State

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_LOCKED = 0, 1, 2, 3


@dataclass
class SourceStats:
    total: int = 0
    new: int = 0
    clean: int = 0
    with_candidates: int = 0
    deferred: int = 0
    excluded: int = 0
    baseline: int = 0
    unreadable: int = 0
    missing: bool = False
    failed: bool = False


@dataclass
class ScanResult:
    candidates: list[Candidate] = field(default_factory=list)
    pending: dict[str, set[str]] = field(default_factory=dict)  # judged only after a successful write
    done: dict[str, set[str]] = field(default_factory=dict)  # clean, excluded, or baseline: seen now
    stats: dict[str, SourceStats] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> list[str]:
        lines = []
        for name, s in self.stats.items():
            if s.missing:
                lines.append(f"**{name}:** no transcripts found at the configured path")
                continue
            if s.baseline:
                lines.append(f"**{name}:** first run, {s.baseline} existing session(s) marked as judged without judging")
                continue
            bits = [f"{s.new} new session(s)", f"{s.clean} clean", f"{s.with_candidates} with candidates"]
            if s.deferred:
                bits.append(f"{s.deferred} deferred to the next run (candidate budget spent)")
            if s.excluded:
                bits.append(f"{s.excluded} excluded by config")
            if s.unreadable:
                bits.append(f"{s.unreadable} unreadable")
            lines.append(f"**{name}:** {', '.join(bits)}")
        lines.append(f"**Candidates:** {len(self.candidates)}")
        lines += [f"**Error:** {e}" for e in self.errors]
        return lines


def patterns_for(cfg: Config) -> Patterns:
    return Patterns.with_extras(cfg.scan.extra_destructive, cfg.scan.extra_benign, cfg.scan.extra_alarm)


def scan(cfg: Config, state: State, backfill: bool = False) -> ScanResult:
    """Read every source and collect candidates. Never writes anything; the caller decides."""
    res = ScanResult()
    pats = patterns_for(cfg)
    budget = cfg.scan.max_candidates
    for src in cfg.sources:
        st = res.stats[src.name] = SourceStats()
        seen = state.seen(src.name)
        pending = res.pending.setdefault(src.name, set())
        done = res.done.setdefault(src.name, set())
        baseline = not state.initialized(src.name) and not backfill and cfg.scan.first_run == "baseline"
        try:
            for ref in iter_source(src.type, src.name, src.path):
                st.total += 1
                if ref.key in seen:
                    continue
                if baseline:
                    st.baseline += 1
                    done.add(ref.key)
                    continue
                st.new += 1
                if any(fnmatch.fnmatch(ref.path, pat) for pat in cfg.scan.exclude):
                    st.excluded += 1
                    done.add(ref.key)
                    continue
                try:
                    sess = ref.load()
                except (OSError, sqlite3.Error, ValueError) as e:
                    # Left unrecorded, so it is tried again next run.
                    st.unreadable += 1
                    res.errors.append(f"{ref.path}: {e}")
                    continue
                ex = extract(sess, src.name, ref.short_id, pats, cfg.scan.max_per_session)
                if ex.clean:
                    st.clean += 1
                    done.add(ref.key)
                    continue
                if len(ex.candidates) > budget:
                    # Not shown, not recorded: the next run starts with it. Keep scanning, so clean
                    # sessions further on are still recorded.
                    st.deferred += 1
                    continue
                budget -= len(ex.candidates)
                st.with_candidates += 1
                res.candidates += ex.candidates
                pending.add(ref.key)
        except SourceError as e:
            res.errors.append(f"{src.name}: {e}")
            st.failed = True
        st.missing = st.total == 0 and not st.failed
    for n, c in enumerate(res.candidates, 1):
        c.id = f"C{n:02d}"
    return res


def _rubric(cfg: Config) -> str:
    if cfg.judge.prompt_file:
        with open(cfg.judge.prompt_file, encoding="utf-8") as f:
            return f.read()
    return judge.default_rubric()


def _judge_label(cfg: Config) -> str:
    j = cfg.judge
    if j.backend == "claude":
        return f"claude CLI, model {j.model or 'default'}"
    if j.backend == "openai":
        return f"{j.model} at {j.base_url}"
    return f"command `{os.path.basename(j.command[0])}`" if j.command else "command"


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _report_path(ledger_dir: str, date: str) -> str:
    base = os.path.join(ledger_dir, "reports", date)
    path, n = base + ".md", 2
    while os.path.exists(path):
        path, n = f"{base}-{n}.md", n + 1
    return path


@dataclass
class Outcome:
    code: int = EXIT_OK
    headline: str = ""
    filed: list = field(default_factory=list)
    excluded: int = 0
    report_path: str = ""

    def summary(self) -> str:
        out = [self.headline]
        out += [f"{i.severity:<6} {i.title}" for i in self.filed]
        if self.report_path:
            out.append(f"report: {self.report_path}")
        return "\n".join(out)

    def env(self, status: str) -> dict[str, str]:
        sev = lambda s: sum(1 for i in self.filed if i.severity == s)  # noqa: E731
        return {
            "AI_INCIDENTS_STATUS": status,
            "AI_INCIDENTS_HEADLINE": self.headline,
            "AI_INCIDENTS_FILED": str(len(self.filed)),
            "AI_INCIDENTS_HIGH": str(sev("HIGH")),
            "AI_INCIDENTS_MEDIUM": str(sev("MEDIUM")),
            "AI_INCIDENTS_LOW": str(sev("LOW")),
            "AI_INCIDENTS_EXCLUDED": str(self.excluded),
            "AI_INCIDENTS_REPORT": self.report_path,
        }


def headline(date: str, filed: list, excluded: int) -> str:
    if not filed:
        return f"ai-incidents {date}: no new incidents" + (f", {excluded} candidate(s) excluded" if excluded else "")
    high = sum(1 for i in filed if i.severity == "HIGH")
    return f"ai-incidents {date}: {len(filed)} filed" + (f" ({high} high)" if high else "") + f", {excluded} candidate(s) excluded"


def run(
    cfg: Config,
    *,
    dry_run: bool = False,
    with_judge: bool = False,
    backfill: bool = False,
    notify_cmd: str | None = None,
    use_git: bool | None = None,
    push: bool | None = None,
    verbose: bool = False,
    say=print,
) -> Outcome:
    if not cfg.ledger.dir:
        raise ConfigError("[ledger] dir is not set")
    ledger_dir = os.path.realpath(cfg.ledger.dir)
    guard = WriteGuard(
        dirs=[ledger_dir],
        files=[cfg.state.file, cfg.state.file + ".lock", cfg.state.seen_list],
    )
    notify = cfg.hooks.notify_cmd if notify_cmd is None else notify_cmd
    git_on = cfg.git.enabled if use_git is None else use_git
    git_on = git_on and gitops.is_repo(ledger_dir)
    do_push = git_on and (cfg.git.push if push is None else push)
    date = ledger.today()
    redact = Redactor(cfg.privacy.extra_redact_patterns, enabled=cfg.privacy.redact)

    def fail(msg: str, outcome: Outcome | None = None) -> Outcome:
        o = outcome or Outcome()
        o.code = EXIT_FAILED
        # An error can quote the judge's answer, which can quote a transcript: mask it like the rest.
        o.headline = f"ai-incidents {date}: FAILED: {redact(msg)}"
        say(o.headline)
        if not dry_run:
            run_hook(cfg.hooks.on_failure_cmd, o.summary(), o.env("failed"), cfg.hooks.timeout)
        return o

    lock = None
    if not dry_run:
        lock = RunLock(guard, cfg.state.file)
        lock.__enter__()  # LockedError propagates to the CLI
    try:
        state = State(cfg.state.file, cfg.state.seen_list)
        warnings = []
        if do_push and not dry_run:
            try:
                w = gitops.pull(ledger_dir, cfg.git)
            except gitops.GitError as e:
                return fail(str(e))
            if w:
                warnings.append(w)

        res = scan(cfg, state, backfill=backfill)
        for line in res.summary():
            say(line.replace("**", ""))
        for w in warnings:
            say(f"warning: {w}")

        if not dry_run:
            for name, keys in res.done.items():
                state.mark_seen(name, keys)
            for src in cfg.sources:
                # A source that could not be read has not had its baseline taken; the next run must
                # still treat it as a first run rather than judging its whole history.
                if not res.stats[src.name].failed:
                    state.mark_initialized(src.name)

        if not res.candidates:
            o = Outcome(headline=f"ai-incidents {date}: no new candidate incidents; judge not invoked")
            say(o.headline)
            if not dry_run:
                state.record_run({"result": "quiet", "candidates": 0})
                state.save(guard)
                if do_push:
                    try:
                        gitops.push(ledger_dir, cfg.git)  # deliver anything an earlier run could not
                    except gitops.GitError as e:
                        return fail(str(e), o)
                run_hook(cfg.hooks.on_success_cmd, o.summary(), o.env("ok"), cfg.hooks.timeout)
            return o

        for c in res.candidates:
            c.body = redact(c.body)

        led_path = os.path.join(ledger_dir, "incidents.md")
        text = _read(led_path)
        led = ledger.parse(text) if text is not None else ledger.Ledger(ledger.DEFAULT_HEADER.format(title=cfg.ledger.title))
        message = judge.build_message(res.candidates, ledger.ordered(led.entries), date)

        if not dry_run:
            state.save(guard)  # clean and baseline sessions are durable even if the judge fails

        if dry_run and not with_judge:
            say(f"dry run: would send {len(res.candidates)} candidate(s), {len(message):,} characters, "
                f"to the judge ({_judge_label(cfg)}). Nothing was sent and nothing was written.")
            if verbose:
                say("\n" + message)
            return Outcome(headline=f"ai-incidents {date}: dry run, {len(res.candidates)} candidate(s)")

        try:
            answer, usage = judge.call(cfg.judge, _rubric(cfg), message)
            verdict = judge.parse_verdict(answer, res.candidates, date, redact)
        except (judge.JudgeError, OSError) as e:
            return fail(f"judge: {e}")
        verdict.usage = usage

        entries = [(i, i.entry()) for i in verdict.incidents]
        added, _ = ledger.add_entries(led, [e for _, e in entries])
        added_ids = {id(e) for e in added}
        filed = [i for i, e in entries if id(e) in added_ids]
        already = [i for i, e in entries if id(e) not in added_ids]
        added_patterns = ledger.add_patterns(led, verdict.patterns)
        verdict.patterns = added_patterns
        excluded = sum(len(e.candidates) or 1 for e in verdict.excluded)

        o = Outcome(filed=filed, excluded=excluded)
        o.headline = headline(date, filed, excluded)
        rep_path = _report_path(ledger_dir, date)
        rep = report.render(date, res.summary(), res.candidates, verdict, filed, already, _judge_label(cfg))
        idx_path = os.path.join(ledger_dir, "index.json")
        prior = None
        if (raw := _read(idx_path)) is not None:
            try:
                prior = json.loads(raw)
            except ValueError:
                prior = None
        new_ledger = ledger.render(led, cfg.ledger.order)
        new_index = ledger.dump_index(ledger.index(led, prior, date))

        if dry_run:
            say(o.headline + " (dry run: nothing written)")
            say("\n" + rep)
            return o

        latest = os.path.join(ledger_dir, "latest.md")
        guard.makedirs(os.path.join(ledger_dir, "reports"))
        guard.write_text(led_path, new_ledger)
        guard.write_text(rep_path, rep)
        guard.write_text(latest, rep)
        guard.write_text(idx_path, new_index)
        o.report_path = rep_path

        if not git_on and cfg.git.enabled and use_git is None:
            say(f"note: {ledger_dir} is not a git repository; the ledger was written but not committed")
        if git_on:
            try:
                sha = gitops.commit(ledger_dir, [led_path, rep_path, latest, idx_path], o.headline, cfg.git)
                if sha:
                    say(f"committed {sha}")
            except gitops.GitError as e:
                return fail(str(e), o)

        # The verdict is on disk (and committed): these sessions are now safely judged.
        for name, keys in res.pending.items():
            state.mark_seen(name, keys)
        state.record_run({"result": "judged", "candidates": len(res.candidates),
                          "filed": len(filed), "excluded": excluded})
        state.save(guard)

        if do_push:
            try:
                gitops.push(ledger_dir, cfg.git)
                say(f"pushed to {cfg.git.remote}")
            except gitops.GitError as e:
                # The ledger is committed locally and the sessions are recorded; the next run pushes.
                return fail(f"push: {e}", o)

        say(o.summary())
        if filed:
            run_hook(notify, o.summary(), o.env("ok"), cfg.hooks.timeout)
        run_hook(cfg.hooks.on_success_cmd, o.summary(), o.env("ok"), cfg.hooks.timeout)
        return o
    except (OSError, ValueError) as e:
        # Includes an EnvelopeError (a write outside the envelope) and a corrupt state file.
        return fail(f"{type(e).__name__}: {e}")
    finally:
        if lock:
            lock.__exit__(None, None, None)


def reindex(cfg: Config, say=print) -> int:
    """Re-rank ``incidents.md`` and regenerate ``index.json`` after hand edits. Commits nothing."""
    if not cfg.ledger.dir:
        raise ConfigError("[ledger] dir is not set")
    ledger_dir = os.path.realpath(cfg.ledger.dir)
    guard = WriteGuard(dirs=[ledger_dir])
    led_path = os.path.join(ledger_dir, "incidents.md")
    text = _read(led_path)
    if text is None:
        say(f"no ledger at {led_path}")
        return EXIT_FAILED
    led = ledger.parse(text)
    idx_path = os.path.join(ledger_dir, "index.json")
    prior = None
    if (raw := _read(idx_path)) is not None:
        try:
            prior = json.loads(raw)
        except ValueError:
            pass
    guard.write_text(led_path, ledger.render(led, cfg.ledger.order))
    guard.write_text(idx_path, ledger.dump_index(ledger.index(led, prior, ledger.today())))
    say(f"reindexed {len(led.entries)} incident(s)")
    return EXIT_OK
