import os
import sqlite3

import pytest

from ai_incidents.sources import (
    SourceError, claude_key, iter_claude_code, iter_opencode, open_readonly, parse_claude_jsonl,
)
from helpers import cc, oc_db, oc_session, write_session


def test_claude_jsonl_parse(tmp_path):
    p = write_session(str(tmp_path), "-home-me-proj", "0123456789abcdef", [
        cc("user", "please clean up"),
        "not json at all",
        "",
        '{"type": "summary"}',
        cc("assistant", "running it", tool_use="rm -rf ./build"),
        cc("user", tool_result="removed 3 files"),
        cc("user", tool_result="boom", is_error=True),
    ])
    s = parse_claude_jsonl(p)
    assert [(t.role, t.text) for t in s.turns] == [("user", "please clean up"), ("assistant", "running it")]
    assert s.turns[0].date == "2026-07-14"
    assert [(c.name, c.command) for c in s.calls] == [("Bash", "rm -rf ./build")]
    assert [(r.is_error, r.text) for r in s.results] == [(False, "removed 3 files"), (True, "boom")]


def test_claude_key_shape_and_change_on_growth(tmp_path):
    p = write_session(str(tmp_path), "proj", "sess", [cc("user", "hi")])
    k1 = claude_key(p)
    name, mtime, size = k1.split(":")
    assert name == "sess.jsonl" and int(size) == os.path.getsize(p) and mtime.isdigit()
    with open(p, "a") as f:
        f.write(cc("assistant", "more") + "\n")
    assert claude_key(p) != k1


def test_claude_iter_includes_subagents_and_missing_root(tmp_path):
    write_session(str(tmp_path), "proj", "aaaaaaaa1111", [cc("user", "x")])
    write_session(str(tmp_path), "proj/aaaaaaaa1111/subagents", "agent-1", [cc("user", "y")])
    refs = list(iter_claude_code("claude-code", str(tmp_path)))
    assert sorted(r.short_id for r in refs) == ["aaaaaaaa", "agent-1"]
    assert list(iter_claude_code("claude-code", str(tmp_path / "nope"))) == []


def test_opencode_parse(tmp_path):
    db = str(tmp_path / "opencode.db")
    con = oc_db(db)
    oc_session(con, "ses_f44d56513ffe8czutv5E", [
        ("user", [{"type": "text", "text": "tidy the repo"},
                  {"type": "text", "text": "<file contents>", "synthetic": True}]),
        ("assistant", [
            {"type": "reasoning", "text": "thinking"},
            {"type": "text", "text": "I deleted the wrong folder"},
            {"type": "tool", "tool": "bash", "state": {"status": "completed", "input": {"command": "rm -rf data"},
                                                      "output": "ok"}},
            {"type": "tool", "tool": "read", "state": {"status": "error", "input": {"filePath": "/x"},
                                                      "error": "Traceback (most recent call last)"}},
        ]),
    ])
    con.close()
    [ref] = list(iter_opencode("opencode", str(tmp_path / "opencode*.db")))
    assert ref.short_id == "f44d5651"
    s = ref.load()
    assert [(t.role, t.text) for t in s.turns] == [("user", "tidy the repo"), ("assistant", "I deleted the wrong folder")]
    assert s.turns[0].date == "2026-07-14"
    assert [(c.name, c.command) for c in s.calls] == [("bash", "rm -rf data"), ("read", "")]
    assert [(r.is_error, r.text) for r in s.results] == [(False, "ok"), (True, "Traceback (most recent call last)")]


def test_opencode_key_changes_when_session_updates(tmp_path):
    db = str(tmp_path / "opencode.db")
    con = oc_db(db)
    oc_session(con, "ses_1", [("user", [{"type": "text", "text": "hi"}])])
    k1 = next(iter_opencode("opencode", db)).key
    con.execute("UPDATE session SET time_updated = time_updated + 5")
    con.commit()
    assert next(iter_opencode("opencode", db)).key != k1


def test_opencode_is_opened_read_only(tmp_path):
    db = str(tmp_path / "opencode.db")
    oc_db(db).close()
    con = open_readonly(db)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("INSERT INTO session VALUES ('x','p',NULL,'s','/','t','v',1,1)")


def test_opencode_rejects_a_foreign_database(tmp_path):
    db = str(tmp_path / "opencode.db")
    sqlite3.connect(db).execute("CREATE TABLE unrelated (x)").connection.close()
    with pytest.raises(SourceError):
        list(iter_opencode("opencode", db))
