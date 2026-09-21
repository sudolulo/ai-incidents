# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2026-09-21

First public release. `ai-incidents` began as a nightly job in a private set of scheduled agent
scripts, where it had been running since July 2026 and had filed 44 incidents. This release
extracts it into a standalone tool with no dependency on that environment.

### Added
- `ai-incidents run`: scan transcripts, gate, judge, write the ledger, commit, record. A run with
  no candidates makes no model call.
- `ai-incidents scan`, `init`, `reindex` and `config` commands.
- `--dry-run` (no model call, no writes), `--dry-run --with-judge` (model call, still no writes),
  and `--backfill` to judge existing history instead of taking a baseline on a first run.
- One TOML configuration file for transcript locations, judge, ledger, git, state, privacy and
  hooks. Unknown keys are rejected.
- opencode 1.x support: sessions are read from its SQLite store, opened read-only.
- Judge backends: the Claude Code CLI (all tools and MCP servers disabled, no session
  persistence), any OpenAI-compatible endpoint for local models, or an arbitrary command.
- Structured verdicts: the judge answers in JSON, which is validated before anything is written;
  the ledger, report and index are rendered by the tool, not edited by the model.
- Secret redaction of candidates before they reach the judge and of verdicts before they reach the
  ledger.
- `WriteGuard`: every file write is checked against the ledger directory and the state file.
- Optional hooks (`notify_cmd`, `on_success_cmd`, `on_failure_cmd`, and `--notify-cmd`) in place
  of built-in notification and heartbeat services.
- Optional plain-text seen-list export, for transcript-cleanup jobs that must not delete
  sessions before they are judged.
- A run lock, so overlapping runs cannot race on the state file.
- Examples for a systemd user timer and cron, a full configuration reference, and a curated sample
  ledger and run report.
- Test suite (pre-filter, both transcript formats, state semantics, judge backends, ledger
  writer, redaction, permission envelope, git) and GitHub Actions CI on Python 3.11 to 3.13.

### Changed (from the private sweep)
- **Renamed and folded.** The collector (`ai-incidents-collect` plus `_scan_incidents.py`), the
  gate (`ai-incidents-gate`), the agent job definition, and the publisher
  (`ai-incidents-publish`) are now one program, `ai-incidents run`. The pre-filter lives in
  `prefilter.py`, transcript reading in `sources.py`, the judge rubric in `prompts/judge.md`, and
  ledger writing in `ledger.py`.
- **The judge no longer edits files.** It used to be an agent with Read, Write and Edit tools and
  a scoped shell; it now receives text and returns JSON, with no tools at all. The tool writes the
  ledger itself.
- **Severity-ranked ledger.** `incidents.md` is ordered most severe first, newest first within a
  severity (it was newest first). `order = "newest"` restores the old order.
- **`state.json` in the ledger is now `index.json`.** It is still derived from `incidents.md` on
  every write and uses the same fingerprints, so existing `first_seen` dates carry over. The name
  `state.json` now belongs to the tool's own run state, kept outside the ledger by default.
- **Two-phase seen-state is kept, in one process.** Sessions with candidates are recorded as judged
  only after the verdict is written and committed; clean sessions immediately. The separate
  `--promote` step is gone.
- **Talkative sessions are cut once instead of split across runs.** A session with more candidates
  than the per-run budget used to be re-emitted on every run and never finished; it is now capped
  at `max_per_session` candidates, with its behaviour block always kept.
- **Tracebacks from the agent's own inline scripts** (`python -c`, stdin heredocs) no longer count
  as notable failures on their own.
- **Only shell commands are checked for destructive patterns**, not file paths passed to read or
  edit tools.
- **Git writes are narrower.** Commits name their paths explicitly (`git commit -- <paths>`), so
  other staged or untracked files in the ledger repository are never swept in. The hard-coded
  remote check became the optional `expected_remote_url`.

### Removed
- Multi-host collection over SSH. Run one instance per machine instead.
- Built-in ntfy notification, the dead-man's-switch heartbeat, quota-retry scheduling and the
  unattended-agent runner: use hooks, your scheduler's own retry, and `on_success_cmd`.
- Hard-coded paths, host names and repository names.

[Unreleased]: https://github.com/sudolulo/ai-incidents/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/sudolulo/ai-incidents/releases/tag/v1.0.0
