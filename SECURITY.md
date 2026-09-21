# Security policy

## Reporting a vulnerability

Please report security problems privately by email to **holden@ssalomon.com**, not in a public
issue. Include what you found, how to reproduce it, and which version you tested. You will get an
acknowledgement within a few days.

## What counts

This tool reads private transcripts and writes to one directory, so the issues that matter most are:

- anything that makes it write outside the ledger directory and its own state file;
- anything that lets transcript content or a judge's answer reach the filesystem, a command line or
  a shell other than as ledger text;
- any way it could modify a transcript or an opencode database;
- secrets surviving the redaction step in a common, recognisable format;
- the judge receiving tools, files or network access it should not have.

## What does not

- Content you configure the tool to send to a hosted model. That is the documented design; use a
  local model if you do not want transcripts to leave the machine (see the README's privacy section).
- Hooks: they are commands you choose to run.
- A secret in a format the redactor does not recognise. Redaction is best-effort; review a ledger
  before you publish it. Reports of common formats it misses are welcome as ordinary issues.
