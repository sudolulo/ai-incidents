"""Optional hooks: tell something else how the run went.

Hooks are commands you configure, so they run outside the permission envelope by definition. They
receive the run summary on stdin and as ``AI_INCIDENTS_*`` environment variables; nothing is ever
interpolated into the command line. A hook that fails is logged and ignored: a broken notifier must
not turn a successful run into a failed one, or the other way round.

* ``notify_cmd``: after a run that filed at least one incident. A quiet run is silent.
* ``on_success_cmd``: after every successful run, quiet or not. Use it as a dead-man's-switch
  heartbeat: a skipped judge is a successful run, and a monitor must be told so.
* ``on_failure_cmd``: after a run that failed (judge error, git error, bad state).
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys


def run_hook(cmd: str, summary: str, env: dict[str, str], timeout: int = 60, log=None) -> bool:
    if not cmd:
        return True
    log = log or (lambda m: print(m, file=sys.stderr))
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        log(f"hook {cmd!r}: cannot parse ({e})")
        return False
    try:
        p = subprocess.run(
            argv, input=summary, text=True, capture_output=True, timeout=timeout,
            env={**os.environ, **{k: str(v) for k, v in env.items()}},
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"hook {argv[0]}: {e}")
        return False
    if p.returncode != 0:
        log(f"hook {argv[0]} exited {p.returncode}: {(p.stderr or p.stdout).strip()[-200:]}")
        return False
    return True
