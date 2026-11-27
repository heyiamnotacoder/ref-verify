# ref-verify

Run `./scripts/check.sh` after any code or test change; it executes the exact lint, format, and offline unit test suite enforced in CI.

## Navigation

- `CODING_STANDARDS.md` — architectural invariants (zero runtime deps, pure core vs. `HttpClient`, diagnostic fields, doc sync); read before editing code.
- `src/ref_verify/` — layered package; imports only flow downward (no cycles):
  - `models.py`, `lexicons.py`, `text.py` — shared records, static vocabularies, pure text helpers.
  - `refparse.py` — pure Vancouver/APA bibliography-entry parser (cited title/author/year/journal); conservative by design.
  - `identifiers.py`, `identity.py`, `metadata.py` — pure DOI/PMID syntax, resolution/conflict, and metadata diagnostics.
  - `quant.py`, `cohort.py`, `claims.py` — pure claim grounding (numbers, cohorts, evaluation).
  - `grounding.py` — pure per-sentence grounding and co-citation reconciliation of citing sentences.
  - `http_client.py` — the only network boundary; `sources/{crossref,pubmed,europepmc,fulltext}.py` pair pure parsers with thin fetchers.
  - `prefetch.py`, `verify.py` — batch prefetch pipeline and per-citation orchestration.
  - `scan.py` — document extraction (identifiers, parsed bibliography fields, superscript citation markers).
  - `citing.py` — pure discovery of the body sentences citing each reference (numeric, author-year, inline ID).
  - `checklist.py` — pure per-citation verdict rows (pass/info/warn/fail/skip per check column).
  - `report.py`, `table.py`, `html_report.py`, `cli.py` — text/JSON rendering, terminal checklist table, self-contained HTML report, argparse commands.
- `src/ref_verify/__init__.py` — single source of `__version__` (keep aligned with `pyproject.toml`).
- `tests/test_ref_verify.py` — offline unit test suite using `FakeHttpClient`.
- `tests/test_human_report.py` — offline tests for reference parsing, checklist rows, and both renderers.
- `tests/test_citing_claims.py` — offline tests for citing-sentence discovery, grounding, and reconciliation.
- `skills/ref-verify/SKILL.md` — portable agent skill definition installed via `npx skills add`; keep free of local paths and synced with CLI flags/diagnostics.
