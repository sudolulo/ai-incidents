"""Builders for synthetic transcripts in both supported formats."""

from __future__ import annotations

import json
import os
import sqlite3
import sys

FAKE_JUDGE = os.path.join(os.path.dirname(__file__), "fake_judge.py")


def cc(role: str, text: str | None = None, *, ts: str = "2026-07-14T10:00:00Z",
       tool_use: str | None = None, tool_name: str = "Bash",
       tool_result: str | None = None, is_error: bool = False) -> str:
    """One Claude Code JSONL line."""
    content: list | str
    blocks = []
    if text is not None:
        blocks.append({"type": "text", "text": text})
    if tool_use is not None:
        blocks.append({"type": "tool_use", "id": "t1", "name": tool_name, "input": {"command": tool_use}})
    if tool_result is not None:
        blocks.append({"type": "tool_result", "tool_use_id": "t1", "is_error": is_error,
                       "content": [{"type": "text", "text": tool_result}]})
    content = blocks
    return json.dumps({"type": role, "timestamp": ts, "message": {"role": role, "content": content}})


def write_session(root: str, project: str, sid: str, lines: list[str]) -> str:
    d = os.path.join(root, project)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{sid}.jsonl")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


OC_SCHEMA = """
CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, parent_id TEXT, slug TEXT NOT NULL,
  directory TEXT NOT NULL, title TEXT NOT NULL, version TEXT NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, time_created INTEGER NOT NULL,
  time_updated INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
"""


def oc_db(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(OC_SCHEMA)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def oc_session(con: sqlite3.Connection, sid: str, turns: list[tuple[str, list[dict]]], t0: int = 1784023200000) -> None:
    """turns: [(role, [part dicts]), ...]"""
    t = t0
    con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?)",
                (sid, "p1", None, "slug", "/work", "title", "1.0.0", t, t + 1000 * len(turns)))
    for n, (role, parts) in enumerate(turns):
        mid = f"msg_{sid}_{n:03d}"
        t += 1000
        con.execute("INSERT INTO message VALUES (?,?,?,?,?)", (mid, sid, t, t, json.dumps({"role": role})))
        for k, p in enumerate(parts):
            con.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                        (f"prt_{sid}_{n:03d}_{k}", mid, sid, t + k, t + k, json.dumps(p)))
    con.commit()


def judge_cmd() -> list[str]:
    return [sys.executable, FAKE_JUDGE]


def config_toml(tmp, *, claude_root="", oc_glob="", ledger="", state="", seen_list="", extra="") -> str:
    parts = []
    if claude_root:
        parts.append(f'[[source]]\ntype = "claude-code"\npath = "{claude_root}"\n')
    if oc_glob:
        parts.append(f'[[source]]\ntype = "opencode"\npath = "{oc_glob}"\n')
    cmd = ", ".join(json.dumps(x) for x in judge_cmd())
    parts.append(f'[judge]\nbackend = "command"\ncommand = [{cmd}]\ntimeout = 60\n')
    parts.append(f'[ledger]\ndir = "{ledger}"\n')
    parts.append(f'[state]\nfile = "{state}"\n' + (f'seen_list = "{seen_list}"\n' if seen_list else ""))
    parts.append(extra)
    path = os.path.join(tmp, "config.toml")
    with open(path, "w") as f:
        f.write("\n".join(parts))
    return path
