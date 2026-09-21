"""The write side of the permission envelope.

Every file this tool writes goes through a ``WriteGuard``. The guard is built once per run from the
configuration and knows exactly which places may be written: the ledger directory, the state file
(and its lock), and the optional seen-list export. Anything else raises ``EnvelopeError`` before a
byte is written, so a bug, a hostile path in the config, or a judge that tries to smuggle a file
name into its verdict cannot turn this tool into a general-purpose writer.

The read side is simpler: transcripts are opened with ``open(..., "r")`` and SQLite ``mode=ro``,
and the judge is given text on stdin and has no tools at all. See the README's "Permission
envelope" section.
"""

from __future__ import annotations

import os
import tempfile


class EnvelopeError(PermissionError):
    pass


def _real(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


class WriteGuard:
    def __init__(self, dirs: list[str] = (), files: list[str] = ()):
        self.dirs = [_real(d) for d in dirs if d]
        self.files = {_real(f) for f in files if f}

    def check(self, path: str) -> str:
        real = _real(path)
        if real in self.files:
            return real
        for d in self.dirs:
            if real == d or real.startswith(d + os.sep):
                return real
        raise EnvelopeError(f"refusing to write outside the envelope: {path}")

    def makedirs(self, path: str) -> None:
        os.makedirs(self.check(path), exist_ok=True)

    def write_text(self, path: str, content: str) -> None:
        """Atomic write: a reader never sees a half-written file, and a crash leaves the old one."""
        real = self.check(path)
        parent = os.path.dirname(real)
        os.makedirs(parent, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, real)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
