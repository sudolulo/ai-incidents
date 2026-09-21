"""The ledger: a markdown file of incidents, ranked by what they cost.

``incidents.md`` is the source of truth and stays hand-editable. Each entry is

    ## <short title> · <YYYY-MM-DD> · <HIGH|MEDIUM|LOW>
    - **What:** one sentence: what the agent did
    - **Cost:** one sentence: what it actually cost
    - **Lesson:** one actionable sentence: how not to repeat it

Everything above the first entry is the header and everything from the first non-entry ``##``
section after the entries (for example ``## Recurring patterns``) is the footer; both are kept
verbatim. Entry bodies are kept verbatim too, so a hand-written entry with extra lines survives a
run untouched. Only the order changes: most severe first, newest first within a severity.

``index.json`` is derived from ``incidents.md`` on every write and is never authored, so a confused
run can garble prose but cannot corrupt the index.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from dataclasses import dataclass, field

SEVERITIES = ("HIGH", "MEDIUM", "LOW")
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}
SEP = " · "

HEAD = re.compile(
    r"^##\s+(?P<title>.+?)\s+·\s+(?P<date>[^·]+?)\s+·\s+(?P<sev>HIGH|MEDIUM|LOW)\s*$"
)
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RULE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")

DEFAULT_HEADER = """# {title}

Times AI-generated work caused a real problem: what it did, what it cost, and the lesson.
Most severe first; newest first within a severity. Written by `ai-incidents`; hand edits and
hand-written entries are kept.
"""


def fingerprint(title: str) -> str:
    """Stable identity for an incident: its title, normalised. Severity gets re-rated and dates get
    corrected, but a retitled incident is a different incident."""
    norm = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return hashlib.sha1(norm.encode()).hexdigest()[:12]


def one_line(s: str, limit: int = 1000) -> str:
    """Collapse whitespace. A newline in model output must never become a new heading."""
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s[:limit]


def clean_title(s: str) -> str:
    # The separator is structural; a title containing it would parse as a different entry.
    return one_line(s, 160).replace("·", "-").lstrip("#").strip()


@dataclass
class Entry:
    title: str
    date: str
    severity: str
    body: list[str] = field(default_factory=list)

    @property
    def fp(self) -> str:
        return fingerprint(self.title)

    def heading(self) -> str:
        return f"## {self.title}{SEP}{self.date}{SEP}{self.severity}"

    def render(self) -> str:
        return "\n".join([self.heading(), *self.body])

    def sort_date(self) -> str:
        m = re.match(r"\d{4}-\d{2}-\d{2}", self.date)
        return m.group(0) if m else ""

    def what(self) -> str:
        for ln in self.body:
            m = re.match(r"^\s*-\s*\*\*What:\*\*\s*(.*)$", ln)
            if m:
                return m.group(1).strip()
        return ""

    @classmethod
    def new(cls, title, date, severity, what, cost, lesson) -> "Entry":
        return cls(
            clean_title(title),
            date,
            severity,
            [f"- **What:** {one_line(what)}", f"- **Cost:** {one_line(cost)}", f"- **Lesson:** {one_line(lesson)}"],
        )


@dataclass
class Ledger:
    header: str = ""
    entries: list[Entry] = field(default_factory=list)
    footer: str = ""

    def fps(self) -> set[str]:
        return {e.fp for e in self.entries}


def _trim(lines: list[str]) -> list[str]:
    """Drop trailing blank lines and horizontal rules: they are separators, not content."""
    while lines and (not lines[-1].strip() or _RULE.match(lines[-1])):
        lines = lines[:-1]
    return lines


def parse(text: str) -> Ledger:
    lines = text.splitlines()
    header: list[str] = []
    entries: list[Entry] = []
    footer: list[str] = []
    cur: Entry | None = None
    in_footer = False
    for ln in lines:
        m = HEAD.match(ln)
        if in_footer:
            if m:
                # An entry after the footer started: treat it as an entry, keep the footer intact.
                in_footer = False
            else:
                footer.append(ln)
                continue
        if m:
            if cur:
                cur.body = _trim(cur.body)
                entries.append(cur)
            cur = Entry(m.group("title").strip(), m.group("date").strip(), m.group("sev"))
        elif ln.startswith("## ") and entries + ([cur] if cur else []):
            if cur:
                cur.body = _trim(cur.body)
                entries.append(cur)
                cur = None
            in_footer = True
            footer.append(ln)
        elif cur is not None:
            cur.body.append(ln)
        else:
            header.append(ln)
    if cur:
        cur.body = _trim(cur.body)
        entries.append(cur)
    return Ledger("\n".join(_trim(header)), entries, "\n".join(footer).rstrip())


def ordered(entries: list[Entry], order: str = "severity") -> list[Entry]:
    # sorted() is stable, so entries that tie keep their existing relative order.
    newest = sorted(entries, key=lambda e: e.sort_date(), reverse=True)
    if order == "newest":
        return newest
    return sorted(newest, key=lambda e: SEV_RANK.get(e.severity, len(SEVERITIES)))


def render(ledger: Ledger, order: str = "severity") -> str:
    parts = [ledger.header.rstrip()] if ledger.header.strip() else []
    parts += [e.render() for e in ordered(ledger.entries, order)]
    if ledger.footer.strip():
        parts.append(ledger.footer.rstrip())
    return "\n\n".join(parts) + "\n"


def add_entries(ledger: Ledger, new: list[Entry]) -> tuple[list[Entry], list[Entry]]:
    """Add entries whose fingerprint is not already present. Returns (added, duplicates)."""
    have = ledger.fps()
    added, dupes = [], []
    for e in new:
        if e.fp in have:
            dupes.append(e)
            continue
        have.add(e.fp)
        ledger.entries.append(e)
        added.append(e)
    return added, dupes


PATTERNS_HEADING = "## Recurring patterns"


def add_patterns(ledger: Ledger, patterns: list[str]) -> list[str]:
    """Append bullets to the Recurring patterns section, creating it if needed."""
    pats = [one_line(p, 500) for p in patterns if one_line(p)]
    existing = set(re.findall(r"^\s*-\s+(.*)$", ledger.footer, flags=re.M))
    pats = [p for p in dict.fromkeys(pats) if p not in existing]
    if not pats:
        return []
    bullets = "\n".join(f"- {p}" for p in pats)
    footer = ledger.footer
    if PATTERNS_HEADING in footer:
        lines = footer.splitlines()
        start = next(i for i, ln in enumerate(lines) if ln.strip() == PATTERNS_HEADING)
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ") or _RULE.match(lines[i])), len(lines))
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        lines[end:end] = bullets.splitlines()
        ledger.footer = "\n".join(lines)
    else:
        ledger.footer = (PATTERNS_HEADING + "\n\n" + bullets + ("\n\n" + footer if footer.strip() else "")).rstrip()
    return pats


def index(ledger: Ledger, prior: dict | None, today: str) -> dict:
    prior_by_fp = {i.get("fp"): i for i in (prior or {}).get("incidents", []) if isinstance(i, dict)}
    items = []
    for e in ordered(ledger.entries):
        items.append({
            "fp": e.fp,
            "title": e.title,
            "date": e.date,
            "severity": e.severity,
            "first_seen": prior_by_fp.get(e.fp, {}).get("first_seen", today),
        })
    counts = {"total": len(items)}
    for s in SEVERITIES:
        counts[s.lower()] = sum(1 for i in items if i["severity"] == s)
    return {"generated": today, "counts": counts, "incidents": items}


def dump_index(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def today() -> str:
    return _dt.date.today().isoformat()
