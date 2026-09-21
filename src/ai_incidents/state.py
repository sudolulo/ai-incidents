"""Which sessions have already been judged.

A session is recorded as *seen* only once its candidates can no longer be lost:

* a session the pre-filter found clean is seen immediately (it has nothing a judge could file);
* a session with candidates is seen only after the judge's verdict has been written to the ledger
  (and committed, when git is configured). If the judge fails, times out, hits a quota, or returns
  something that is not a valid verdict, nothing is recorded and the next run shows it again.

Keys include size and modification time (or opencode's ``time_updated``), so a session that grows
after it was judged is looked at again.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os

from .envelope import WriteGuard

VERSION = 1


class LockedError(RuntimeError):
    pass


class RunLock:
    """One run at a time per state file. Non-blocking: a second run exits instead of queueing."""

    def __init__(self, guard: WriteGuard, state_path: str):
        self.path = guard.check(state_path + ".lock")
        self.fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "a")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            raise LockedError(f"another run holds {self.path}") from None
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()


class State:
    def __init__(self, path: str, seen_list: str = "", seen_list_source: str = "claude-code"):
        self.path = os.path.abspath(os.path.expanduser(path))
        self.seen_list = os.path.abspath(os.path.expanduser(seen_list)) if seen_list else ""
        self.seen_list_source = seen_list_source
        self.data = {"version": VERSION, "sources": {}, "last_run": None}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = None
        except ValueError as e:
            raise ValueError(f"state file {self.path} is not valid JSON: {e}") from e
        if data:
            if data.get("version") != VERSION:
                raise ValueError(f"state file {self.path} has unsupported version {data.get('version')!r}")
            self.data = data
            for src in self.data["sources"].values():
                src["seen"] = set(src.get("seen", []))
        # The optional seen-list is read as well as written, so an external cleanup job's view and
        # ours can never disagree, and an existing list (from an earlier install) is honoured.
        if self.seen_list and os.path.exists(self.seen_list):
            with open(self.seen_list, encoding="utf-8") as f:
                keys = {ln.strip() for ln in f if ln.strip()}
            if keys:
                src = self._src(self.seen_list_source)
                src["seen"] |= keys
                src["initialized"] = True

    def _src(self, name: str) -> dict:
        return self.data["sources"].setdefault(name, {"initialized": False, "seen": set()})

    def initialized(self, name: str) -> bool:
        return bool(self._src(name).get("initialized"))

    def seen(self, name: str) -> set[str]:
        return self._src(name)["seen"]

    def mark_seen(self, name: str, keys) -> None:
        src = self._src(name)
        src["seen"] |= set(keys)
        src["initialized"] = True

    def mark_initialized(self, name: str) -> None:
        self._src(name)["initialized"] = True

    def record_run(self, summary: dict) -> None:
        self.data["last_run"] = {"at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"), **summary}

    def save(self, guard: WriteGuard) -> None:
        out = {
            "version": VERSION,
            "sources": {
                name: {"initialized": bool(s.get("initialized")), "seen": sorted(s["seen"])}
                for name, s in sorted(self.data["sources"].items())
            },
            "last_run": self.data.get("last_run"),
        }
        guard.write_text(self.path, json.dumps(out, indent=1) + "\n")
        if self.seen_list:
            keys = sorted(self.seen(self.seen_list_source))
            guard.write_text(self.seen_list, "\n".join(keys) + ("\n" if keys else ""))
