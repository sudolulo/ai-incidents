"""A stand-in judge for tests. Reads the prompt on stdin and answers according to FAKE_JUDGE_MODE.

file-first  file the first candidate as a HIGH incident, exclude the rest (default)
exclude-all exclude every candidate
fail        exit non-zero
prose       exit 0 with a provider message instead of a verdict
leak        file an incident whose text contains a secret
"""

import json
import os
import re
import sys

prompt = sys.stdin.read()
marker = os.environ.get("FAKE_JUDGE_MARKER")
if marker:
    with open(marker, "a") as f:
        f.write(prompt + "\n=====\n")

mode = os.environ.get("FAKE_JUDGE_MODE", "file-first")
ids = re.findall(r"^\[(C\d+)\]", prompt, flags=re.M)
title = os.environ.get("FAKE_JUDGE_TITLE", "Deleted the production table on a hunch")

if mode == "fail":
    print("upstream exploded", file=sys.stderr)
    sys.exit(3)
if mode == "prose":
    print("You've hit your monthly spend limit. Raise it to keep going.")
    sys.exit(0)

incidents, excluded = [], []
if mode in ("file-first", "leak") and ids:
    what = "ran DROP TABLE on the live database"
    if mode == "leak":
        what += " and printed password=" + "hunter2hunter2"
    incidents.append({
        "title": title, "date": "2026-07-14", "severity": "HIGH", "category": "data-loss",
        "what": what, "cost": "a day of orders was lost",
        "lesson": "never run destructive SQL without a backup", "candidates": [ids[0]],
        "why": "real, unrecoverable data loss",
    })
    rest = ids[1:]
else:
    rest = ids
if rest:
    excluded.append({"candidates": rest, "reason": "routine cleanup of its own scratch files"})
print("Here is my verdict:\n```json\n" + json.dumps({"incidents": incidents, "excluded": excluded, "patterns": []}) + "\n```")
