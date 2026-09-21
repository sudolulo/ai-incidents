"""Best-effort removal of secret-shaped strings.

Applied twice: to transcript excerpts before they leave the machine for the judge, and to the
judge's verdict before anything is written to the ledger. A ledger that records credential leaks
must not become one.

This is pattern matching, not a guarantee. It catches the common token formats and ``key=value``
assignments of secret-named keys; it cannot recognise a bare password with no context around it.
"""

from __future__ import annotations

import re

MASK = "[REDACTED]"

_TOKEN_PATTERNS = [
    # PEM private keys, whole block
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(-----END [A-Z0-9 ]*PRIVATE KEY-----|$)",
    r"\bgh[pousr]_[A-Za-z0-9]{30,}\b",  # GitHub tokens
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
    r"\bglpat-[A-Za-z0-9_-]{20,}\b",  # GitLab
    r"\bsk-[A-Za-z0-9_-]{20,}\b",  # OpenAI / Anthropic style API keys
    r"\bAKIA[0-9A-Z]{16}\b",  # AWS access key id
    r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b",  # Slack
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",  # JWT
    r"\bAIza[0-9A-Za-z_-]{35}\b",  # Google API key
]

# Authorization headers: keep the scheme, drop the credential.
_BEARER = re.compile(r"\b(Bearer|Basic|Token)\s+[A-Za-z0-9._~+/=-]{16,}", re.I)

# key = value / key: value / "key": "value" for secret-named keys.
_ASSIGN = re.compile(
    r"(?P<key>[\"']?[A-Za-z0-9_.-]*(?:password|passwd|passphrase|secret|token|api[_-]?key|"
    r"access[_-]?key|private[_-]?key|client[_-]?secret)[A-Za-z0-9_.-]*[\"']?)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<q>[\"']?)(?P<val>[^\s\"',;&]{6,})",
    re.I,
)

# scheme://user:password@host
_URL_CREDS = re.compile(r"(?P<pre>[a-z][a-z0-9+.-]*://[^\s:/@]+:)(?P<pw>[^\s@/]+)(?P<at>@)", re.I)

_VALUE_ALLOW = re.compile(
    r"^(\[REDACTED\]|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|true|false|null|none|\*+|[0-9][0-9.,_kKmMbB]*)$", re.I
)


class Redactor:
    def __init__(self, extra_patterns: list[str] | tuple[str, ...] = (), enabled: bool = True):
        self.enabled = enabled
        self.tokens = [re.compile(p) for p in _TOKEN_PATTERNS] + [re.compile(p) for p in extra_patterns]

    def __call__(self, text: str) -> str:
        if not self.enabled or not text:
            return text
        for pat in self.tokens:
            text = pat.sub(MASK, text)
        text = _BEARER.sub(lambda m: f"{m.group(1)} {MASK}", text)
        text = _URL_CREDS.sub(lambda m: f"{m.group('pre')}{MASK}{m.group('at')}", text)

        def assign(m: re.Match) -> str:
            val = m.group("val")
            # Leave variable references and obvious placeholders alone: `password=$PW` shows how a
            # secret was passed, which is often the lesson, and is not itself a secret.
            if _VALUE_ALLOW.match(val):
                return m.group(0)
            return f"{m.group('key')}{m.group('sep')}{m.group('q')}{MASK}"

        return _ASSIGN.sub(assign, text)
