"""End-to-end runs against synthetic transcripts and a stand-in judge.

The state tests port the verification scenarios of the sweep this tool was extracted from:
first run seeds a baseline; a failed judge loses nothing (the same candidates come back);
success records the sessions; a recorded session is not shown again; a clean session is recorded
without a judge; the candidate budget defers whole sessions without stranding clean ones after them.
"""

import hashlib
import json
import os
import subprocess

import pytest

from ai_incidents import cli, config, pipeline
from ai_incidents.envelope import EnvelopeError, WriteGuard
from ai_incidents.state import LockedError, RunLock, State
from helpers import cc, config_toml, oc_db, oc_session, write_session


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_JUDGE_MODE", "file-first")
    marker = tmp_path / "judge-calls.txt"
    monkeypatch.setenv("FAKE_JUDGE_MARKER", str(marker))
    root = tmp_path / "claude"
    root.mkdir()
    led = tmp_path / "ledger"
    st = tmp_path / "state" / "state.json"
    cfgp = config_toml(str(tmp_path), claude_root=str(root), ledger=str(led), state=str(st),
                       extra="[scan]\nfirst_run = \"backfill\"\n")

    class E:
        pass

    e = E()
    e.tmp, e.root, e.ledger, e.state, e.cfg_path, e.marker = tmp_path, root, led, st, cfgp, marker
    e.cfg = lambda **kw: config.load(cfgp)
    e.calls = lambda: marker.read_text().count("=====") if marker.exists() else 0
    return e


def bad_session(root, sid="deadbeef0001", date="2026-07-14"):
    return write_session(str(root), "-work-proj", sid, [
        cc("user", "clean up the database", ts=f"{date}T09:00:00Z"),
        cc("assistant", "Running it.", tool_use="psql -c 'DROP TABLE orders'", ts=f"{date}T09:01:00Z"),
        cc("assistant", "I made a mistake: that was the live table.", ts=f"{date}T09:02:00Z"),
        cc("user", "why did you do that", ts=f"{date}T09:03:00Z"),
    ])


def clean_session(root, sid="c1ea000000001"):
    return write_session(str(root), "-work-proj", sid, [cc("user", "add a test"), cc("assistant", "Added; passing.")])


def quiet(*a, **k):
    pass


def run(e, **kw):
    return pipeline.run(e.cfg(), say=quiet, **kw)


def snapshot(root):
    out = {}
    for d, _, files in os.walk(root):
        for f in files:
            p = os.path.join(d, f)
            st = os.stat(p)
            out[p] = (hashlib.sha1(open(p, "rb").read()).hexdigest(), st.st_mtime_ns)
    return out


# --- the happy path -------------------------------------------------------------------------

def test_run_files_an_incident_and_writes_only_the_ledger(env):
    bad_session(env.root)
    clean_session(env.root)
    o = run(env)
    assert o.code == 0 and [i.title for i in o.filed] == ["Deleted the production table on a hunch"]
    text = (env.ledger / "incidents.md").read_text()
    assert "## Deleted the production table on a hunch · 2026-07-14 · HIGH" in text
    assert "- **Lesson:** never run destructive SQL without a backup" in text
    reports = os.listdir(env.ledger / "reports")
    assert len(reports) == 1
    rep = (env.ledger / "reports" / reports[0]).read_text()
    assert rep == (env.ledger / "latest.md").read_text()
    assert "## Excluded candidates" in rep and "routine cleanup of its own scratch files" in rep
    idx = json.loads((env.ledger / "index.json").read_text())
    assert idx["counts"]["high"] == 1
    assert env.calls() == 1


def test_second_run_is_quiet_and_does_not_call_the_judge(env):
    bad_session(env.root)
    run(env)
    o = run(env)
    assert o.code == 0 and "judge not invoked" in o.headline
    assert env.calls() == 1


def test_candidates_are_redacted_before_the_judge_sees_them(env):
    write_session(str(env.root), "p", "leaky0000001", [
        cc("assistant", "Oops, I leaked it: password=correcthorsebattery"),
    ])
    run(env)
    prompt = env.marker.read_text()
    assert "correcthorsebattery" not in prompt and "[REDACTED]" in prompt


def test_verdict_is_redacted_before_the_ledger(env, monkeypatch):
    monkeypatch.setenv("FAKE_JUDGE_MODE", "leak")
    bad_session(env.root)
    run(env)
    assert "hunter2hunter2" not in (env.ledger / "incidents.md").read_text()


# --- state: nothing is lost, nothing is judged twice -----------------------------------------

def test_first_run_baseline_emits_nothing(env, tmp_path):
    bad_session(env.root)
    cfgp = config_toml(str(tmp_path), claude_root=str(env.root), ledger=str(env.ledger), state=str(env.state))
    o = pipeline.run(config.load(cfgp), say=quiet)
    assert "judge not invoked" in o.headline and env.calls() == 0
    assert not (env.ledger / "incidents.md").exists()
    # A session that appears after the baseline is judged.
    bad_session(env.root, sid="newsession01", date="2026-07-15")
    o = pipeline.run(config.load(cfgp), say=quiet)
    assert env.calls() == 1 and len(o.filed) == 1


def test_failed_judge_loses_nothing(env, monkeypatch):
    bad_session(env.root)
    clean_session(env.root)
    for mode in ("fail", "prose"):
        monkeypatch.setenv("FAKE_JUDGE_MODE", mode)
        o = run(env)
        assert o.code == 1
    assert not (env.ledger / "incidents.md").exists()
    s = State(str(env.state))
    # the clean session was recorded; the bad one was not
    assert len(s.seen("claude-code")) == 1
    monkeypatch.setenv("FAKE_JUDGE_MODE", "file-first")
    o = run(env)
    assert o.code == 0 and len(o.filed) == 1
    assert len(State(str(env.state)).seen("claude-code")) == 2


def test_a_grown_session_is_looked_at_again(env):
    p = bad_session(env.root)
    run(env)
    with open(p, "a") as f:
        f.write(cc("user", "you broke the export too", ts="2026-07-16T10:00:00Z") + "\n")
    o = run(env)
    assert env.calls() == 2
    # the same incident, re-surfaced, is not filed twice
    assert o.filed == []
    assert (env.ledger / "incidents.md").read_text().count("## Deleted the production table") == 1


def test_budget_defers_whole_sessions_and_still_records_clean_ones(env, tmp_path):
    for i in range(3):
        bad_session(env.root, sid=f"bad{i:09d}")
    clean_session(env.root, sid="zzzclean0001")  # sorts after the bad ones
    cfgp = config_toml(str(tmp_path), claude_root=str(env.root), ledger=str(env.ledger), state=str(env.state),
                       extra="[scan]\nfirst_run = \"backfill\"\nmax_candidates = 4\nmax_per_session = 4\n")
    cfg = config.load(cfgp)
    s = State(str(env.state))
    res = pipeline.scan(cfg, s)
    assert res.stats["claude-code"].with_candidates == 1
    assert res.stats["claude-code"].deferred == 2
    assert res.stats["claude-code"].clean == 1
    pipeline.run(cfg, say=quiet)
    pipeline.run(cfg, say=quiet)
    pipeline.run(cfg, say=quiet)
    assert env.calls() == 3
    assert len(State(str(env.state)).seen("claude-code")) == 4


def test_seen_list_export_and_import(env, tmp_path):
    bad_session(env.root)
    clean_session(env.root)
    seen_list = tmp_path / "external" / "seen.txt"
    seen_list.parent.mkdir()
    cfgp = config_toml(str(tmp_path), claude_root=str(env.root), ledger=str(env.ledger), state=str(env.state),
                       seen_list=str(seen_list), extra="[scan]\nfirst_run = \"backfill\"\n")
    pipeline.run(config.load(cfgp), say=quiet)
    keys = seen_list.read_text().split()
    assert sorted(k.split(":")[0] for k in keys) == ["c1ea000000001.jsonl", "deadbeef0001.jsonl"]
    # A fresh state file with an existing list does not re-judge or re-baseline.
    os.remove(env.state)
    o = pipeline.run(config.load(cfgp), say=quiet)
    assert "judge not invoked" in o.headline and env.calls() == 1


# --- dry run, lock, hooks -----------------------------------------------------------------

def test_dry_run_calls_no_model_and_writes_nothing(env):
    bad_session(env.root)
    before = snapshot(env.tmp)
    o = run(env, dry_run=True)
    assert o.code == 0 and "dry run" in o.headline
    assert env.calls() == 0
    assert snapshot(env.tmp) == before


def test_dry_run_with_judge_still_writes_nothing(env):
    bad_session(env.root)
    run(env, dry_run=True, with_judge=True)
    assert env.calls() == 1
    assert not env.ledger.exists() and not env.state.exists()


def test_concurrent_run_is_refused(env):
    bad_session(env.root)
    guard = WriteGuard(files=[str(env.state) + ".lock"])
    with RunLock(guard, str(env.state)):
        with pytest.raises(LockedError):
            run(env)


def test_hooks(env, tmp_path):
    out = tmp_path / "hooks.log"
    script = tmp_path / "hook.sh"
    script.write_text(f'#!/bin/sh\nprintf "%s %s %s\\n" "$1" "$AI_INCIDENTS_FILED" "$AI_INCIDENTS_HIGH" >> {out}\ncat >> {out}\n')
    script.chmod(0o755)
    text = open(env.cfg_path).read() + (
        f'\n[hooks]\nnotify_cmd = "{script} notify"\non_success_cmd = "{script} ok"\n'
        f'on_failure_cmd = "{script} failed"\n')
    open(env.cfg_path, "w").write(text)
    bad_session(env.root)
    run(env)
    log = out.read_text()
    assert "notify 1 1" in log and "ok 1 1" in log
    assert "HIGH   Deleted the production table on a hunch" in log
    out.unlink()
    run(env)  # quiet run: heartbeat only
    assert out.read_text().startswith("ok 0 0")


def test_failure_hook(env, tmp_path, monkeypatch):
    out = tmp_path / "fail.log"
    open(env.cfg_path, "a").write(f'\n[hooks]\non_failure_cmd = "sh -c \'cat > {out}\'"\n')
    monkeypatch.setenv("FAKE_JUDGE_MODE", "fail")
    bad_session(env.root)
    assert run(env).code == 1
    assert "FAILED: judge" in out.read_text()


# --- the permission envelope ---------------------------------------------------------------

def test_write_guard_refuses_outside_paths(tmp_path):
    g = WriteGuard(dirs=[str(tmp_path / "ledger")], files=[str(tmp_path / "state.json")])
    g.write_text(str(tmp_path / "ledger" / "reports" / "x.md"), "ok")
    g.write_text(str(tmp_path / "state.json"), "{}")
    for bad in [tmp_path / "elsewhere.md", tmp_path / "ledger" / ".." / "escape.md", tmp_path / "ledger-evil" / "x"]:
        with pytest.raises(EnvelopeError):
            g.write_text(str(bad), "no")
    os.symlink(tmp_path, tmp_path / "ledger" / "link")
    with pytest.raises(EnvelopeError):
        g.write_text(str(tmp_path / "ledger" / "link" / "escape.md"), "no")


def test_a_full_run_changes_nothing_outside_the_envelope(env, tmp_path):
    bad_session(env.root)
    clean_session(env.root)
    db = tmp_path / "oc" / "opencode.db"
    db.parent.mkdir()
    con = oc_db(str(db))
    oc_session(con, "ses_abc", [("user", [{"type": "text", "text": "you deleted my notes"}])])
    con.close()
    open(env.cfg_path, "a").write(f'\n[[source]]\ntype = "opencode"\npath = "{db}"\n')
    open(env.cfg_path, "a").write(f'\n[[source]]\ntype = "claude-code"\nname = "cc"\npath = "{env.root}"\n')
    before = snapshot(tmp_path)
    assert run(env).code == 0
    after = snapshot(tmp_path)
    changed = {p for p in set(before) | set(after) if before.get(p) != after.get(p)}
    # SQLite's WAL index (-shm) is reader bookkeeping, created as the database's own user; the
    # database itself and every transcript are untouched.
    allowed = (str(env.ledger) + os.sep, str(env.state), str(env.marker), str(db) + "-shm", str(db) + "-wal")
    assert changed and all(p.startswith(allowed) for p in changed), sorted(changed)
    assert before[str(db)] == after[str(db)]


def test_someone_elses_opencode_database_gets_no_sidecar_files(tmp_path, monkeypatch):
    from ai_incidents import sources
    db = tmp_path / "opencode.db"
    con = oc_db(str(db))
    oc_session(con, "ses_abc", [("user", [{"type": "text", "text": "you deleted my notes"}])])
    con.close()
    before = sorted(os.listdir(tmp_path))
    monkeypatch.setattr(sources.os, "geteuid", lambda: os.stat(db).st_uid + 1)
    [ref] = list(sources.iter_opencode("opencode", str(db)))
    assert ref.load().turns[0].text == "you deleted my notes"
    assert sorted(os.listdir(tmp_path)) == before


# --- git ------------------------------------------------------------------------------------

def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def test_git_commits_only_what_it_wrote(env):
    env.ledger.mkdir()
    git(env.ledger, "init", "-q", "-b", "main")
    git(env.ledger, "config", "user.name", "Test")
    git(env.ledger, "config", "user.email", "test@example.com")
    (env.ledger / "notes.txt").write_text("my own uncommitted notes")
    (env.ledger / "staged.txt").write_text("someone else's staged work")
    git(env.ledger, "add", "staged.txt")
    bad_session(env.root)
    o = run(env)
    assert o.code == 0
    files = git(env.ledger, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(files) == sorted(["incidents.md", "index.json", "latest.md", f"reports/{os.listdir(env.ledger / 'reports')[0]}"])
    assert git(env.ledger, "log", "-1", "--format=%s").strip() == o.headline
    status = git(env.ledger, "status", "--porcelain")
    assert "A  staged.txt" in status and "?? notes.txt" in status


def test_git_push_goes_to_the_configured_remote_and_verifies_it(env, tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    env.ledger.mkdir()
    git(env.ledger, "init", "-q", "-b", "main")
    git(env.ledger, "config", "user.name", "Test")
    git(env.ledger, "config", "user.email", "test@example.com")
    git(env.ledger, "commit", "-q", "--allow-empty", "-m", "init")
    git(env.ledger, "remote", "add", "origin", str(remote))
    git(env.ledger, "push", "-q", "origin", "main")
    open(env.cfg_path, "a").write(f'\n[git]\npush = true\nexpected_remote_url = "{remote}"\n')
    bad_session(env.root)
    assert run(env).code == 0
    assert "ai-incidents" in git(remote, "log", "-1", "--format=%s")
    # A repointed checkout is refused before anything is pushed or written.
    git(env.ledger, "remote", "set-url", "origin", str(tmp_path / "somewhere-else.git"))
    bad_session(env.root, sid="another00001", date="2026-07-20")
    o = run(env)
    assert o.code == 1 and "refusing" in o.headline


# --- CLI ------------------------------------------------------------------------------------

def test_cli_init_scan_config_and_exit_codes(tmp_path, monkeypatch, capsys):
    cfgp = str(tmp_path / "cfg" / "config.toml")
    led = str(tmp_path / "led")
    assert cli.main(["-c", cfgp, "init", "--ledger", led, "--git"]) == 0
    assert os.path.isdir(os.path.join(led, ".git"))
    assert cli.main(["-c", cfgp, "init", "--ledger", led]) == 2  # refuses to overwrite
    cfg = config.load(cfgp)
    assert cfg.ledger.dir == led and cfg.judge.backend == "claude" and cfg.judge.model == "sonnet"
    assert cli.main(["-c", cfgp, "config"]) == 0
    assert "writes:" in capsys.readouterr().out
    assert cli.main(["-c", str(tmp_path / "missing.toml"), "run"]) == 2


def test_config_rejects_typos_and_bad_values(tmp_path):
    with pytest.raises(config.ConfigError, match="unknown key"):
        config.from_dict({"judge": {"modle": "x"}})
    with pytest.raises(config.ConfigError, match="must be int"):
        config.from_dict({"scan": {"max_candidates": "50"}})
    with pytest.raises(config.ConfigError, match="first_run"):
        config.from_dict({"scan": {"first_run": "sometimes"}})
    with pytest.raises(config.ConfigError, match="used twice"):
        config.from_dict({"source": [{"type": "claude-code"}, {"type": "claude-code"}]})
    with pytest.raises(config.ConfigError, match="needs"):
        config.from_dict({"judge": {"backend": "command"}})
    cfg = config.from_dict({})
    assert [s.type for s in cfg.sources] == ["claude-code", "opencode"]


def test_reindex_reranks_hand_edits(env):
    env.ledger.mkdir()
    (env.ledger / "incidents.md").write_text(
        "# L\n\n## Low one · 2026-09-01 · LOW\n- **What:** a\n\n## High one · 2026-07-01 · HIGH\n- **What:** b\n")
    assert pipeline.reindex(env.cfg(), say=quiet) == 0
    text = (env.ledger / "incidents.md").read_text()
    assert text.index("High one") < text.index("Low one")
    assert json.loads((env.ledger / "index.json").read_text())["counts"]["total"] == 2


def test_example_config_is_valid():
    path = os.path.join(os.path.dirname(__file__), "..", "examples", "config.toml")
    cfg = config.load(path)
    assert cfg.judge.backend == "claude" and cfg.ledger.order == "severity"
    assert [s.type for s in cfg.sources] == ["claude-code", "opencode"]
