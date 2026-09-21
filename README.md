# ai-incidents

Coding agents occasionally do real damage: they delete data, take a service down, leak a
credential into a transcript, or burn a week of quota, and the lesson is usually forgotten by the
next session. `ai-incidents` runs unattended over your own Claude Code and opencode transcripts,
uses deterministic rules to pick out the moments that look like trouble, has a model judge which
of them were real incidents, and keeps a severity-ranked markdown ledger of what broke, what it
cost and the lesson, with a record of everything it excluded and why.

[`examples/sample-ledger.md`](examples/sample-ledger.md) is a curated excerpt from real use, and
[`examples/sample-report.md`](examples/sample-report.md) shows the matching run report.

## Install

```sh
pipx install git+https://github.com/sudolulo/ai-incidents   # or: uv tool install git+https://...
ai-incidents init --ledger ~/ai-incidents-ledger --git      # writes ~/.config/ai-incidents/config.toml
ai-incidents run --dry-run                                  # see what would be judged; changes nothing
```

From a checkout, `pipx install .` or `uv tool install .` works the same way. Python 3.11 or later,
no other dependencies. The default judge is the Claude Code CLI (`claude`), which must be logged in;
see [Choosing a judge](#choosing-a-judge) for local models.

## Quick start

```sh
ai-incidents run --dry-run      # scan, and show what would be sent to the judge; no model call
ai-incidents run                # first run: record existing sessions as a baseline, judge nothing
ai-incidents run                # later runs: judge only sessions that are new or have grown
```

The first run takes a **baseline**: it records every existing session as already judged, so you
do not pay to judge your whole history on day one. To judge history instead, run
`ai-incidents run --backfill` (or set `first_run = "backfill"`); it works through the backlog
`max_candidates` at a time, one batch per run.

Then schedule it. A systemd user timer and a crontab line are in
[`examples/systemd/`](examples/systemd) and [`examples/cron.txt`](examples/cron.txt). Nightly is
plenty: runs are incremental, and a night with nothing to judge makes no model call at all.

A run prints one line per source and a headline:

```
claude-code: 12 new session(s), 10 clean, 2 with candidates
opencode: 3 new session(s), 3 clean, 0 with candidates
Candidates: 5
committed 240f3cb
ai-incidents 2026-09-21: 1 filed (1 high), 2 candidate(s) excluded
HIGH   Deleted 48,211 live production orders with the wrong connection string
report: /home/you/ai-incidents-ledger/reports/2026-09-21.md
```

## What it writes

Everything goes into the ledger directory (a git repository, if you want history):

| File | What |
|---|---|
| `incidents.md` | The ledger. One entry per incident: title, date, severity, then **What**, **Cost** and **Lesson**. Most severe first. Hand edits and hand-written entries are kept. |
| `reports/YYYY-MM-DD.md` | One per judged run: what the pre-filter found, each confirmed incident with the reason it cleared the bar, each excluded candidate with the reason it did not, and the judge's token use. |
| `latest.md` | A copy of the newest report. |
| `index.json` | A machine index (fingerprint, title, date, severity, first seen). Regenerated from `incidents.md` on every write; do not edit it. |

An entry looks like this:

```markdown
## Committed 14 embedded git repositories to `main` with `git add -A` · 2026-07-14 · MEDIUM
- **What:** ran `git add -A && git commit && git push` in a worktree where a scheduled review job had left 14 throwaway clones ...
- **Cost:** `main` carried 14 broken submodule references that broke cloning until a follow-up commit removed them ...
- **Lesson:** `git add -A` is not a review step: stage explicit paths anywhere a tool may have written, and read the warnings from commands that succeed.
```

The tool's own run state (which sessions have been judged) lives outside the ledger, in
`~/.local/state/ai-incidents/state.json` by default.

## Configuration

One TOML file, `~/.config/ai-incidents/config.toml` by default (or `$AI_INCIDENTS_CONFIG`, or
`-c PATH`). [`examples/config.toml`](examples/config.toml) documents every key. Unknown keys are an
error, so a typo cannot quietly switch something off. `ai-incidents config` prints the effective
settings, including exactly which paths the tool will write.

| Table | Key | Default | Meaning |
|---|---|---|---|
| `[[source]]` | `type` | `claude-code`, `opencode` | Transcript format. Omit every `[[source]]` to read both defaults. |
| | `path` | `~/.claude/projects`, `~/.local/share/opencode/opencode*.db` | Where the transcripts are. The opencode path is a glob. |
| | `name` | the type | Needed only to tell two sources of one type apart. |
| `[scan]` | `max_candidates` | `50` | Most candidates shown to the judge per run; the rest wait for the next run. |
| | `max_per_session` | `12` | Most candidates from one session. |
| | `first_run` | `baseline` | `baseline` or `backfill` (see Quick start). |
| | `exclude` | `[]` | Globs on transcript paths to skip. |
| | `extra_destructive`, `extra_benign`, `extra_alarm` | `[]` | Extra pre-filter regexes (for example your own alert names). |
| `[judge]` | `backend` | `claude` | `claude`, `openai` or `command`. |
| | `model` | `sonnet` | Model name or alias for the backend. |
| | `timeout` | `900` | Seconds before the judge call is abandoned. |
| | `prompt_file` | built in | Replace the judging rubric. |
| | `base_url`, `api_key_env`, `json_mode` | | `openai` backend. `api_key_env` names an environment variable; keys never go in the file. |
| | `command` | | `command` backend: argv of a program that reads the prompt on stdin. |
| `[ledger]` | `dir` | (required) | The ledger directory. |
| | `order` | `severity` | `severity` or `newest`. |
| `[git]` | `enabled` | `true` | Commit if `ledger.dir` is a git repository. |
| | `push` | `false` | Push after committing. |
| | `expected_remote_url` | | Refuse to push if the remote has been repointed. |
| `[state]` | `file` | `~/.local/state/ai-incidents/state.json` | Run state. |
| | `seen_list` | | Optional plain-text list of judged Claude Code transcripts, for cleanup jobs. |
| `[privacy]` | `redact` | `true` | Mask secret-shaped strings before the judge and before the ledger. |
| `[hooks]` | `notify_cmd` | | Runs after a run that filed something. |
| | `on_success_cmd` | | Runs after every successful run, quiet ones included: a heartbeat. |
| | `on_failure_cmd` | | Runs after a failed run. |

### Commands and exit codes

| Command | Does |
|---|---|
| `ai-incidents run` | Scan, judge if there is anything to judge, write, commit, record. `--dry-run`, `--dry-run --with-judge`, `--backfill`, `--notify-cmd CMD`, `--no-git`, `--push` / `--no-push`. |
| `ai-incidents scan [--show]` | Show what the pre-filter finds. Reads only; records nothing. |
| `ai-incidents init --ledger DIR [--git]` | Write a starter config and create the ledger directory. |
| `ai-incidents reindex` | Re-rank `incidents.md` and rebuild `index.json` after hand edits. |
| `ai-incidents config` | Print the effective configuration. |

Exit codes: `0` success (a quiet run is a success), `1` the run failed (judge, git, or state
error), `2` bad usage or configuration, `3` another run holds the lock.

### Hooks

Hooks replace any built-in notifier. Each is a command line, split the way a shell would split it
but not run through one; wrap it in `sh -c '...'` if you want pipes. The run summary arrives on
stdin, and these variables are set: `AI_INCIDENTS_STATUS` (`ok` or `failed`),
`AI_INCIDENTS_HEADLINE`, `AI_INCIDENTS_FILED`, `AI_INCIDENTS_HIGH`, `AI_INCIDENTS_MEDIUM`,
`AI_INCIDENTS_LOW`, `AI_INCIDENTS_EXCLUDED`, `AI_INCIDENTS_REPORT`. A hook that fails is logged and
ignored.

```toml
[hooks]
notify_cmd = "curl -s -H 'Title: ai-incidents' --data-binary @- https://ntfy.sh/your-topic"
on_success_cmd = "curl -fsS -m 10 https://hc-ping.com/your-uuid"
```

### Choosing a judge

```toml
[judge]                               # default: the Claude Code CLI you already use
backend = "claude"
model = "sonnet"

[judge]                               # a local model through any OpenAI-compatible server
backend = "openai"
base_url = "http://localhost:11434/v1"    # Ollama; llama.cpp's server is http://localhost:8080/v1
model = "qwen3:32b"

[judge]                               # anything that reads a prompt on stdin
backend = "command"
command = ["llm", "-m", "some-model"]
```

The judge must answer with a JSON object in the format the rubric describes. Larger models follow
it more reliably; a malformed answer fails the run safely (see [Failure modes](#failure-modes)).

## Permission envelope

The tool reads private transcripts, so what it may do is deliberately narrow, and enforced in
code rather than by convention:

- **Transcripts are read-only.** Claude Code JSONL files are opened for reading. opencode's SQLite
  database is opened with `mode=ro` and `query_only`. (SQLite's WAL mode has every reader
  coordinate through a `-shm` index file next to the database, which a reader may create or update;
  that is lock bookkeeping, not data. For a database owned by another user, the tool opens it
  `immutable=1` instead, so it never leaves a sidecar file that user cannot write.)
- **The judge only reads.** It never sees a transcript directly: it receives the pre-filtered,
  redacted excerpts as text on stdin (never on the command line, where other local users could
  read them) and answers with JSON. With the default backend, `claude -p` runs with
  `--tools ""` (no built-in tools), `--strict-mcp-config` (no MCP servers), and
  `--no-session-persistence` (its own session is not written back into your transcripts), in an
  empty temporary directory. With the `openai` backend it is one HTTP request. The `command`
  backend runs whatever you configure, so the envelope there is yours to keep.
- **The only writes are the ledger.** Every file write goes through one guard that allows exactly
  the ledger directory and the state file (plus its lock, and the optional seen-list). Anything
  else raises before a byte is written. The ledger text is rendered by the tool from validated,
  single-line JSON fields; the model never chooses a path or writes markdown structure.
- **Git is narrow.** Commits name their files explicitly, so nothing else in the ledger repository
  (staged or not) is swept in. Pushing is off by default; when on, it pushes the current branch
  to its own name on the configured remote: no force, no tags, no detached HEAD, and optionally
  only to an `expected_remote_url`. Git is never allowed to prompt.
- **Hooks are yours.** They run the commands you configure and are outside the envelope by
  definition. Nothing from a transcript is ever put on a hook's command line.

The test suite checks these: a full run over both transcript formats must leave every file outside
the ledger and state unchanged, writes outside the guard must fail, the judge's argv must not
contain the candidates, and a commit must not include files the tool did not write.

## Privacy

Transcripts contain everything you and your agents did: code, file contents, command output, and
now and then a secret that should never have been printed. Know where they go:

- **Excerpts leave the machine for the judge.** Only the pre-filtered moments are sent (a few
  hundred characters per turn, the flagged commands and failures, and the title and first line of
  each existing ledger entry), not whole transcripts. They go to whichever model you configure. With the default
  `claude` backend that is Anthropic, the same provider your Claude Code sessions already use;
  opencode sessions may have run against a different provider, so sending them to Claude is a new
  disclosure.
- **A local model keeps everything local.** Point the `openai` backend at llama.cpp, Ollama, vLLM
  or LM Studio on your own hardware and no excerpt leaves the machine.
- **Redaction is best-effort.** Common token formats, private keys, `Authorization` headers, URL
  credentials and `password=`-style assignments are masked before the judge sees them and again
  before anything reaches the ledger. A bare password with nothing around it cannot be recognised.
  The rubric also tells the judge never to copy a secret or a third party's name into its answer.
- **The ledger is private by default.** It describes your mistakes, your infrastructure and your
  projects. Review it before you publish it; `examples/sample-ledger.md` shows what a scrubbed
  excerpt looks like.
- `ai-incidents run --dry-run -v` prints the candidates exactly as they would be sent, without
  sending them.

## Design

### Why it runs unattended

Nobody writes a postmortem for their own agent session. The session that caused the damage is
usually the one that cleaned it up, and by the next day the lesson survives only in a transcript
nobody will reread, until the transcript itself is deleted (Claude Code removes old transcripts
after `cleanupPeriodDays`, 30 by default). A recorder that depends on someone remembering to run it
has the same problem as the postmortem. So it runs on a timer, reads what was actually said and
done, and costs nothing on the nights when nothing happened.

### Why a model judges instead of rules

Rules are good at finding *candidate* moments and bad at deciding whether they mattered. The same
`git reset --hard` is routine cleanup in one session and the loss of a day's unpushed work in
another; `rm -rf` of a scratch directory and of a data directory look alike to a regex. Deciding
needs the surrounding conversation and the consequences, which is a judgement.

So there are two stages, each doing what it is good at. The **pre-filter** is deterministic, free,
and tuned for recall. It reads what sessions *said* (the agent admitting a mistake, the user
objecting, an alarm, the agent blaming someone else's code) and, just as important, what they
*did*: destructive commands, notable failures, and warnings printed by commands that exited 0.
Reading behaviour is not optional. The sweep this tool came from originally matched only
confessions, and on its first real test it missed all three incidents of that day, because none of
the sessions happened to say one of the magic phrases; one of them committed 14 repositories to
`main` from a command that exited 0 and announced the damage in a warning nobody read. The
**judge** is a model, tuned for precision: strict about what counts, told that a digest of
destructive commands is not an accusation, and told that confidently blaming an upstream project
for your own bug is an incident in its own right.

The pre-filter also acts as a gate. When there are no candidates, no model is loaded. An agent
that starts up only to discover there is no work still pays for the whole start-up, and a nightly
job that does that every night is the kind of unmetered cost this ledger exists to catch.

### Why exclusions are recorded

Every run report lists each candidate the judge excluded, with a one-line reason. Without that, the
bar for "incident" is invisible and cannot be argued with: you cannot tell a quiet week from a
judge that has become lax, or a noisy ledger from one that has become credulous. The exclusions are
where you notice the judge drifting, where you find a real incident it waved through, and where you
learn which pre-filter patterns only produce noise. They also make a quiet run a real result: "five
candidates, all routine, here is why" is a finding; silence is not.

### Severity is the cost

Severity is defined by what an incident *cost*, not by how alarming the mistake looked. **HIGH**
is data loss, an outage, or a real credential exposure. **MEDIUM** is wasted rework, a bad
decision pushed to a shared branch, or a debug spiral. **LOW** is a self-inflicted mess that was
cleaned up cheaply. The agent catching its own mistake before it cost anything is not an incident
at all. The ledger is ordered by severity, then date, because its reader wants the most expensive
lessons first; the chronology is in `reports/`.

### Failure modes

| What goes wrong | What happens |
|---|---|
| The judge fails: it times out, exits non-zero, hits a quota, or answers with prose instead of a verdict (`claude -p` exits 0 while printing a spend-limit message). | The answer is validated, not the exit code, so the run fails with exit 1 and records nothing for the candidate sessions. The next run shows them again. Clean sessions are still recorded. |
| The judge returns a malformed incident (no lesson, an unknown severity). | That incident is rejected and listed in the report under "Loose ends"; the rest of the verdict is used. Candidates it never mentions are listed too. |
| The pre-filter misses something. | Recall is bounded by what a session said or did in a way the patterns recognise. A wrong decision that involved no destructive command, no failure and no complaint is invisible. `extra_*` patterns let you widen it; the exclusions show you what it currently catches. |
| The judge invents or overstates detail. | It is told to judge only from the excerpts, and each entry links back to its sessions in the report. The ledger is plain markdown: correct it by hand and run `reindex`. |
| The same incident surfaces twice (a resumed session, two sessions about one mistake). | The judge sees the existing ledger and marks duplicates, and an entry whose title matches an existing one is never filed twice. A duplicate under a different title can still slip through; delete it by hand. |
| More candidates than the budget. | Whole sessions are deferred to the next run, never split; clean sessions later in the scan are still recorded. A backfill takes several runs. |
| Two runs overlap. | The second exits with code 3 without touching anything. |
| The push fails. | The ledger is already committed locally and the sessions recorded; the run exits 1, and the next run pushes. |
| A transcript is deleted before it is judged. | It is simply gone. Schedule runs more often than your transcript retention. If a cleanup job of your own deletes transcripts, point it at `seen_list` so it only removes what has been judged. |
| A secret in an unrecognised format. | It can reach the judge and, if the judge repeats it, the ledger. Keep the ledger private and review before publishing. |

## Compatibility

- **Claude Code** 2.x: transcripts under `~/.claude/projects/` (or `$CLAUDE_CONFIG_DIR`), and the
  `claude` CLI for the default judge, which needs a build with `--tools` and
  `--no-session-persistence`. Tested with 2.1.
- **opencode** 1.x: the SQLite store (`opencode.db`, or `opencode-<channel>.db`) under
  `~/.local/share/opencode/`. Tested on 1.18.22 or later. The older JSON-file storage is not read.
  A port for opencode 2.x is planned.
- **Python** 3.11 to 3.13, on Linux and macOS.

## License

[MIT](LICENSE). See [NOTICE](NOTICE) for attribution.
