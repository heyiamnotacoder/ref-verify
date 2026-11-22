# ref-verify

Zero-dependency Python CLI and AI agent skill for verifying scientific citations and grounding quantitative claims against live **Crossref**, **PubMed (NCBI E-utilities)**, and **Europe PMC** records.

Built on the Python 3.10+ standard library with zero runtime dependencies.

## What it checks

- **Identifier syntax and resolution**: Validates and normalizes DOIs and PMIDs (catching trailing punctuation, missing `10.` prefixes, embedded whitespace, URL junk, and PMCID/PMID mix-ups) and resolves them live against Crossref, PubMed, and Europe PMC.
- **PMID–DOI conflict detection**: Flags `CRITICAL` mismatches when a cited DOI and PMID resolve to different papers.
- **Retractions and editorial notices**: Detects retracted publications (`CRITICAL`), expressions of concern (`WARNING`), retraction notices (`WARNING`), and published errata/corrections (`INFO`) across PubMed publication types, `CommentsCorrectionsList`, Crossref `updated-by` / `relation`, and Europe PMC records.
- **Field-level metadata diffs**: Cross-checks cited title (distinguishing subtitle truncation from a different paper with token-level diffs), first author (distinguishing co-author position from an absent author), print/online publication year (distinguishing ±1-year epub offsets from genuine errors), and journal name/abbreviation (with biomedical alias normalization).
- **Quantitative claim grounding**: Extracts numbers, percentages, hazard/odds/risk ratios, confidence intervals, p-values, sample sizes, and dosages from a claim, compares them against the official abstract (and Europe PMC Open Access `fullTextXML` when `--fulltext` is passed), and prints the matched sentences alongside the official abstract.
- **Document scanning**: Extracts and verifies all DOIs and PMIDs directly from `.md`, `.txt`, `.docx`, and `.pptx` files (including footnotes, split XML runs, and slide speaker notes).

## Why pinpointing diagnostics matter for AI agents

A plain pass/fail check forces an AI agent to guess why a citation failed. Every `ref-verify` diagnostic emits structured `Expected`, `Actual`, `Hint`, and `Fix` lines—plus `TokenDiff` on title mismatches, `Cleaned` candidates on malformed DOIs, and ranked `DID_YOU_MEAN` matches when a DOI or PMID is not found—so coding and research agents can repair broken citations and verify quantitative claims in a single pass.

## Installation

### CLI

Using [`uv`](https://docs.astral.sh/uv/) (recommended):

```bash
uv tool install git+https://github.com/heyiamnotacoder/ref-verify.git
```

Using `pipx`:

```bash
pipx install git+https://github.com/heyiamnotacoder/ref-verify.git
```

Using `pip`:

```bash
pip install git+https://github.com/heyiamnotacoder/ref-verify.git
```

### AI agent skill

Install the portable `ref-verify` skill into your coding agent (Claude Code, Codex, Cursor, etc.):

```bash
npx skills add heyiamnotacoder/ref-verify
```

## Usage

### Verify a single citation and claim

```bash
ref-verify check \
  --doi "10.1056/NEJMoa1911303" \
  --pmid "31535829" \
  --author "McMurray" \
  --year 2019 \
  --journal "NEJM" \
  --claim "Dapagliflozin 10 mg in 4,744 patients reduced the primary outcome (16.3% vs 21.2%; HR 0.74, 95% CI 0.65-0.85, p<0.001)."
```

Pass `--fulltext` when a claim cites subgroup or secondary figures not present in the abstract (fetches Europe PMC Open Access XML when a PMCID is available).

### Verify a batch JSON file (or stdin)

Provide a JSON array of citation objects (`id`, `doi`, `pmid`, `title`, `author`, `year`, `journal`, `claim`, `fulltext`):

```bash
ref-verify batch refs.json
cat refs.json | ref-verify batch - --json
```

### Scan a manuscript or slide deck

```bash
ref-verify scan manuscript.docx
ref-verify scan journal_club.pptx --no-abstract
ref-verify scan review.md --json
```

### Common flags and exit codes

- `--fulltext` — also fetch Europe PMC Open Access `fullTextXML` for claim checks.
- `--no-abstract` — omit the full official abstract block from text output.
- `--json` — emit machine-readable JSON output.
- `--strict` — exit `1` on `WARNING` diagnostics as well as `FAIL`.
- `--timeout <seconds>` — per-request HTTP timeout (default `15.0`).
- Exit codes: `0` on pass (or warn without `--strict`), `1` on fail (or warn with `--strict`), `2` on input/file error.

## Example diagnostic output

Running `ref-verify check` against a citation with trailing DOI punctuation, a co-author cited as first author, a wrong year and journal, and misstated claim numbers:

```bash
ref-verify check \
  --doi "10.1056/NEJMoa1911303." \
  --author "Solomon" \
  --year 2021 \
  --journal "The Lancet" \
  --claim "Dapagliflozin 10 mg reduced the primary outcome by 40% (HR 0.60, 95% CI 0.50-0.70, p<0.001)." \
  --no-abstract
```

```text
==============================================================================
STATUS: FAIL  |  TARGET: 10.1056/NEJMoa1911303.
------------------------------------------------------------------------------
OFFICIAL METADATA (Sources: pubmed, crossref):
  Title   : Dapagliflozin in Patients with Heart Failure and Reduced Ejection Fraction
  Authors : McMurray JJV, Solomon SD, Inzucchi SE, Køber L, Kosiborod MN, Martinez FA, ... (39 authors)
  Year    : print=2019 / online=2019
  Journal : The New England journal of medicine (N Engl J Med)
  IDs     : DOI:10.1056/NEJMoa1911303 | PMID:31535829
------------------------------------------------------------------------------
DIAGNOSTICS (5):
  [!] [ERROR] DOI_SYNTAX_INVALID: Malformed DOI '10.1056/NEJMoa1911303.': trailing punctuation '.'.
      Expected : 10.<4-9 digits>/<suffix>
      Actual   : 10.1056/NEJMoa1911303.
      Cleaned  : 10.1056/NEJMoa1911303
      Hint     : Common copy-paste error: trailing sentence punctuation, missing '10.' prefix, spaces, or URL parameters.
      Fix      : Replace '10.1056/NEJMoa1911303.' with '10.1056/NEJMoa1911303' (verified live: "Dapagliflozin in Patients with Heart Failure and Reduced Ejection Fraction", 2019).
  [!] [ERROR] YEAR_MISMATCH: Cited year 2021 mismatches official publication year by 2 years (published_print=2019 / published_online=2019).
      Expected : cited=2021
      Actual   : published_print=2019 / published_online=2019
      Hint     : A 2-year discrepancy is not an epub-ahead-of-print offset — check for typo or wrong paper.
      Fix      : Confirm the DOI/PMID matches the intended paper, then correct the year to 2019.
  [!] [ERROR] JOURNAL_MISMATCH: Cited journal "The Lancet" does not match official journal (full="The New England journal of medicine" / abbrev="N Engl J Med"); similarity=0.27.
      Expected : cited="The Lancet"
      Actual   : full="The New England journal of medicine" / abbrev="N Engl J Med"
      Hint     : Low journal similarity — either wrong paper/DOI or non-standard journal name.
      Fix      : Replace journal with "N Engl J Med" (official: "The New England journal of medicine").
  [*] [WARNING] AUTHOR_MISMATCH: Cited author "Solomon" is author #2 ("Solomon SD"), NOT first author ("McMurray JJV").
      Expected : cited_author="Solomon"
      Actual   : actual_first_author="McMurray JJV"; full_authors=['McMurray JJV', 'Solomon SD', 'Inzucchi SE', 'Køber L', 'Kosiborod MN', 'Martinez FA', 'Ponikowski P', 'Sabatine MS', 'Anand IS', 'Bělohlávek J', '... (39 total)']
      Hint     : "Solomon" is on the paper as author #2, not the first author.
      Fix      : Replace cited first author with "McMurray JJV" (or cite "McMurray et al.").
  [*] [WARNING] CLAIM_NUMBER_NOT_IN_ABSTRACT: Claim contains quantitative value(s) NOT found in the official abstract: 95% CI 0.50-0.70, HR 0.60.
      Expected : claim_numbers=['p<0.001', '95% CI 0.50-0.70', 'HR 0.60', '40%', '10 mg']
      Actual   : matched=['p<0.001', '40%', '10 mg']; missing=['95% CI 0.50-0.70', 'HR 0.60']; abstract_numbers=['P<0.001', '95% confidence interval [CI], 0.65 to 0.85', '95% CI, 0.59 to 0.83)', '95% CI, 0.69 to 0.98)', '95% CI, 0.71 to 0.97)', 'hazard ratio, 0.74', 'hazard ratio, 0.70', 'hazard ratio, 0.82', 'hazard ratio, 0.83', '40%', '16.3%', '21.2%', '10.0%', '13.7%', '9.6%']
      Hint     : Either the claim misstates/hallucinates the paper's numbers, expresses a derived calculation (e.g. RRR from HR), or cites subgroup/secondary results found only in the full text/tables.
      Fix      : Compare the claim word-for-word against the printed OFFICIAL ABSTRACT below (and run with --fulltext or inspect the PDF if needed), then assign verdict: SUPPORTED / PARTIAL / CONTRADICTED / NEEDS_FULLTEXT.
------------------------------------------------------------------------------
CLAIM_CHECK:
  Claim            : Dapagliflozin 10 mg reduced the primary outcome by 40% (HR 0.60, 95% CI 0.50-0.70, p<0.001).
  Numbers extracted: ['p<0.001', '95% CI 0.50-0.70', 'HR 0.60', '40%', '10 mg']
  Numbers matched  : ['p<0.001', '40%', '10 mg']
  Numbers MISSING  : ['95% CI 0.50-0.70', 'HR 0.60']
  Matched Abstract Sentences:
    > RESULTS: Over a median of 18.2 months, the primary outcome occurred in 386 of 2373 patients (16.3%) in the dapagliflozin group and in 502 of 2371 patients (21.2%) in the placebo group (hazard ratio, 0.74; 95% confidence interval [CI], 0.65 to 0.85; P<0.001).
```

## Optional NCBI API key

PubMed E-utilities permits 3 requests/second without an API key and 10 requests/second with one. `ref-verify` paces requests automatically and picks up `NCBI_API_KEY` when set:

```bash
export NCBI_API_KEY="your-ncbi-api-key"
```

## Development

Run the local lint, format, and offline unit test gate:

```bash
./scripts/check.sh
```

## License

MIT — see [LICENSE](LICENSE).
