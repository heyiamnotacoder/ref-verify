"""Per-citation checklist: one verdict cell per check, derived purely from a ``VerificationResult``.

A cell is ``skip`` whenever the check never ran (no cited value, or no official value to compare), so a
human-facing tick always means "compared and matched" — never "not looked at".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ref_verify.metadata import checked_metadata_fields
from ref_verify.models import Diagnostic, UnlinkedEntry, VerificationResult
from ref_verify.scan import unlinked_entry_diagnostic

PASS = "pass"
INFO = "info"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"

UNVERIFIED = "UNVERIFIED"  # row status for a bibliography entry that carries no DOI/PMID

VERDICT_LABELS = {
    PASS: "pass",
    WARN: "warning",
    FAIL: "fail",
    INFO: "note",
    SKIP: "not checked (no value to compare, or the paper did not resolve)",
}

_SEVERITY_VERDICT = {"CRITICAL": FAIL, "ERROR": FAIL, "WARNING": WARN, "INFO": INFO}
_STATUS_VERDICT = {"PASS": PASS, "WARN": WARN, "FAIL": FAIL, UNVERIFIED: WARN}
_STATUS_ORDER = ("PASS", "WARN", "FAIL", UNVERIFIED)
_VERDICT_RANK = {PASS: 0, INFO: 1, WARN: 2, FAIL: 3}


def severity_verdict(severity: str) -> str:
    """Verdict a diagnostic severity maps to; unknown severities fail closed."""
    return _SEVERITY_VERDICT.get(severity, FAIL)


def status_verdict(status: str) -> str:
    """Verdict colour for a row status; unknown statuses fail closed."""
    return _STATUS_VERDICT.get(status, FAIL)


@dataclass(frozen=True)
class CheckSpec:
    key: str
    header: str
    description: str
    codes: frozenset[str]


CHECKS: tuple[CheckSpec, ...] = (
    CheckSpec(
        "identifier",
        "DOI/PMID",
        "DOI/PMID is well-formed, resolves, and both point to the same paper",
        frozenset(
            {
                "DOI_SYNTAX_INVALID",
                "PMID_SYNTAX_INVALID",
                "DOI_NOT_FOUND",
                "PMID_NOT_FOUND",
                "PMID_DOI_CONFLICT",
                "NO_IDENTIFIER_PROVIDED",
                "LOOKUP_ERROR",
                "UNLINKED_BIBLIOGRAPHY_ENTRIES",
                "DUPLICATE_REFERENCE",
            }
        ),
    ),
    CheckSpec(
        "integrity",
        "Retraction",
        "Not retracted; no expression of concern, retraction notice, or correction",
        frozenset({"RETRACTED_PAPER", "RETRACTION_NOTICE", "EXPRESSION_OF_CONCERN", "HAS_CORRECTION_NOTICE"}),
    ),
    CheckSpec("title", "Title", "Cited title matches the official title", frozenset({"TITLE_MISMATCH"})),
    CheckSpec("author", "Author", "Cited first author is the official first author", frozenset({"AUTHOR_MISMATCH"})),
    CheckSpec("year", "Year", "Cited year matches the print/online year", frozenset({"YEAR_MISMATCH"})),
    CheckSpec("journal", "Journal", "Cited journal matches the official journal", frozenset({"JOURNAL_MISMATCH"})),
    CheckSpec(
        "claim",
        "Claim",
        "Numbers and cohort in the claim are found in the abstract / OA full text",
        frozenset(
            {"CLAIM_NUMBER_NOT_IN_ABSTRACT", "CLAIM_COHORT_MISMATCH", "ABSTRACT_UNAVAILABLE", "FULLTEXT_UNAVAILABLE"}
        ),
    ),
)

_SPEC_BY_CODE = {code: spec for spec in CHECKS for code in spec.codes}


@dataclass(frozen=True)
class CheckCell:
    verdict: str
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True)
class ChecklistRow:
    index: int
    status: str  # PASS | WARN | FAIL | UNVERIFIED
    identifier: str  # DOI, PMID:n, or the entry text for unlinked rows
    paper: str  # e.g. "McMurray 2019" once resolved, else ""
    location: str
    cells: Mapping[str, CheckCell]
    # Codes no column claims are listed under "Other", never dropped (the code-coverage test keeps this empty).
    extra: tuple[Diagnostic, ...] = ()
    doi: str | None = None
    pmid: str | None = None
    result: VerificationResult | None = None
    unlinked: UnlinkedEntry | None = None
    reference_text: str = field(default="")

    def problems(self) -> list[tuple[CheckSpec | None, Diagnostic]]:
        """Every non-passing diagnostic in column order, then any unmapped ones."""
        out: list[tuple[CheckSpec | None, Diagnostic]] = [
            (spec, d) for spec in CHECKS for d in self.cells[spec.key].diagnostics
        ]
        out.extend((None, d) for d in self.extra)
        return out


def _worst(diags: Sequence[Diagnostic]) -> str:
    return max((severity_verdict(d.severity) for d in diags), key=_VERDICT_RANK.__getitem__)


def _applicable_checks(res: VerificationResult) -> set[str]:
    applicable = {"identifier"}
    if res.resolved is not None:
        applicable.add("integrity")
        applicable |= checked_metadata_fields(res.input, res.resolved)
        # A claim is only "checked" when it carried numbers to look up; wording alone is never ticked.
        reports = ([res.claim_report] if res.claim_report else []) + [c.report for c in res.sentence_checks]
        if any(r.extracted_numbers for r in reports):
            applicable.add("claim")
    return applicable


def _identifier_label(res: VerificationResult) -> str:
    cit = res.input
    if cit.doi:
        return cit.doi.strip()
    if cit.pmid:
        return f"PMID:{cit.pmid.strip()}"
    return cit.title or cit.label or "citation"


def _paper_label(res: VerificationResult) -> str:
    rec = res.resolved
    if rec is None:
        return ""
    first = rec.authors[0] if rec.authors else None
    name = (first.collective or first.family) if first else ""
    year = rec.best_year()
    return " ".join(p for p in (name, str(year) if year else "") if p)


def build_row(index: int, res: VerificationResult) -> ChecklistRow:
    by_key: dict[str, list[Diagnostic]] = {spec.key: [] for spec in CHECKS}
    extra: list[Diagnostic] = []
    for d in res.diagnostics:
        spec = _SPEC_BY_CODE.get(d.code)
        if spec is None:
            extra.append(d)
        else:
            by_key[spec.key].append(d)

    applicable = _applicable_checks(res)
    cells: dict[str, CheckCell] = {}
    for spec in CHECKS:
        diags = tuple(by_key[spec.key])
        if diags:
            cells[spec.key] = CheckCell(_worst(diags), diags)
        elif spec.key == "identifier" and res.resolved is None:
            cells[spec.key] = CheckCell(FAIL)  # fail-closed: nothing resolved, whatever the reason
        else:
            cells[spec.key] = CheckCell(PASS if spec.key in applicable else SKIP)

    return ChecklistRow(
        index=index,
        status=res.status,
        identifier=_identifier_label(res),
        paper=_paper_label(res),
        location=res.input.location or res.input.label or "",
        cells=cells,
        extra=tuple(extra),
        doi=res.input.doi,
        pmid=res.input.pmid,
        result=res,
        reference_text=res.input.reference_text or "",
    )


def build_unlinked_row(index: int, entry: UnlinkedEntry) -> ChecklistRow:
    diag = unlinked_entry_diagnostic(entry)
    cells = {spec.key: CheckCell(SKIP) for spec in CHECKS}
    cells["identifier"] = CheckCell(WARN, (diag,))
    return ChecklistRow(
        index=index,
        status=UNVERIFIED,
        identifier=entry.text,
        paper="",
        location=entry.location,
        cells=cells,
        unlinked=entry,
        reference_text=entry.text,
    )


def build_checklist(
    results: Sequence[VerificationResult], unlinked: Sequence[UnlinkedEntry] = ()
) -> list[ChecklistRow]:
    """Verified citations first (document order), then unlinked bibliography entries, numbered continuously."""
    rows = [build_row(i, res) for i, res in enumerate(results, start=1)]
    rows.extend(build_unlinked_row(len(rows) + i, e) for i, e in enumerate(unlinked, start=1))
    return rows


def status_counts(rows: Sequence[ChecklistRow]) -> list[tuple[str, int]]:
    """Non-zero row counts per status, in PASS, WARN, FAIL, UNVERIFIED order."""
    statuses = [r.status for r in rows]
    return [(s, statuses.count(s)) for s in _STATUS_ORDER if s in statuses]
