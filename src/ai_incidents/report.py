"""The per-run report: the judge's reasoning, shown plainly.

Confirmed incidents get one line on why they cleared the bar; every excluded candidate gets one
line on why it did not. The exclusions are the point: they keep the bar visible and arguable, and
they are where a judge drifting lax or strict shows up first.
"""

from __future__ import annotations

from .judge import Incident, Verdict
from .prefilter import Candidate


def _refs(ids: list[str], by_id: dict[str, Candidate]) -> str:
    seen, out = set(), []
    for i in ids:
        c = by_id.get(i)
        if not c:
            continue
        tag = f"{c.source} {c.session}"
        if tag not in seen:
            seen.add(tag)
            out.append(f"`{c.session}` ({c.source}, {c.date or 'undated'})")
    return ", ".join(out)


def render(
    date: str,
    scan_summary: list[str],
    candidates: list[Candidate],
    verdict: Verdict,
    filed: list[Incident],
    already: list[Incident],
    judge_label: str,
) -> str:
    by_id = {c.id: c for c in candidates}
    out = [f"# AI-incidents run · {date}", "", "## What the pre-filter found", ""]
    out += [f"- {line}" for line in scan_summary]
    out += [f"- **Judge:** {judge_label}"]
    if verdict.usage:
        u = verdict.usage
        bits = []
        if u.get("input_tokens") is not None:
            bits.append(f"{u['input_tokens']:,} input tokens")
        if u.get("output_tokens") is not None:
            bits.append(f"{u['output_tokens']:,} output tokens")
        if u.get("total_cost_usd") is not None:
            bits.append(f"${float(u['total_cost_usd']):.2f} at API rates")
        if bits:
            out.append(f"- **Judge cost:** {', '.join(bits)}")
    out += ["", f"## Confirmed incidents ({len(filed)})", ""]
    if not filed:
        out += ["Nothing cleared the bar. A quiet run is a real result, not a failure.", ""]
    for i in filed:
        out += [
            f"### {i.title} · {i.date} · {i.severity}",
            f"**Category:** {i.category} · **Sessions:** {_refs(i.candidates, by_id) or 'not given'}",
            "",
            f"Clears the bar: {i.why or '(no reason given)'}",
            "",
        ]
    if already:
        out += [f"## Confirmed but already in the ledger ({len(already)})", ""]
        out += [f"- **{i.title}** ({i.severity}): same title as an existing entry; not filed again." for i in already]
        out.append("")
    out += [f"## Excluded candidates ({sum(len(e.candidates) or 1 for e in verdict.excluded)})", ""]
    if not verdict.excluded:
        out += ["None.", ""]
    for e in verdict.excluded:
        ids = ", ".join(e.candidates) or "?"
        dup = f" Already filed as {e.duplicate_of}." if e.duplicate_of else ""
        out.append(f"- **{ids}** {_refs(e.candidates, by_id)}: {e.reason}{dup}")
    out.append("")
    if verdict.unaddressed or verdict.rejected:
        out += ["## Loose ends", ""]
        if verdict.unaddressed:
            out.append(f"- The judge did not address: {', '.join(verdict.unaddressed)}.")
        for r in verdict.rejected:
            out.append(f"- Rejected a malformed incident from the judge: {r}.")
        out.append("")
    if verdict.patterns:
        out += ["## Recurring patterns added", ""] + [f"- {p}" for p in verdict.patterns] + [""]
    out += ["## Candidates shown to the judge", ""]
    for c in candidates:
        out.append(f"- **{c.id}** {c.kind} · {c.source} · `{c.session}` · {c.date or 'undated'}")
    return "\n".join(out).rstrip() + "\n"
