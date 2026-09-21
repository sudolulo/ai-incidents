# AI incidents

A curated excerpt from a real ledger kept by `ai-incidents`: five of its entries, with host names,
project names, people and infrastructure details generalized. The format is exactly what the tool
writes. Most severe first; newest first within a severity.

## Cleanup deleted the user's own agent sessions, unrecoverably, twice in one day · 2026-09-01 · HIGH
- **What:** cleaning up its own smoke-test sessions, the agent filtered a global session list by name and by time window and piped the result into a delete; the filter also matched the user's real sessions, including the one open in their terminal, and after reporting the damage it ran the same filter again later that day.
- **Cost:** at least five sessions destroyed with nothing recoverable; the user's active session was cut off mid-use and unrelated work was lost.
- **Lesson:** never choose what to delete by name or time window over a shared list; delete only what you can prove you created (a recorded ID list, a tagged prefix), and after a filter has deleted someone else's data once, redesign it before running it again.

## An hourly agent's cost, once measured: ~47.6 billion tokens a year · 2026-07-14 · HIGH
- **What:** an ingestion pipeline ran an unattended agent every hour; measured over a week, each run used about 5 million tokens, some ran past 200 turns, and 11 of 27 runs found no work at all.
- **Cost:** roughly 47.6 billion tokens a year, drawn from the same subscription quota as interactive work, so the bill was paid invisibly in rate-limit headroom.
- **Lesson:** gate an unattended agent behind a deterministic check so a model is loaded only when there is work, price the model to the work, and treat cadence as a cost multiplier.

## Deleted source data after mistaking its own bug for corruption · 2026-07-11 · HIGH
- **What:** decided a price-history file was corrupt ("905 missing trading days") and deleted it; the corruption was the agent's own validation bug, because a parquet round-trip had renamed the date column and broken its comparison.
- **Cost:** the file could not be fetched again (the free source returned nothing and the paid one was rate-limited), so the data loss stood.
- **Lesson:** never delete source data to exclude it; quarantine it or filter at read time, and check a corruption diagnosis against a known-good sample before acting on it.

## Committed 14 embedded git repositories to `main` with `git add -A` · 2026-07-14 · MEDIUM
- **What:** ran `git add -A && git commit && git push` in a worktree where a scheduled review job had left 14 throwaway clones; git staged each one as an embedded repository and the commit went to `main`, while printing `warning: adding embedded git repository` 14 times from a command that exited 0.
- **Cost:** `main` carried 14 broken submodule references that broke cloning until a follow-up commit removed them; it also exposed that the review job had been cloning into its own checkout on every run.
- **Lesson:** `git add -A` is not a review step: stage explicit paths anywhere a tool may have written, and read the warnings from commands that succeed.

## Monitoring went blind: an inline comment corrupted a metric value · 2026-07-14 · MEDIUM
- **What:** wrote `MaxAge=9d   # comment` into a config file; a homegrown metrics generator read the comment as part of the value, and the metrics exporter rejected the whole file. The ready explanation, that the exporter was being strict about comments, was wrong.
- **Cost:** every job-health metric vanished from monitoring until it was caught in the same session, so any job failure in that window would have gone unnoticed.
- **Lesson:** keep comments off value lines in key=value files, test the parser against trailing content, and when a tool rejects your output, read what your own code wrote before blaming the tool.

## Recurring patterns
- **Matching by name or time window instead of an ID you own, before deleting or killing.** A loose match over a shared namespace (sessions, processes, files) eventually catches something that is not yours.
