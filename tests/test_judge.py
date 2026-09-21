import http.server
import json
import threading

import pytest

from ai_incidents import judge
from ai_incidents.judge import JudgeConfig, JudgeError, build_message, claude_argv, extract_json, parse_verdict
from ai_incidents.ledger import Entry
from ai_incidents.prefilter import Candidate
from ai_incidents.redact import Redactor
from helpers import judge_cmd


def cands(n=3):
    out = []
    for i in range(1, n + 1):
        c = Candidate("SNIPPET", "claude-code", f"sess{i:04d}", "2026-07-1" + str(i), f"    [user] thing {i}")
        c.id = f"C{i:02d}"
        out.append(c)
    return out


GOOD = {
    "incidents": [{
        "title": "Wiped the cache", "date": "2026-07-12", "severity": "high", "category": "data-loss",
        "what": "w", "cost": "c", "lesson": "l", "candidates": ["C01", "C99"], "why": "because",
    }],
    "excluded": [{"candidates": ["C02"], "reason": "routine", "duplicate_of": "E01"}],
    "patterns": [],
}


def test_extract_json_tolerates_fences_and_prose():
    assert extract_json('Sure!\n```json\n{"a": 1}\n```\nthanks') == {"a": 1}
    assert extract_json('verdict: {"a": {"b": 2}} trailing') == {"a": {"b": 2}}
    with pytest.raises(JudgeError):
        extract_json("You've hit your monthly spend limit.")
    with pytest.raises(JudgeError):
        extract_json("{not json")


def test_parse_verdict_validates_and_normalises():
    v = parse_verdict(json.dumps(GOOD), cands(), "2026-09-21")
    [i] = v.incidents
    assert i.severity == "HIGH" and i.candidates == ["C01"]  # unknown id dropped
    assert v.excluded[0].duplicate_of == "E01"
    assert v.unaddressed == ["C03"]


def test_malformed_incidents_are_rejected_not_filed():
    bad = {"incidents": [{"title": "x", "severity": "CATASTROPHIC", "what": "w", "cost": "c", "lesson": "l"},
                         {"title": "no lesson", "severity": "LOW", "what": "w", "cost": "c"},
                         "not an object"],
           "excluded": []}
    v = parse_verdict(json.dumps(bad), cands(), "2026-09-21")
    assert v.incidents == [] and len(v.rejected) == 3


def test_bad_date_falls_back_to_candidate_date():
    raw = {"incidents": [dict(GOOD["incidents"][0], date="last Tuesday", candidates=["C02"])], "excluded": []}
    assert parse_verdict(json.dumps(raw), cands(), "2026-09-21").incidents[0].date == "2026-07-12"


def test_unknown_category_becomes_other():
    raw = {"incidents": [dict(GOOD["incidents"][0], category="vibes")], "excluded": []}
    assert parse_verdict(json.dumps(raw), cands(), "2026-09-21").incidents[0].category == "other"


def test_verdict_text_is_redacted():
    raw = {"incidents": [dict(GOOD["incidents"][0], what="printed password=abcdef123456 to the log")],
           "excluded": []}
    v = parse_verdict(json.dumps(raw), cands(), "2026-09-21", Redactor())
    assert "abcdef123456" not in v.incidents[0].what


def test_non_object_answers_fail():
    with pytest.raises(JudgeError):
        parse_verdict('{"incidents": "none"}', cands(), "2026-09-21")
    with pytest.raises(JudgeError):
        parse_verdict('{"summary": "all quiet"}', cands(), "2026-09-21")


def test_build_message_lists_candidates_and_ledger():
    msg = build_message(cands(2), [Entry.new("Old one", "2026-07-01", "LOW", "what it did", "c", "l")], "2026-09-21")
    assert "[C01] SNIPPET · claude-code · sess0001" in msg
    assert "[E01] Old one · 2026-07-01 · LOW -- what it did" in msg


def test_claude_argv_disables_every_tool_and_keeps_content_off_argv():
    argv = claude_argv(JudgeConfig(model="sonnet"), "RUBRIC")
    assert argv[:2] == ["claude", "-p"]
    i = argv.index("--tools")
    assert argv[i + 1] == ""
    for flag in ("--strict-mcp-config", "--no-session-persistence"):
        assert flag in argv
    assert "--mcp-config" not in argv
    assert argv[argv.index("--model") + 1] == "sonnet"


def _fake_claude(tmp_path, envelope: dict | str, code: int = 0) -> str:
    """A stand-in `claude` binary that also records its argv and stdin."""
    p = tmp_path / "claude"
    out = envelope if isinstance(envelope, str) else json.dumps(envelope)
    p.write_text(
        "#!/usr/bin/env python3\nimport sys, json, os\n"
        f"open({str(tmp_path / 'argv.json')!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        f"open({str(tmp_path / 'stdin.txt')!r}, 'w').write(sys.stdin.read())\n"
        f"open({str(tmp_path / 'cwd.txt')!r}, 'w').write(os.getcwd())\n"
        f"sys.stdout.write({out!r})\nsys.exit({code})\n"
    )
    p.chmod(0o755)
    return str(p)


def test_claude_backend_reads_envelope_and_usage(tmp_path):
    env = {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(GOOD),
           "total_cost_usd": 0.12, "usage": {"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 7}}
    cfg = JudgeConfig(binary=_fake_claude(tmp_path, env))
    text, usage = judge.call(cfg, "RUBRIC", "CANDIDATES GO HERE")
    assert json.loads(text) == GOOD
    assert usage == {"total_cost_usd": 0.12, "input_tokens": 15, "output_tokens": 7}
    assert (tmp_path / "stdin.txt").read_text() == "CANDIDATES GO HERE"
    assert "CANDIDATES GO HERE" not in (tmp_path / "argv.json").read_text()
    assert "ai-incidents-judge-" in (tmp_path / "cwd.txt").read_text()


def test_claude_backend_errors(tmp_path):
    cfg = JudgeConfig(binary=_fake_claude(tmp_path, {"is_error": True, "result": "You've hit your spend limit"}))
    with pytest.raises(JudgeError, match="spend limit"):
        judge.call(cfg, "R", "M")
    cfg = JudgeConfig(binary=_fake_claude(tmp_path, "plain text, no envelope"))
    with pytest.raises(JudgeError, match="envelope"):
        judge.call(cfg, "R", "M")
    cfg = JudgeConfig(binary=_fake_claude(tmp_path, "", code=1))
    with pytest.raises(JudgeError, match="exited 1"):
        judge.call(cfg, "R", "M")
    with pytest.raises(JudgeError, match="not found"):
        judge.call(JudgeConfig(binary=str(tmp_path / "missing")), "R", "M")


def test_command_backend(monkeypatch):
    monkeypatch.setenv("FAKE_JUDGE_MODE", "file-first")
    cfg = JudgeConfig(backend="command", command=judge_cmd())
    text, _ = judge.call(cfg, "RUBRIC", "[C01] SNIPPET · x\n[C02] SNIPPET · y")
    v = parse_verdict(text, cands(2), "2026-09-21")
    assert [i.candidates for i in v.incidents] == [["C01"]] and v.excluded[0].candidates == ["C02"]


def test_command_backend_timeout(tmp_path):
    cfg = JudgeConfig(backend="command", command=["sleep", "5"], timeout=1)
    with pytest.raises(JudgeError, match="timed out"):
        judge.call(cfg, "R", "M")


class _Handler(http.server.BaseHTTPRequestHandler):
    seen = {}

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Handler.seen = {"path": self.path, "body": body, "auth": self.headers.get("Authorization")}
        resp = {"choices": [{"message": {"content": json.dumps(GOOD)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20}}
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def test_openai_backend(monkeypatch):
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        monkeypatch.setenv("LOCAL_LLM_KEY", "k-123")
        cfg = JudgeConfig(backend="openai", model="qwen3", base_url=f"http://127.0.0.1:{srv.server_port}/v1",
                          api_key_env="LOCAL_LLM_KEY")
        text, usage = judge.call(cfg, "RUBRIC", "MSG")
    finally:
        srv.shutdown()
    assert json.loads(text) == GOOD
    assert usage == {"input_tokens": 100, "output_tokens": 20}
    s = _Handler.seen
    assert s["path"] == "/v1/chat/completions" and s["auth"] == "Bearer k-123"
    assert s["body"]["messages"][0] == {"role": "system", "content": "RUBRIC"}
    assert s["body"]["response_format"] == {"type": "json_object"} and s["body"]["temperature"] == 0


def test_openai_backend_missing_key_and_unreachable(monkeypatch):
    monkeypatch.delenv("NOPE_KEY", raising=False)
    with pytest.raises(JudgeError, match="NOPE_KEY"):
        judge.call(JudgeConfig(backend="openai", api_key_env="NOPE_KEY"), "R", "M")
    with pytest.raises(JudgeError, match="unreachable"):
        judge.call(JudgeConfig(backend="openai", base_url="http://127.0.0.1:9/v1", timeout=2), "R", "M")


def test_default_rubric_ships_with_the_package():
    r = judge.default_rubric()
    assert "Answer format" in r and "misattribution" in r.lower()
