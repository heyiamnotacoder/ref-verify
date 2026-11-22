# Coding Standards

- **Zero runtime dependencies**: Use only the Python 3.10+ standard library in `src/ref_verify/`. Keep `dependencies = []` in `pyproject.toml`.
- **Fail-closed, pinpointing diagnostics**: Emit structured `Diagnostic` records (`code`, `severity`, `message`, `expected`, `actual`, `hint`, `fix`, plus `details` for diffs) whenever an identifier, metadata field, retraction state, or claim number fails verification.
- **Pure core, thin network boundary**: Keep syntax validation, XML/JSON parsing, text/journal normalization, metadata comparison, claim number matching, and document extraction pure. Confine HTTP calls, retries, and NCBI rate limiting to `HttpClient`.
- **Layered modules, no cycles**: Keep each module single-purpose and import only from lower layers (see `CLAUDE.md`). Cross-module helpers are public names; never import an `_underscore` name from another module.
- **Offline deterministic tests**: Mock `HttpClient` in `tests/` so unit tests run deterministically with zero live network calls.
- **Keep docs synchronized**: Update `README.md` and `skills/ref-verify/SKILL.md` in the same change whenever CLI subcommands, flags, or diagnostic codes change.
- **Pass the single gate**: Run `./scripts/check.sh` cleanly (`ruff check` incl. pyupgrade `UP` typing rules, `ruff format --check`, and full `unittest` discovery) before committing.
