"""Read agent session transcripts. Read-only, always.

Every source yields ``SessionRef`` objects cheaply (a stat or one small query), so a run can skip
sessions it has already judged without opening them. ``SessionRef.load()`` then reads one session
into a ``Session`` that the pre-filter understands, whatever tool produced it.

Two formats are supported:

* Claude Code: one JSONL file per session under ``~/.claude/projects/``. Subagent sessions live
  alongside their parent and are read too.
* opencode 1.x: the SQLite store under ``~/.local/share/opencode/``. It is opened with
  ``mode=ro``; nothing here can write to it.
"""

from __future__ import annotations

import datetime as _dt
import glob
import json
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Iterator


@dataclass
class Turn:
    """One human-readable message: what somebody *said*."""

    role: str  # "user" | "assistant"
    date: str  # YYYY-MM-DD (UTC), or "" when unknown
    text: str


@dataclass
class ToolCall:
    name: str
    command: str  # the shell command; empty for tools that take no command


@dataclass
class ToolResult:
    is_error: bool
    text: str


@dataclass
class Session:
    """What a session said (turns) and what it did (tool calls and their results)."""

    turns: list[Turn] = field(default_factory=list)
    calls: list[ToolCall] = field(default_factory=list)
    results: list[ToolResult] = field(default_factory=list)


@dataclass
class SessionRef:
    source: str  # the configured source name, e.g. "claude-code"
    key: str  # changes whenever the session's content changes
    short_id: str  # a short, stable handle for reports
    path: str  # where it lives, for exclusion rules and error messages
    loader: Callable[[], Session]

    def load(self) -> Session:
        return self.loader()


class SourceError(Exception):
    pass


# --- Claude Code ------------------------------------------------------------------------------


def claude_key(path: str) -> str | None:
    """``<file name>:<int mtime>:<size>``.

    The shape is deliberate and stable: an external cleanup job can read the exported seen-list and
    refuse to delete any transcript whose current key is not in it. A transcript that grows after it
    was judged gets a new key, so it is judged again rather than silently skipped.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    return f"{os.path.basename(path)}:{int(st.st_mtime)}:{st.st_size}"


def _cc_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _cc_result_text(content) -> str:
    if isinstance(content, list):
        return " ".join(x.get("text", "") for x in content if isinstance(x, dict))
    return str(content or "")


def parse_claude_jsonl(path: str) -> Session:
    sess = Session()
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            role = msg.get("role") or obj.get("type", "")
            date = str(obj.get("timestamp") or "")[:10]
            content = msg.get("content")
            text = _cc_text(content).strip()
            if text:
                sess.turns.append(Turn(role, date, text))
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    inp = block.get("input") or {}
                    cmd = inp.get("command") if isinstance(inp, dict) else None
                    sess.calls.append(
                        ToolCall(block.get("name", "?"), cmd.strip() if isinstance(cmd, str) else "")
                    )
                elif block.get("type") == "tool_result":
                    sess.results.append(
                        ToolResult(bool(block.get("is_error")), _cc_result_text(block.get("content")))
                    )
    return sess


def iter_claude_code(name: str, root: str) -> Iterator[SessionRef]:
    root = os.path.expanduser(root)
    if not os.path.isdir(root):
        return
    for path in sorted(glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)):
        key = claude_key(path)
        if not key:
            continue
        base = os.path.basename(path)[: -len(".jsonl")]
        yield SessionRef(name, key, base[:8], path, lambda p=path: parse_claude_jsonl(p))


# --- opencode -------------------------------------------------------------------------------


def _ms_date(ms) -> str:
    try:
        return _dt.datetime.fromtimestamp(int(ms) / 1000, _dt.timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def open_readonly(db_path: str) -> sqlite3.Connection:
    """Open an SQLite database so that this process cannot write to it.

    ``mode=ro`` is enforced by SQLite itself, and ``query_only`` on top of it. One caveat is
    inherent to SQLite's WAL mode, which opencode uses: even a read-only reader coordinates through
    the ``-shm`` shared-memory index next to the database, and may create or update it. That file is
    lock bookkeeping, not data. It is only a problem when it is created by a *different* user, who
    then owns a sidecar the database's real owner can no longer write. So this happens only when
    we are the database's owner; for anyone else's database the connection is ``immutable=1``, which
    never touches a sidecar file but may not see sessions written since the last checkpoint (they
    are picked up on a later run).
    """
    path = os.path.abspath(db_path)
    try:
        foreign = os.stat(path).st_uid != os.geteuid()
    except OSError:
        foreign = False
    uri = "file:" + path + ("?mode=ro&immutable=1" if foreign else "?mode=ro")
    con = sqlite3.connect(uri, uri=True, timeout=10)
    con.execute("PRAGMA query_only = ON")
    return con


def _load_opencode_session(db_path: str, session_id: str) -> Session:
    sess = Session()
    con = open_readonly(db_path)
    try:
        roles: dict[str, tuple[str, str]] = {}
        order: list[str] = []
        for mid, created, data in con.execute(
            "SELECT id, time_created, data FROM message WHERE session_id = ? "
            "ORDER BY time_created, id",
            (session_id,),
        ):
            try:
                role = json.loads(data).get("role", "")
            except ValueError:
                role = ""
            roles[mid] = (role, _ms_date(created))
            order.append(mid)
        parts: dict[str, list[dict]] = {}
        for mid, data in con.execute(
            "SELECT message_id, data FROM part WHERE session_id = ? ORDER BY time_created, id",
            (session_id,),
        ):
            try:
                parts.setdefault(mid, []).append(json.loads(data))
            except ValueError:
                continue
    finally:
        con.close()

    for mid in order:
        role, date = roles[mid]
        texts = []
        for p in parts.get(mid, []):
            ptype = p.get("type")
            if ptype == "text":
                # Text opencode injected on the user's behalf (file attachments, reminders) is not
                # something the user said.
                if not p.get("synthetic") and not p.get("ignored"):
                    texts.append(str(p.get("text") or ""))
            elif ptype == "tool":
                state = p.get("state") or {}
                inp = state.get("input") or {}
                cmd = inp.get("command") if isinstance(inp, dict) else None
                sess.calls.append(
                    ToolCall(str(p.get("tool") or "?"), cmd.strip() if isinstance(cmd, str) else "")
                )
                status = state.get("status")
                if status == "error":
                    sess.results.append(ToolResult(True, str(state.get("error") or "")))
                elif status == "completed":
                    sess.results.append(ToolResult(False, str(state.get("output") or "")))
        text = " ".join(t for t in texts if t).strip()
        if text and role in ("user", "assistant"):
            sess.turns.append(Turn(role, date, text))
    return sess


def iter_opencode(name: str, pattern: str) -> Iterator[SessionRef]:
    for db_path in sorted(glob.glob(os.path.expanduser(pattern))):
        if not os.path.isfile(db_path):
            continue
        try:
            con = open_readonly(db_path)
        except sqlite3.Error as e:
            raise SourceError(f"{db_path}: cannot open read-only: {e}") from e
        try:
            rows = con.execute("SELECT id, time_updated FROM session ORDER BY time_created, id").fetchall()
        except sqlite3.Error as e:
            raise SourceError(f"{db_path}: not an opencode 1.x database ({e})") from e
        finally:
            con.close()
        for sid, updated in rows:
            short = sid[4:12] if sid.startswith("ses_") else sid[:8]
            yield SessionRef(
                name,
                f"{sid}:{updated}",
                short,
                f"{db_path}#{sid}",
                lambda d=db_path, s=sid: _load_opencode_session(d, s),
            )


SOURCE_TYPES = {
    "claude-code": iter_claude_code,
    "opencode": iter_opencode,
}


def iter_source(kind: str, name: str, path: str) -> Iterator[SessionRef]:
    try:
        fn = SOURCE_TYPES[kind]
    except KeyError:
        raise SourceError(f"unknown source type {kind!r}; expected one of {sorted(SOURCE_TYPES)}") from None
    return fn(name, path)
