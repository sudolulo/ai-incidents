"""Deterministic pre-filter: find the moments in a session worth showing to the judge.

Nothing here calls a model. The pre-filter is tuned for recall, and it is cheap, so it can look at
every session; the judge is tuned for precision, and it is expensive, so it only sees what survives
this step.

A session produces candidates in three shapes:

* SNIPPET: somebody *said* something. The user corrected or blamed the agent, or the agent admitted
  a mistake, raised an alarm, or blamed somebody else's code. The surrounding turns come with it.
* EVIDENCE: what a session that produced snippets actually *did*: its destructive commands and its
  notable failures, attached to those snippets.
* DIGEST: nobody said anything, but the session ran something destructive or something notably
  broke. The task, the commands and the failures are shown instead.

Recall cannot depend on the culprit choosing the right words. The worst incidents are often the
ones nobody narrates, so tool calls and tool output are read as well as conversation text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .sources import Session

# High-precision signals: agent self-admissions ...
AI_ADMIT = re.compile(
    r"\b(i made a mistake|my (fault|mistake|error)|i broke|i accidentally|"
    r"i shouldn'?t have|i should not have|credential exposure|real credential|"
    r"leaked|data loss|i deleted|i overwrote|i wiped|i blinded|i clobbered|"
    r"i corrupted|i destroyed|i lost|i caused|i introduced|i polluted|i swept in|"
    r"premature (exclusion|delete|deletion)|unplanned reboot|that was wrong of me|"
    r"that was my (error|mistake|fault)|(that|the) (outage|breakage|damage) was mine|"
    r"i wrongly|hard to undo|blast radius|was mine\b|apolog(y|ise|ize)|"
    r"i had to (kill|revert|undo|roll back)|undo(ing)? (my|the damage)|"
    r"i (just )?(reverted|rolled back)|no damage|left (it|things) (broken|dirty)|"
    r"went blind|stopped being (monitored|watched)|silently (failed|stopped|broke))\b",
    re.I,
)

# ... and direct user blame or correction.
USER_SIG = re.compile(
    r"\b(you (broke|deleted|overwrote|wiped|lost|removed|nuked|corrupted|clobbered)|"
    r"why did you|that'?s (wrong|not what|broken)|what merge|something you did|"
    r"revert (that|it|the)|undo (that|it|the)|roll ?back|stop,? (you|that)|"
    r"you shouldn'?t|you weren'?t supposed|don'?t do that|that broke|"
    r"put (it|that) back|restore (it|that))\b",
    re.I,
)

# "User" turns injected by the agent harness. They are not a person complaining.
SYNTHETIC = re.compile(
    r"^\s*(<task-notification|<command-|<local-command-|<bash-(input|stdout|stderr)>|"
    r"<user-prompt-submit-hook|\[SYSTEM NOTIFICATION|"
    r"<system-reminder|This session is being continued|Caveat: The messages)",
    re.I,
)

# What damage and remediation look like as commands, whatever anybody said about them.
DESTRUCTIVE = re.compile(
    r"(git\s+(reset\s+--hard|revert|checkout\s+--|clean\s+-[a-z]*f|push\s+(-f|--force))"
    r"|rm\s+-[a-z]*[rf]|shred\b|truncate\b|>\s*/dev/(sd|nvme)"
    r"|DROP\s+(TABLE|DATABASE)|DELETE\s+FROM|TRUNCATE\s+TABLE"
    r"|pkill|kill\s+-9|kill\s+-KILL|killall"
    r"|systemctl[^\n]*\b(stop|disable|mask)\b"
    r"|docker[^\n]*\b(rm|kill|down|prune)\b|zfs\s+destroy|--restore\b"
    r"|chown\s+-R|chmod\s+-R|mv\s+[^\n]*\.(bak|old|orig)\b)",
    re.I,
)

# Monitoring and billing alarms. If a session trips them, that is the incident, confessed or not.
ALARM = re.compile(
    r"(scrape_error\s+1|node_textfile_scrape_error"
    r"|spend limit|silently (failed|stopped|broke)|stopped being (monitored|watched)"
    r"|went blind|data ?loss|corrupt(ed|ion)?)\b",
    re.I,
)

# Misattribution: the agent deciding somebody else's code is at fault. It hides itself (no
# confession, no complaint, no destructive command), so it needs its own signal: the language of
# blame. High recall on purpose; most hits are correct diagnoses and the judge says so in one line.
BLAME = re.compile(
    r"((this|that|it)('?s| is) an? (known )?(bug|issue|limitation|quirk|restriction) (in|with)"
    r"|(is|are) (being |just )?(strict|picky|fussy|weird|broken|buggy) (about|here|with)"
    r"|(bug|issue|regression|limitation) (in|with) (the )?(upstream|library|package|module|tool|api|sdk|"
    r"framework|kernel|driver|compiler|runtime)"
    r"|upstream (bug|issue|problem|regression)|known (bug|issue) (in|with)"
    r"|must be a bug|looks like a bug in|appears to be a bug in|seems to be a bug in"
    r"|work ?around (for|the) |workaround for a|monkey.?patch"
    r"|pin(ning)? (it |the )?(to|back to) (an? )?(older|previous|earlier)"
    r"|(broken|regressed) (in|since) (version|v?\d)"
    r"|file (an? )?(bug|issue) (upstream|against)|report(ing)? (it|this) upstream"
    r"|not our (bug|fault|problem)|nothing (we|i) can do about)",
    re.I,
)

# Commands that do their damage while SUCCEEDING and say so in output nobody reads. A short,
# high-signal list; this is not a place to grep for the word "warning".
WARN_NOTABLE = re.compile(
    r"(adding embedded git repository"
    r"|would be overwritten by (merge|checkout)"
    r"|untracked working tree files would be overwritten"
    r"|non-fast-forward|have diverged|detached HEAD"
    r"|forced update|\(forced update\))",
    re.I,
)

# A failed tool call is not automatically interesting: most are a typo'd path or a grep that
# matched nothing. Only failures that mean something broke.
ERR_NOTABLE = re.compile(
    r"(Traceback \(most recent|Segmentation fault|Out of memory|\bOOM\b|\bKilled\b"
    r"|spend limit|scrape_error|would be overwritten|non-fast-forward|diverged"
    r"|corrupt|permission denied.*(/etc|/var|/mnt)|disk (full|quota))",
    re.I,
)

# A traceback from the agent's own inline one-liner (`python -c`, a heredoc on stdin) is routine
# iteration, not something breaking. Only a traceback from real code counts on its own.
ADHOC_TRACEBACK = re.compile(r'File "<(string|stdin)>"')

# Destructive-looking but routine: a session clearing its own scratch space, a worktree being torn
# down, a throwaway container removed. Filtered here, for free, rather than paying a model to say
# "this is fine".
BENIGN = re.compile(
    r"(scratchpad|/tmp/claude-|/tmp/wt-"
    r"|rm\s+-[a-z]*[rf][a-z]*\s+[\"']?(/tmp/|/var/tmp/|\$?\{?TMP|\$?\{?WT)"
    r"|git\s+worktree\s+(remove|prune|add)"
    r"|docker\s+(rm|kill)\s+[^\n]*\b(test|tmp|scratch|throwaway)\b"
    r"|rm\s+-[a-z]*f?\s+[\"']?/tmp/)",
    re.I,
)

# Excerpt sizes. Enough context to judge, small enough that fifty candidates fit in one prompt.
TURN_CHARS = 280
CMD_CHARS = 160
ERR_CHARS = 160
WARN_CHARS = 150
TASK_CHARS = 200
MAX_COMMANDS = 8
MAX_FAILURES = 5


def danger_rank(cmd: str) -> int:
    """Lower is worse. The evidence block is capped, so the cap must keep the WORST commands, not
    the first ones: rewriting history or deleting real paths outranks stopping a service, which
    outranks anything confined to /tmp."""
    c = cmd.lower()
    if re.search(r"git\s+(rm|reset\s+--hard|checkout\s+--|push\s+(-f|--force)|commit\s+--amend|clean)", c):
        return 0
    if re.search(r"(zfs\s+destroy|drop\s+(table|database)|delete\s+from|truncate|shred|>\s*/dev/)", c):
        return 0
    if re.search(r"rm\s+-[a-z]*[rf]", c) and not re.search(r"/tmp|/var/tmp|scratch", c):
        return 1
    if re.search(r"(systemctl[^\n]*\b(stop|disable|mask)|docker[^\n]*\b(rm|kill|down|prune))", c):
        return 2
    if re.search(r"(pkill|kill\s+-9|kill\s+-kill|killall)", c):
        return 3
    return 4


@dataclass
class Patterns:
    """The regexes in force, plus any the user configured."""

    destructive: list[re.Pattern] = field(default_factory=lambda: [DESTRUCTIVE])
    benign: list[re.Pattern] = field(default_factory=lambda: [BENIGN])
    alarm: list[re.Pattern] = field(default_factory=lambda: [ALARM])

    @classmethod
    def with_extras(cls, destructive=(), benign=(), alarm=()) -> "Patterns":
        p = cls()
        p.destructive += [re.compile(x, re.I) for x in destructive]
        p.benign += [re.compile(x, re.I) for x in benign]
        p.alarm += [re.compile(x, re.I) for x in alarm]
        return p


def _any(pats, s) -> bool:
    return any(p.search(s) for p in pats)


def flat(s: str, limit: int) -> str:
    """One line, at most ``limit`` characters: an excerpt must not break the block around it."""
    return re.sub(r"\s+", " ", s).strip()[:limit]


def notable_failure(text: str) -> bool:
    if not ERR_NOTABLE.search(text):
        return False
    if ADHOC_TRACEBACK.search(text):
        return bool(ERR_NOTABLE.search(text.replace("Traceback (most recent", "")))
    return True


@dataclass
class Candidate:
    kind: str  # SNIPPET | EVIDENCE | DIGEST
    source: str
    session: str
    date: str
    body: str  # indented lines, ready to show the judge
    id: str = ""  # assigned per run: C01, C02, ...

    def render(self) -> str:
        note = {
            "EVIDENCE": " -- what this session actually did; read with its snippets",
            "DIGEST": " -- nobody said anything; the commands did",
        }.get(self.kind, "")
        head = f"[{self.id}] {self.kind} · {self.source} · {self.session} · {self.date or 'undated'}{note}"
        return head + "\n" + self.body


@dataclass
class Extraction:
    candidates: list[Candidate]
    clean: bool  # nothing at all to judge
    dropped: int = 0  # snippets cut by the per-session limit


def is_hit(role: str, text: str, pats: Patterns) -> bool:
    if role == "user":
        return not SYNTHETIC.match(text) and bool(USER_SIG.search(text))
    if role == "assistant":
        return bool(AI_ADMIT.search(text) or BLAME.search(text) or _any(pats.alarm, text))
    return False


def behaviour(sess: Session, pats: Patterns) -> tuple[list[str], list[str]]:
    """Destructive commands run, and notable failures or warnings seen, for one session."""
    danger, failures = [], []
    for call in sess.calls:
        if call.command and _any(pats.destructive, call.command) and not _any(pats.benign, call.command):
            danger.append(f"{call.name}: {flat(call.command, CMD_CHARS)}")
    for res in sess.results:
        if res.is_error:
            if notable_failure(res.text):
                failures.append(flat(res.text, ERR_CHARS))
        else:
            # A command that SUCCEEDED can still have announced the damage in its output.
            hits = [ln.strip() for ln in res.text.splitlines() if WARN_NOTABLE.search(ln)]
            if hits:
                failures.append(flat(hits[0], WARN_CHARS) + (f"   (x{len(hits)})" if len(hits) > 1 else ""))
    return danger, failures


def _behaviour_lines(danger: list[str], failures: list[str]) -> list[str]:
    out = []
    if danger:
        out.append("    [destructive commands run]")
        for d in sorted(dict.fromkeys(danger), key=danger_rank)[:MAX_COMMANDS]:
            out.append(f"      ! {d}")
    if failures:
        out.append("    [notable failures]")
        for e in list(dict.fromkeys(failures))[:MAX_FAILURES]:
            out.append(f"      x {e}")
    return out


def extract(
    sess: Session,
    source: str,
    short_id: str,
    pats: Patterns | None = None,
    per_session: int = 12,
) -> Extraction:
    """Candidates for one session, at most ``per_session`` of them.

    A session with no hit, no destructive command and no notable failure is ``clean``: it has been
    examined, deterministically, and found empty. That costs nothing and needs no judge.
    """
    if per_session < 2:
        raise ValueError("per_session must be at least 2")
    pats = pats or Patterns()
    turns = sess.turns
    danger, failures = behaviour(sess, pats)
    hit_idx = [i for i, t in enumerate(turns) if is_hit(t.role, t.text, pats)]

    if not hit_idx and not danger and not failures:
        return Extraction([], clean=True)

    out: list[Candidate] = []
    for i in hit_idx:
        ctx = "\n".join(
            f"    [{turns[j].role}] {flat(turns[j].text, TURN_CHARS)}"
            for j in range(max(0, i - 1), min(len(turns), i + 2))
        )
        out.append(Candidate("SNIPPET", source, short_id, turns[i].date, ctx))

    date = turns[0].date if turns else ""
    if danger or failures:
        # A session that talked is still a session that did. Its words must never decide whether
        # its actions are examined, so behaviour rides along with snippets as well as standing alone.
        if hit_idx:
            out.append(Candidate("EVIDENCE", source, short_id, date, "\n".join(_behaviour_lines(danger, failures))))
        else:
            task = next((t.text for t in turns if t.role == "user" and not SYNTHETIC.match(t.text)), "")
            ended = next((t.text for t in reversed(turns) if t.role == "assistant"), "")
            lines = [f"    [task] {flat(task, TASK_CHARS)}"] + _behaviour_lines(danger, failures)
            lines.append(f"    [ended] {flat(ended, TASK_CHARS)}")
            out.append(Candidate("DIGEST", source, short_id, date, "\n".join(lines)))

    dropped = 0
    if len(out) > per_session:
        # A very talkative session is cut here, once, rather than split across runs. Splitting it
        # would re-show the same first snippets on every run and never finish the session. The
        # behaviour block is always kept: it is what a chatty transcript would otherwise crowd out.
        keep_tail = [out[-1]] if out[-1].kind == "EVIDENCE" else []
        head = out[: per_session - len(keep_tail)]
        dropped = len(out) - len(head) - len(keep_tail)
        out = head + keep_tail
        out[-1].body += f"\n    ({dropped} more snippet(s) from this session not shown)"
    return Extraction(out, clean=False, dropped=dropped)
