"""One TOML file configures everything. Unknown keys are an error, so a typo cannot silently turn a
setting off."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields

from .judge import BACKENDS, JudgeConfig
from .sources import SOURCE_TYPES


class ConfigError(ValueError):
    pass


def xdg(var: str, fallback: str) -> str:
    return os.environ.get(var) or os.path.expanduser(fallback)


def default_config_path() -> str:
    return os.environ.get("AI_INCIDENTS_CONFIG") or os.path.join(
        xdg("XDG_CONFIG_HOME", "~/.config"), "ai-incidents", "config.toml"
    )


def default_state_path() -> str:
    return os.path.join(xdg("XDG_STATE_HOME", "~/.local/state"), "ai-incidents", "state.json")


def default_source_path(kind: str) -> str:
    if kind == "claude-code":
        base = os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
        return os.path.join(base, "projects")
    if kind == "opencode":
        return os.path.join(xdg("XDG_DATA_HOME", "~/.local/share"), "opencode", "opencode*.db")
    return ""


@dataclass
class SourceConfig:
    type: str = ""
    path: str = ""
    name: str = ""


@dataclass
class ScanConfig:
    max_candidates: int = 50
    max_per_session: int = 12
    first_run: str = "baseline"  # "baseline" | "backfill"
    exclude: list[str] = field(default_factory=list)
    extra_destructive: list[str] = field(default_factory=list)
    extra_benign: list[str] = field(default_factory=list)
    extra_alarm: list[str] = field(default_factory=list)


@dataclass
class LedgerConfig:
    dir: str = ""
    order: str = "severity"  # "severity" | "newest"
    title: str = "AI incidents"


@dataclass
class GitConfig:
    enabled: bool = True
    push: bool = False
    remote: str = "origin"
    branch: str = ""
    expected_remote_url: str = ""
    author_name: str = ""
    author_email: str = ""


@dataclass
class StateConfig:
    file: str = ""
    seen_list: str = ""


@dataclass
class PrivacyConfig:
    redact: bool = True
    extra_redact_patterns: list[str] = field(default_factory=list)


@dataclass
class HooksConfig:
    notify_cmd: str = ""
    on_success_cmd: str = ""
    on_failure_cmd: str = ""
    timeout: int = 60


@dataclass
class Config:
    sources: list[SourceConfig] = field(default_factory=list)
    scan: ScanConfig = field(default_factory=ScanConfig)
    judge: JudgeConfig = field(default_factory=JudgeConfig)
    ledger: LedgerConfig = field(default_factory=LedgerConfig)
    git: GitConfig = field(default_factory=GitConfig)
    state: StateConfig = field(default_factory=StateConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    hooks: HooksConfig = field(default_factory=HooksConfig)
    path: str = ""


def _section(cls, raw, where: str):
    if raw is None:
        return cls()
    if not isinstance(raw, dict):
        raise ConfigError(f"[{where}] must be a table")
    known = {f.name: f for f in fields(cls)}
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise ConfigError(f"[{where}] unknown key(s): {', '.join(unknown)}")
    defaults = cls()
    for k, v in raw.items():
        want = type(getattr(defaults, k))
        if want is int and isinstance(v, bool) or not isinstance(v, want):
            raise ConfigError(f"[{where}] {k} must be {want.__name__}, got {type(v).__name__}")
        if want is list and not all(isinstance(x, str) for x in v):
            raise ConfigError(f"[{where}] {k} must be a list of strings")
    return cls(**raw)


def _expand(p: str) -> str:
    return os.path.abspath(os.path.expanduser(os.path.expandvars(p))) if p else p


def load(path: str | None = None, required: bool = True) -> Config:
    path = path or default_config_path()
    raw: dict = {}
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: {e}") from e
    elif required:
        raise ConfigError(f"no config at {path} (create one with `ai-incidents init`)")
    return from_dict(raw, path)


def from_dict(raw: dict, path: str = "") -> Config:
    top = {"source", "scan", "judge", "ledger", "git", "state", "privacy", "hooks"}
    unknown = sorted(set(raw) - top)
    if unknown:
        raise ConfigError(f"unknown top-level key(s): {', '.join(unknown)}")

    cfg = Config(path=path)
    srcs = raw.get("source")
    if srcs is None:
        srcs = [{"type": "claude-code"}, {"type": "opencode"}]
    if not isinstance(srcs, list):
        raise ConfigError("[[source]] must be an array of tables")
    names = set()
    for i, s in enumerate(srcs):
        sc = _section(SourceConfig, s, f"source #{i + 1}")
        if sc.type not in SOURCE_TYPES:
            raise ConfigError(f"[[source]] #{i + 1}: type must be one of {sorted(SOURCE_TYPES)}")
        sc.name = sc.name or sc.type
        if sc.name in names:
            raise ConfigError(f"[[source]] name {sc.name!r} is used twice; give one a distinct `name`")
        names.add(sc.name)
        sc.path = _expand(sc.path or default_source_path(sc.type))
        cfg.sources.append(sc)

    cfg.scan = _section(ScanConfig, raw.get("scan"), "scan")
    cfg.judge = _section(JudgeConfig, raw.get("judge"), "judge")
    cfg.ledger = _section(LedgerConfig, raw.get("ledger"), "ledger")
    cfg.git = _section(GitConfig, raw.get("git"), "git")
    cfg.state = _section(StateConfig, raw.get("state"), "state")
    cfg.privacy = _section(PrivacyConfig, raw.get("privacy"), "privacy")
    cfg.hooks = _section(HooksConfig, raw.get("hooks"), "hooks")

    if cfg.scan.first_run not in ("baseline", "backfill"):
        raise ConfigError("[scan] first_run must be 'baseline' or 'backfill'")
    if cfg.scan.max_candidates < 2:
        raise ConfigError("[scan] max_candidates must be at least 2")
    if not 2 <= cfg.scan.max_per_session <= cfg.scan.max_candidates:
        raise ConfigError("[scan] max_per_session must be between 2 and max_candidates")
    if cfg.ledger.order not in ("severity", "newest"):
        raise ConfigError("[ledger] order must be 'severity' or 'newest'")
    if cfg.judge.backend not in BACKENDS:
        raise ConfigError(f"[judge] backend must be one of {sorted(BACKENDS)}")
    if cfg.judge.backend == "command" and not cfg.judge.command:
        raise ConfigError("[judge] backend = 'command' needs `command = [...]`")
    if cfg.judge.timeout <= 0:
        raise ConfigError("[judge] timeout must be positive")

    cfg.ledger.dir = _expand(cfg.ledger.dir)
    cfg.state.file = _expand(cfg.state.file) or default_state_path()
    cfg.state.seen_list = _expand(cfg.state.seen_list)
    cfg.judge.prompt_file = _expand(cfg.judge.prompt_file)
    return cfg


EXAMPLE = '''\
# ai-incidents configuration. Every key is optional except ledger.dir.
# Reference: https://github.com/sudolulo/ai-incidents#configuration

# Where the transcripts are. Read-only, always. Omit [[source]] entirely to read both defaults.
[[source]]
type = "claude-code"
path = "~/.claude/projects"

[[source]]
type = "opencode"
path = "~/.local/share/opencode/opencode*.db"

[scan]
max_candidates = 50      # most candidates shown to the judge in one run
max_per_session = 12     # most candidates from any one session
first_run = "baseline"   # "baseline": mark existing sessions judged; "backfill": judge them

[judge]
backend = "claude"       # "claude" | "openai" | "command"
model = "sonnet"
timeout = 900
# For a local model, use an OpenAI-compatible server instead (llama.cpp, Ollama, vLLM):
# backend = "openai"
# base_url = "http://localhost:11434/v1"
# model = "qwen3:32b"

[ledger]
dir = "{ledger}"
order = "severity"       # "severity" (most severe first) | "newest"

[git]
enabled = true           # commit the ledger if ledger.dir is a git repository
push = false

[state]
file = "{state}"

[privacy]
redact = true

[hooks]
# notify_cmd = "curl -s -H 'Title: ai-incidents' --data-binary @- https://ntfy.sh/your-topic"
'''
