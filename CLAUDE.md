# ref-verify

Run `./scripts/check.sh` after any code or test change; it executes the exact lint, format, and offline unit test suite enforced in CI.

## Navigation

- `CODING_STANDARDS.md` — architectural invariants (zero runtime deps, pure core vs. `HttpClient`, diagnostic fields, doc sync); read before editing code.
- `src/ref_verify/cli.py` — complete CLI, Crossref/PubMed/Europe PMC parsers, document scanner, and claim checker.
- `src/ref_verify/__init__.py` — single source of `__version__` (keep aligned with `pyproject.toml`).
- `tests/test_ref_verify.py` — offline unit test suite using `FakeHttpClient`.
- `skills/ref-verify/SKILL.md` — portable agent skill definition installed via `npx skills add`; keep free of local paths and synced with CLI flags/diagnostics.
