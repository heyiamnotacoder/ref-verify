---
name: ref-verify
description: Verify scientific citations (DOI, PMID, metadata, retractions) and ground quantitative claims against live Crossref, PubMed, and Europe PMC records via ref-verify. Use when checking references, fixing broken citations, or auditing scientific manuscripts, SRMAs, journal clubs, seminars, or letters before delivery.
---

Verify every scientific reference and quantitative claim against live primary-source metadata via `ref-verify` (`ref-verify --help` for flags and options). Ground every verdict in the fetched text returned by the tool rather than memory.

## Workflow

1. **Run `ref-verify` across the target scope.**
   - Whole document or slide deck (`.md`, `.txt`, `.docx`, `.pptx`): `ref-verify scan <file>`
   - Bibliography or claim list: write a JSON array (`doi`, `pmid`, `title`, `author`, `year`, `journal`, `claim`) and run `ref-verify batch <refs.json>`
   - Single citation or specific claim: `ref-verify check --doi ... --pmid ... --title ... --author ... --year ... --journal ... --claim "..."`
   - Pass `--fulltext` when a quantitative claim cites subgroup or secondary figures absent from the abstract (fetches Open Access JATS XML from Europe PMC with NCBI PMC fallback when a PMCID is available).

2. **Triage every diagnostic code by severity.**
   - **`CRITICAL` (`RETRACTED_PAPER`, `PMID_DOI_CONFLICT`)**: Stop and resolve immediately. Remove retracted sources from supporting evidence (or label them explicitly as retracted if discussing the retraction). For conflicts, confirm which paper is intended and replace the mismatched identifier.
   - **`ERROR` (`DOI_SYNTAX_INVALID`, `PMID_SYNTAX_INVALID`, `DOI_NOT_FOUND`, `PMID_NOT_FOUND`, `TITLE_MISMATCH`, `AUTHOR_MISMATCH`, `YEAR_MISMATCH`, `JOURNAL_MISMATCH`)**: Read the `Expected`, `Actual`, `TokenDiff`, `DID_YOU_MEAN`, and `Fix` lines; correct the broken identifier or metadata field in the source deliverable.
   - **`WARNING` (`CLAIM_NUMBER_NOT_IN_ABSTRACT`, `UNLINKED_BIBLIOGRAPHY_ENTRIES`, `ABSTRACT_UNAVAILABLE`, `EXPRESSION_OF_CONCERN`, `RETRACTION_NOTICE`, ±1-year epub offset, middle-author mismatch, subtitle truncation)**: Inspect the `Actual` field and official text, fix genuine inaccuracies, resolve unlinked plain-text citations via `batch`, and verify derived metrics (such as relative risk reduction from a hazard ratio).
   - **`INFO` (`HAS_CORRECTION_NOTICE`, `FULLTEXT_UNAVAILABLE`)**: Check published errata when quoting exact figures.

3. **Render a grounded verdict for every claim.**
   Read the `CLAIM_CHECK` block (matched/missing numbers, abstract numbers, matched sentences, and printed `OFFICIAL ABSTRACT` or OA full-text excerpts) and classify each claim:
   - `SUPPORTED` — every directional and numerical statement matches the fetched primary text (including exact arithmetic equivalents like RRR = 1 − HR).
   - `PARTIAL` — direction holds, while one or more numbers, subgroups, or caveats differ; revise the claim to match the primary text.
   - `CONTRADICTED` — fetched text refutes the direction, significance, or values; rewrite or replace the claim.
   - `NEEDS_FULLTEXT` — numbers do not appear in the abstract and Europe PMC OA full text is unavailable; inspect the full-text PDF before treating the number as verified, or flag it explicitly to the user.

4. **Re-verify after edits.**
   Re-run `ref-verify` on every modified citation or scanned file after applying fixes.

## Completion criteria

Verification is complete only when:
- Every cited DOI/PMID in the deliverable has been checked live with `ref-verify`.
- Zero unresolved `CRITICAL` or `ERROR` diagnostics remain.
- Every quantitative claim is either corrected to `SUPPORTED` against fetched primary text or explicitly marked `NEEDS_FULLTEXT` with the exact unverified numbers named.
