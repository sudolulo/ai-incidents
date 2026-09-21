# Contributing

Bug reports and pull requests are welcome at https://github.com/sudolulo/ai-incidents.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
pytest
```

The package has no runtime dependencies beyond the Python standard library (3.11+), and it should
stay that way.

## Ground rules

- **The permission envelope is the product.** Every file write goes through `WriteGuard`;
  transcripts are only ever opened read-only; the judge gets text on stdin and no tools. A change
  that weakens any of these needs a very good reason and a test.
- **A new pre-filter pattern needs a test** showing a transcript line it catches, and ideally one it
  must not catch. The pre-filter favours recall, but every false positive costs judge tokens.
- **Never commit a real transcript.** Build fixtures with the helpers in `tests/helpers.py`. Test
  secrets are assembled at runtime so the source does not trip secret scanners.
- **Keep the ledger format stable.** People hand-edit `incidents.md`; a format change must parse
  the old format and must not rewrite hand-written entries.
- Update `CHANGELOG.md` under `Unreleased`.
