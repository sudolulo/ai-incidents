"""Command line: ``ai-incidents run | scan | init | reindex | config``."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from . import __version__, config, gitops, pipeline
from .config import ConfigError
from .envelope import EnvelopeError, WriteGuard
from .state import LockedError, State


def _say(msg: str) -> None:
    try:
        print(msg, flush=True)
    except BrokenPipeError:
        # The reader went away (`ai-incidents scan --show | head`). Stop printing, keep working:
        # a run must not be abandoned halfway because nobody is reading its progress.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


def cmd_run(args, cfg) -> int:
    o = pipeline.run(
        cfg,
        dry_run=args.dry_run,
        with_judge=args.with_judge,
        backfill=args.backfill,
        notify_cmd=args.notify_cmd,
        use_git=False if args.no_git else None,
        push=args.push,
        verbose=args.verbose,
        say=_say,
    )
    return o.code


def cmd_scan(args, cfg) -> int:
    """Show what the pre-filter finds. Reads transcripts and state; writes nothing."""
    state = State(cfg.state.file, cfg.state.seen_list)
    res = pipeline.scan(cfg, state, backfill=args.backfill)
    for line in res.summary():
        _say(line.replace("**", ""))
    if args.show:
        for c in res.candidates:
            _say("\n" + c.render())
    return pipeline.EXIT_OK


def cmd_init(args, cfg_path: str) -> int:
    ledger_dir = os.path.abspath(os.path.expanduser(args.ledger))
    if os.path.exists(cfg_path) and not args.force:
        _say(f"{cfg_path} already exists (use --force to overwrite)")
        return pipeline.EXIT_USAGE
    guard = WriteGuard(dirs=[ledger_dir], files=[cfg_path])
    guard.write_text(cfg_path, config.EXAMPLE.format(ledger=ledger_dir, state=config.default_state_path()))
    _say(f"wrote {cfg_path}")
    guard.makedirs(ledger_dir)
    if args.git and not gitops.is_repo(ledger_dir):
        subprocess.run(["git", "init", "--quiet", ledger_dir], check=True)
        _say(f"initialised a git repository in {ledger_dir}")
    _say("next: `ai-incidents run --dry-run` to see what would be judged")
    return pipeline.EXIT_OK


def cmd_config(args, cfg) -> int:
    _say(f"config:  {cfg.path}")
    for s in cfg.sources:
        _say(f"source:  {s.name} ({s.type}) {s.path}")
    j = cfg.judge
    _say(f"judge:   {pipeline._judge_label(cfg)}, timeout {j.timeout}s")
    _say(f"ledger:  {cfg.ledger.dir or '(not set)'}"
         + (" [git]" if cfg.ledger.dir and gitops.is_repo(cfg.ledger.dir) and cfg.git.enabled else ""))
    _say(f"state:   {cfg.state.file}")
    if cfg.state.seen_list:
        _say(f"seen:    {cfg.state.seen_list}")
    _say(f"writes:  {cfg.ledger.dir}/ and {cfg.state.file}" + (f" and {cfg.state.seen_list}" if cfg.state.seen_list else ""))
    return pipeline.EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ai-incidents",
        description="Find the moments your AI coding agents caused real damage, and keep a ledger of the lessons.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("-c", "--config", help="config file (default: $AI_INCIDENTS_CONFIG or ~/.config/ai-incidents/config.toml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="scan, judge, and update the ledger")
    r.add_argument("--dry-run", action="store_true",
                   help="scan and show what would be judged; call no model and write nothing")
    r.add_argument("--with-judge", action="store_true",
                   help="with --dry-run: do call the judge and print the report, but still write nothing")
    r.add_argument("--backfill", action="store_true",
                   help="judge existing sessions on a first run instead of taking a baseline")
    r.add_argument("--notify-cmd", help="command to run when incidents are filed (overrides hooks.notify_cmd)")
    r.add_argument("--no-git", action="store_true", help="write the ledger but do not commit")
    push = r.add_mutually_exclusive_group()
    push.add_argument("--push", dest="push", action="store_true", default=None, help="push after committing")
    push.add_argument("--no-push", dest="push", action="store_false", help="do not push")
    r.add_argument("-v", "--verbose", action="store_true", help="with --dry-run: print the full judge prompt")

    s = sub.add_parser("scan", help="show what the pre-filter finds; read-only")
    s.add_argument("--show", action="store_true", help="print the candidates")
    s.add_argument("--backfill", action="store_true", help="include sessions a first run would baseline")

    i = sub.add_parser("init", help="write a starter config and create the ledger directory")
    i.add_argument("--ledger", required=True, help="ledger directory to create")
    i.add_argument("--git", action="store_true", help="also `git init` the ledger directory")
    i.add_argument("--force", action="store_true", help="overwrite an existing config")

    sub.add_parser("reindex", help="re-rank incidents.md and rebuild index.json after hand edits")
    sub.add_parser("config", help="print the effective configuration")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg_path = os.path.abspath(os.path.expanduser(args.config)) if args.config else config.default_config_path()
    try:
        if args.cmd == "init":
            return cmd_init(args, cfg_path)
        cfg = config.load(cfg_path, required=args.cmd in ("run", "reindex"))
        if args.cmd == "run":
            return cmd_run(args, cfg)
        if args.cmd == "scan":
            return cmd_scan(args, cfg)
        if args.cmd == "reindex":
            return pipeline.reindex(cfg, say=_say)
        if args.cmd == "config":
            return cmd_config(args, cfg)
    except ConfigError as e:
        print(f"ai-incidents: config: {e}", file=sys.stderr)
        return pipeline.EXIT_USAGE
    except LockedError as e:
        print(f"ai-incidents: {e}; not starting a second run", file=sys.stderr)
        return pipeline.EXIT_LOCKED
    except (EnvelopeError, ValueError, OSError) as e:
        print(f"ai-incidents: {e}", file=sys.stderr)
        return pipeline.EXIT_FAILED
    return pipeline.EXIT_USAGE
