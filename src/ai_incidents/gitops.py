"""Commit (and optionally push) the ledger. The only git operations this tool performs.

* Only the files this run wrote are staged and committed, by explicit path, with
  ``git commit -- <paths>``. Anything else in the ledger repository, staged or not, is left alone.
  ``git add -A`` is not a review step.
* Push is off by default. When on, it pushes the current branch to the configured remote under its
  own name: no refspec rewriting, no force, no tags. A detached HEAD is refused.
* ``expected_remote_url`` pins the remote: if the checkout has been repointed, the push is refused
  instead of publishing the ledger somewhere else.
* Git never prompts: an unattended run must fail, not hang waiting for a password.
"""

from __future__ import annotations

import os
import subprocess

from .config import GitConfig


class GitError(RuntimeError):
    pass


def _git(repo: str, *args: str, check: bool = True, cfg: GitConfig | None = None) -> subprocess.CompletedProcess:
    pre = []
    if cfg and cfg.author_name:
        pre += ["-c", f"user.name={cfg.author_name}"]
    if cfg and cfg.author_email:
        pre += ["-c", f"user.email={cfg.author_email}"]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""}
    p = subprocess.run(["git", *pre, "-C", repo, *args], capture_output=True, text=True, env=env)
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {(p.stderr or p.stdout).strip()[-400:]}")
    return p


def is_repo(path: str) -> bool:
    if not os.path.isdir(path):
        return False
    p = _git(path, "rev-parse", "--show-toplevel", check=False)
    return p.returncode == 0 and os.path.realpath(p.stdout.strip()) == os.path.realpath(path)


def current_branch(repo: str) -> str:
    return _git(repo, "branch", "--show-current").stdout.strip()


def _check_remote(repo: str, cfg: GitConfig) -> None:
    if not cfg.expected_remote_url:
        return
    url = _git(repo, "remote", "get-url", cfg.remote, check=False).stdout.strip()
    if url.rstrip("/").removesuffix(".git") != cfg.expected_remote_url.rstrip("/").removesuffix(".git"):
        raise GitError(f"remote {cfg.remote} is {url!r}, not the expected {cfg.expected_remote_url!r}; refusing")


def _branch(repo: str, cfg: GitConfig) -> str:
    branch = current_branch(repo)
    if not branch:
        raise GitError("detached HEAD; refusing")
    if cfg.branch and branch != cfg.branch:
        raise GitError(f"ledger repo is on {branch!r}, configured branch is {cfg.branch!r}; refusing")
    return branch


def pull(repo: str, cfg: GitConfig) -> str | None:
    """Fast-forward from the remote before writing. Returns a warning, or None."""
    _check_remote(repo, cfg)
    branch = _branch(repo, cfg)
    p = _git(repo, "pull", "--ff-only", "--quiet", cfg.remote, branch, check=False)
    if p.returncode != 0:
        return f"git pull --ff-only failed: {(p.stderr or p.stdout).strip()[-200:]}"
    return None


def commit(repo: str, paths: list[str], message: str, cfg: GitConfig) -> str | None:
    """Commit exactly ``paths``. Returns the short hash, or None when nothing changed."""
    _branch(repo, cfg)
    rel = [os.path.relpath(p, repo) for p in paths]
    for r in rel:
        if r.startswith(".."):
            raise GitError(f"refusing to commit a path outside the ledger repo: {r}")
    _git(repo, "add", "--", *rel)
    if _git(repo, "diff", "--cached", "--quiet", "--", *rel, check=False).returncode == 0:
        return None
    _git(repo, "commit", "--quiet", "-m", message, "--", *rel, cfg=cfg)
    return _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()


def push(repo: str, cfg: GitConfig) -> None:
    _check_remote(repo, cfg)
    branch = _branch(repo, cfg)
    _git(repo, "push", "--quiet", "--no-follow-tags", cfg.remote, f"refs/heads/{branch}:refs/heads/{branch}")
