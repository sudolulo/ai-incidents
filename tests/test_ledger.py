import json
import os

from ai_incidents import ledger
from ai_incidents.ledger import Entry, add_entries, add_patterns, fingerprint, index, parse, render

HAND = """# AI incidents

Times AI-generated work bit me.

> Seeded by hand.

## Newer low thing · 2026-09-04 · LOW
- **What:** a
- **Cost:** b
- **Lesson:** c

## Leaked a key into the transcript · 2026-07-13 · HIGH
- **What:** printed the config
  and a continuation line written by hand
- **Cost:** rotation
- **Lesson:** never cat a config

## Spiral with a version range · v0.5.8–v0.6.0 · MEDIUM
- **Project:** a hand-added field
- **What:** x
- **Cost:** y
- **Lesson:** z

## Recurring patterns
- **Config dumps leak secrets** -- redact at the source.

---
*Add new incidents at the top.*
"""


def test_parse_keeps_header_entries_and_footer():
    led = parse(HAND)
    assert led.header.startswith("# AI incidents") and led.header.endswith("> Seeded by hand.")
    assert [(e.title, e.date, e.severity) for e in led.entries] == [
        ("Newer low thing", "2026-09-04", "LOW"),
        ("Leaked a key into the transcript", "2026-07-13", "HIGH"),
        ("Spiral with a version range", "v0.5.8–v0.6.0", "MEDIUM"),
    ]
    assert led.footer.startswith("## Recurring patterns")
    assert led.footer.endswith("*Add new incidents at the top.*")
    assert "  and a continuation line written by hand" in led.entries[1].body


def test_render_ranks_by_severity_then_date():
    out = render(parse(HAND))
    heads = [ln for ln in out.splitlines() if ln.startswith("## ") and "·" in ln]
    assert heads == [
        "## Leaked a key into the transcript · 2026-07-13 · HIGH",
        "## Spiral with a version range · v0.5.8–v0.6.0 · MEDIUM",
        "## Newer low thing · 2026-09-04 · LOW",
    ]
    assert out.index("## Recurring patterns") > out.index("## Newer low thing")


def test_render_newest_order():
    out = render(parse(HAND), order="newest")
    heads = [ln.split(" · ")[0] for ln in out.splitlines() if ln.startswith("## ") and "·" in ln]
    # undated (non-ISO) entries sort last
    assert heads == ["## Newer low thing", "## Leaked a key into the transcript", "## Spiral with a version range"]


def test_round_trip_is_stable_and_lossless():
    once = render(parse(HAND))
    assert render(parse(once)) == once
    for line in HAND.splitlines():
        if line.strip() and line.strip() != "---":
            assert line in once, line


def test_same_severity_ties_keep_existing_order():
    text = "# L\n\n## B · 2026-01-01 · LOW\n- x\n\n## A · 2026-01-01 · LOW\n- y\n"
    assert [e.title for e in ledger.ordered(parse(text).entries)] == ["B", "A"]


def test_add_entries_dedupes_by_title_fingerprint():
    led = parse(HAND)
    new = [Entry.new("leaked a KEY into the transcript!", "2026-07-20", "LOW", "w", "c", "l"),
           Entry.new("Something new", "2026-09-10", "MEDIUM", "w", "c", "l")]
    added, dupes = add_entries(led, new)
    assert [e.title for e in added] == ["Something new"]
    assert [e.title for e in dupes] == ["leaked a KEY into the transcript!"]
    assert fingerprint("Leaked a key into the transcript") == fingerprint("leaked a KEY into the transcript!")


def test_model_output_cannot_break_the_structure():
    e = Entry.new("## Title · with a dot\nand a newline", "2026-01-01", "HIGH",
                  "what\n## Injected heading · 2026-01-01 · HIGH", "cost", "lesson")
    led = ledger.Ledger("# L", [e])
    reparsed = parse(render(led))
    assert len(reparsed.entries) == 1
    assert reparsed.entries[0].title == "Title - with a dot and a newline"
    assert "Injected heading" in reparsed.entries[0].body[0]


def test_new_entry_format():
    e = Entry.new("Pushed to main", "2026-07-11", "MEDIUM", "pushed", "a revert", "ask first")
    assert e.render() == (
        "## Pushed to main · 2026-07-11 · MEDIUM\n"
        "- **What:** pushed\n- **Cost:** a revert\n- **Lesson:** ask first"
    )


def test_patterns_append_to_existing_section_and_dedupe():
    led = parse(HAND)
    added = add_patterns(led, ["Matching by name instead of an owned id", "Matching by name instead of an owned id"])
    assert added == ["Matching by name instead of an owned id"]
    lines = led.footer.splitlines()
    i = lines.index("- Matching by name instead of an owned id")
    assert lines[i - 1].startswith("- **Config dumps")
    assert led.footer.rstrip().endswith("*Add new incidents at the top.*")
    assert add_patterns(led, ["Matching by name instead of an owned id"]) == []


def test_patterns_section_created_when_missing():
    led = ledger.Ledger("# L", [Entry.new("t", "2026-01-01", "LOW", "w", "c", "l")])
    add_patterns(led, ["a pattern"])
    assert render(led).rstrip().endswith("## Recurring patterns\n\n- a pattern")


def test_index_preserves_first_seen_and_counts():
    led = parse(HAND)
    fp = fingerprint("Leaked a key into the transcript")
    prior = {"incidents": [{"fp": fp, "first_seen": "2026-07-13"}]}
    idx = index(led, prior, "2026-09-21")
    assert idx["counts"] == {"total": 3, "high": 1, "medium": 1, "low": 1}
    by = {i["fp"]: i for i in idx["incidents"]}
    assert by[fp]["first_seen"] == "2026-07-13"
    assert by[fingerprint("Newer low thing")]["first_seen"] == "2026-09-21"
    json.loads(ledger.dump_index(idx))


def test_fingerprint_matches_the_original_scheme():
    # Same algorithm as the state.json of the sweep this was extracted from, so an existing index
    # keeps its first_seen dates.
    import hashlib
    t = "Committed 14 embedded git repos to `main` with `git add -A`"
    norm = "committed-14-embedded-git-repos-to-main-with-git-add-a"
    assert fingerprint(t) == hashlib.sha1(norm.encode()).hexdigest()[:12]


def test_sample_ledger_is_in_canonical_form():
    path = os.path.join(os.path.dirname(__file__), "..", "examples", "sample-ledger.md")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    led = parse(text)
    assert 4 <= len(led.entries) <= 6
    assert render(led) == text
