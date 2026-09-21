"""The judge: one model call, text in, JSON out, no tools.

The judge never touches the filesystem. It receives the rubric as a system prompt and the
candidates on stdin (never in argv, where other local users could read them with ``ps``), and it
answers with a JSON verdict. The verdict is validated here before anything is written: an exit code
of 0 is not evidence of an answer. ``claude -p`` exits 0 while printing "You've hit your spend
limit", and a local model can return prose. Either is a failed run, and a failed run records
nothing, so the next run shows the same candidates again.

Backends:

* ``claude``: the Claude Code CLI in print mode, with every built-in tool and every MCP server
  disabled and session persistence off (so the judge's own session is not swept next time).
* ``openai``: any OpenAI-compatible ``/chat/completions`` endpoint. This is the local-model path:
  llama.cpp's server, Ollama, vLLM, LM Studio.
* ``command``: any program that reads a prompt on stdin and prints the answer on stdout.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from importlib import resources

from .ledger import ISO_DATE, SEVERITIES, Entry, one_line
from .prefilter import Candidate

CATEGORIES = (
    "data-loss",
    "outage",
    "credential-leak",
    "destructive-action",
    "debug-spiral",
    "unmetered-cost",
    "misattribution",
    "other",
)


class JudgeError(RuntimeError):
    pass


def default_rubric() -> str:
    return resources.files("ai_incidents").joinpath("prompts/judge.md").read_text(encoding="utf-8")


def build_message(candidates: list[Candidate], existing: list[Entry], today: str) -> str:
    lines = [f"Today is {today}.", "", f"## Candidates ({len(candidates)})", ""]
    lines += [c.render() + "\n" for c in candidates]
    lines += ["", f"## Already in the ledger ({len(existing)})", ""]
    if not existing:
        lines.append("(the ledger is empty)")
    for i, e in enumerate(existing, 1):
        what = e.what()
        lines.append(f"- [E{i:02d}] {e.title} · {e.date} · {e.severity}" + (f" -- {what[:200]}" if what else ""))
    lines += ["", "Answer with the JSON object only."]
    return "\n".join(lines)


# --- verdict ---------------------------------------------------------------------------------


@dataclass
class Incident:
    title: str
    date: str
    severity: str
    category: str
    what: str
    cost: str
    lesson: str
    candidates: list[str]
    why: str

    def entry(self) -> Entry:
        return Entry.new(self.title, self.date, self.severity, self.what, self.cost, self.lesson)


@dataclass
class Exclusion:
    candidates: list[str]
    reason: str
    duplicate_of: str = ""


@dataclass
class Verdict:
    incidents: list[Incident] = field(default_factory=list)
    excluded: list[Exclusion] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)  # malformed incidents, with the reason
    unaddressed: list[str] = field(default_factory=list)  # candidate ids the judge never mentioned
    usage: dict = field(default_factory=dict)


def extract_json(text: str) -> dict:
    """The first JSON object in ``text``. Tolerates a code fence or a sentence around it, because
    models add them however firmly they are told not to."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    if start < 0:
        raise JudgeError(f"judge answer contains no JSON object: {text[:200]!r}")
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError as e:
        raise JudgeError(f"judge answer is not valid JSON ({e}): {text[start:start + 200]!r}") from e
    if not isinstance(obj, dict):
        raise JudgeError("judge answer is not a JSON object")
    return obj


def _ids(v) -> list[str]:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if str(x).strip()]


def parse_verdict(text: str, candidates: list[Candidate], today: str, redact=lambda s: s) -> Verdict:
    obj = extract_json(text)
    if not isinstance(obj.get("incidents", []), list) or not isinstance(obj.get("excluded", []), list):
        raise JudgeError("judge answer: 'incidents' and 'excluded' must be lists")
    if "incidents" not in obj and "excluded" not in obj:
        raise JudgeError("judge answer has neither 'incidents' nor 'excluded'")

    by_id = {c.id: c for c in candidates}
    v = Verdict()
    for i, raw in enumerate(obj.get("incidents") or []):
        if not isinstance(raw, dict):
            v.rejected.append(f"incident #{i + 1}: not an object")
            continue
        def get(k, raw=raw):
            return one_line(redact(str(raw.get(k) or "")))

        sev = get("severity").upper()
        missing = [k for k in ("title", "what", "cost", "lesson") if not get(k)]
        if missing or sev not in SEVERITIES:
            why = f"missing {', '.join(missing)}" if missing else f"severity {sev!r}"
            v.rejected.append(f"incident #{i + 1} ({get('title') or 'untitled'}): {why}")
            continue
        ids = [x for x in _ids(raw.get("candidates")) if x in by_id]
        date = get("date")
        if not ISO_DATE.match(date):
            dated = [by_id[x].date for x in ids if by_id[x].date]
            date = dated[0] if dated else today
        cat = get("category").lower()
        v.incidents.append(Incident(
            title=get("title"), date=date, severity=sev,
            category=cat if cat in CATEGORIES else "other",
            what=get("what"), cost=get("cost"), lesson=get("lesson"),
            candidates=ids, why=get("why"),
        ))
    for raw in obj.get("excluded") or []:
        if not isinstance(raw, dict):
            continue
        ids = [x for x in _ids(raw.get("candidates") or raw.get("candidate")) if x in by_id]
        v.excluded.append(Exclusion(ids, one_line(redact(str(raw.get("reason") or ""))) or "(no reason given)",
                                    one_line(str(raw.get("duplicate_of") or ""), 40)))
    pats = obj.get("patterns") or []
    if isinstance(pats, list):
        v.patterns = [one_line(redact(str(p)), 500) for p in pats if str(p).strip()]
    mentioned = {x for i in v.incidents for x in i.candidates} | {x for e in v.excluded for x in e.candidates}
    v.unaddressed = [c.id for c in candidates if c.id not in mentioned]
    return v


# --- backends --------------------------------------------------------------------------------


@dataclass
class JudgeConfig:
    backend: str = "claude"
    model: str = "sonnet"
    timeout: int = 900
    binary: str = "claude"
    extra_args: list[str] = field(default_factory=list)
    base_url: str = "http://localhost:8080/v1"
    api_key_env: str = ""
    json_mode: bool = True
    max_tokens: int = 16000
    command: list[str] = field(default_factory=list)
    prompt_file: str = ""


def claude_argv(cfg: JudgeConfig, system_prompt: str) -> list[str]:
    argv = [
        cfg.binary, "-p",
        "--output-format", "json",
        "--tools", "",               # no built-in tools: no Read, no Bash, no Write, no web
        "--strict-mcp-config",       # and no MCP servers, since none are passed in
        "--no-session-persistence",  # the judge's own session is not written to ~/.claude/projects
        "--system-prompt", system_prompt,
    ]
    if cfg.model:
        argv += ["--model", cfg.model]
    return argv + list(cfg.extra_args)


def _run(argv: list[str], stdin: str, timeout: int) -> str:
    # An empty working directory: no project CLAUDE.md, .mcp.json or settings get picked up, and
    # nothing the judge's CLI might write lands anywhere that matters.
    with tempfile.TemporaryDirectory(prefix="ai-incidents-judge-") as cwd:
        try:
            p = subprocess.run(
                argv, input=stdin, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                env={**os.environ, "NO_COLOR": "1"},
            )
        except FileNotFoundError as e:
            raise JudgeError(f"judge command not found: {argv[0]}") from e
        except subprocess.TimeoutExpired as e:
            raise JudgeError(f"judge timed out after {timeout}s") from e
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "").strip()[-500:]
        raise JudgeError(f"judge exited {p.returncode}: {tail}")
    return p.stdout


def _claude(cfg: JudgeConfig, system_prompt: str, message: str) -> tuple[str, dict]:
    out = _run(claude_argv(cfg, system_prompt), message, cfg.timeout)
    try:
        env = json.loads(out)
    except ValueError as e:
        raise JudgeError(f"claude did not return its JSON envelope: {out.strip()[:300]!r}") from e
    if not isinstance(env, dict):
        raise JudgeError("claude returned an unexpected envelope")
    result = str(env.get("result") or "")
    if env.get("is_error") or env.get("subtype") not in (None, "success"):
        raise JudgeError(f"claude reported an error: {result[:300] or env.get('subtype')}")
    usage = {k: env[k] for k in ("total_cost_usd", "duration_ms", "num_turns") if k in env}
    if isinstance(env.get("usage"), dict):
        u = env["usage"]
        usage["input_tokens"] = sum(int(u.get(k) or 0) for k in (
            "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        usage["output_tokens"] = int(u.get("output_tokens") or 0)
    return result, usage


def _openai(cfg: JudgeConfig, system_prompt: str, message: str) -> tuple[str, dict]:
    body = {
        "model": cfg.model,
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": message}],
        "temperature": 0,
        "max_tokens": cfg.max_tokens,
    }
    if cfg.json_mode:
        body["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    if cfg.api_key_env:
        key = os.environ.get(cfg.api_key_env, "")
        if not key:
            raise JudgeError(f"environment variable {cfg.api_key_env} is not set")
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(
        cfg.base_url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(), headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise JudgeError(f"judge endpoint returned HTTP {e.code}: {e.read()[:300]!r}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise JudgeError(f"judge endpoint unreachable: {e}") from e
    except ValueError as e:
        raise JudgeError(f"judge endpoint returned non-JSON: {e}") from e
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise JudgeError(f"unexpected response shape from judge endpoint: {str(data)[:300]}") from e
    usage = {}
    if isinstance(data.get("usage"), dict):
        usage = {"input_tokens": data["usage"].get("prompt_tokens"), "output_tokens": data["usage"].get("completion_tokens")}
    return text, usage


def _command(cfg: JudgeConfig, system_prompt: str, message: str) -> tuple[str, dict]:
    if not cfg.command:
        raise JudgeError("judge.backend = 'command' needs judge.command")
    return _run(list(cfg.command), system_prompt + "\n\n---\n\n" + message, cfg.timeout), {}


BACKENDS = {"claude": _claude, "openai": _openai, "command": _command}


def call(cfg: JudgeConfig, system_prompt: str, message: str) -> tuple[str, dict]:
    try:
        fn = BACKENDS[cfg.backend]
    except KeyError:
        raise JudgeError(f"unknown judge backend {cfg.backend!r}; expected one of {sorted(BACKENDS)}") from None
    return fn(cfg, system_prompt, message)
