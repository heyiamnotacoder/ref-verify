"""Grounding the document's own citing sentences against the papers they cite.

Pure: ``ground_sentence`` checks one sentence against one paper; ``reconcile_sentence_checks`` then
looks across every paper a sentence cites, so a number counts as found when *any* co-cited paper
reports it, and turns the outcome into diagnostics on each cited paper's result.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace

from ref_verify.claims import evaluate_claim
from ref_verify.models import (
    SEVERITY_ORDER,
    CitingSentence,
    Diagnostic,
    FulltextResult,
    PaperRecord,
    SentenceCheck,
    VerificationResult,
    status_for,
)
from ref_verify.quant import extract_claim_numbers
from ref_verify.text import truncate

_BARE_INT_RE = re.compile(r"^\d+$")
_PERCENT_RE = re.compile(r"^(\d{1,2}(?:\.\d+)?)\s*%$")
_RATIO_RE = re.compile(
    r"\b(hazard ratio|HR|relative risk|risk ratio|RR|rate ratio|odds ratio|OR)\b[^0-9;.]{0,25}?(0?\.\d+)",
    re.IGNORECASE,
)
# evaluate_claim diagnostics that describe the paper (once per result), not one sentence.
_PER_PAPER_CODES = ("ABSTRACT_UNAVAILABLE", "FULLTEXT_UNAVAILABLE")


def sentence_claim_numbers(claim: str) -> list[str]:
    """Quantitative tokens worth grounding in running text: drops bare years and bare single digits.

    "In 2019, type 2 diabetes ..." would otherwise demand that "2019" and "2" appear in the abstract.
    """
    out: list[str] = []
    for tok in extract_claim_numbers(claim):
        if _BARE_INT_RE.match(tok):
            value = int(tok)
            if value < 10 or 1900 <= value <= 2100:
                continue
        if tok.count(")") > tok.count("("):  # "95% CI 0.65-0.85)" picked up with the closing bracket
            tok = tok.rstrip(")").rstrip()
        out.append(tok)
    return out


def _derived_reduction(token: str, source_text: str) -> str | None:
    """'26%' is a relative reduction derived from 'hazard ratio was 0.74' (1 - 0.74 = 0.26): name that ratio."""
    m = _PERCENT_RE.match(token)
    if not m:
        return None
    pct = float(m.group(1))
    for r in _RATIO_RE.finditer(source_text):
        if abs((1 - float(r.group(2))) * 100 - pct) <= 0.6:
            return f"{r.group(1)} {r.group(2)}"
    return None


def ground_sentence(
    sentence: CitingSentence,
    rec: PaperRecord,
    fulltext_requested: bool = False,
    fulltext: FulltextResult | None = None,
) -> SentenceCheck:
    report, diags = evaluate_claim(
        sentence.claim,
        rec,
        fulltext_requested=fulltext_requested,
        fulltext=fulltext,
        claim_numbers=sentence_claim_numbers(sentence.claim),
    )
    source_text = "\n".join([rec.abstract or "", *(fulltext.paragraphs if fulltext else [])])
    still_missing: list[str] = []
    for tok in report.missing_numbers:
        ratio = _derived_reduction(tok, source_text)
        if ratio:
            report.matched_numbers.append(f"{tok} [derived: 1 - {ratio}]")
        else:
            still_missing.append(tok)
    report.missing_numbers = still_missing
    return SentenceCheck(sentence=sentence, report=report, diagnostics=diags)


def _sentence_id(s: CitingSentence) -> tuple[str, str]:
    return (s.location, s.text)


def _quote(s: CitingSentence) -> str:
    return f'{s.location}: "{truncate(s.claim, 140)}"'


def _missing_number_diagnostic(check: SentenceCheck, missing: list[str], co_cited: int, unread: int) -> Diagnostic:
    """``unread``: cited references whose text could not be checked (no DOI/PMID, or did not resolve)."""
    s = check.sentence
    uncheckable = unread > 0
    scope = "the cited paper's abstract"
    if co_cited > 1:
        scope = f"any of the {co_cited} cited papers' abstracts"
    if check.report.fulltext_status.startswith("fetched"):
        scope += " or OA full text"
    message = f"{_quote(s)} — {', '.join(missing)} not found in {scope}."
    if uncheckable:
        message += f" {unread} other cited reference(s) could not be read (no DOI/PMID, or not found)."
    return Diagnostic(
        code="CLAIM_NUMBER_NOT_IN_ABSTRACT",
        severity="INFO" if uncheckable else "WARNING",
        message=message,
        expected=f"sentence_numbers={check.report.extracted_numbers}",
        actual=f"missing={missing}; abstract_numbers={check.report.abstract_numbers[:15]}",
        details={
            "location": s.location,
            "sentence": s.text,
            "missing_numbers": missing,
            "co_cited": co_cited,
            "unread_refs": unread,
        },
        hint=(
            "The number may come from a co-cited reference that could not be read; check that reference by hand."
            if uncheckable
            else "The sentence may misquote the paper, state a derived value (e.g. RRR from HR), "
            "or cite a figure found only in the full text or tables."
        ),
        fix="Compare the sentence with the paper's abstract (re-run with --fulltext for OA papers) and correct the number or the citation.",
    )


def _sentence_diagnostics(
    check: SentenceCheck, by_sentence: dict[tuple[str, str], list[SentenceCheck]]
) -> list[Diagnostic]:
    s = check.sentence
    co_cited = len(s.cited_keys)
    diags: list[Diagnostic] = []
    if check.missing_after_cocitation:
        grounded = [c for c in by_sentence[_sentence_id(s)] if c.report.abstract_available]
        unread = s.uncheckable_refs + (co_cited - len(grounded))
        diags.append(
            _missing_number_diagnostic(check, check.missing_after_cocitation, co_cited + s.uncheckable_refs, unread)
        )
    diags.extend(_cohort_diagnostics(check, by_sentence[_sentence_id(s)]))
    return diags


def _has_cohort_mismatch(check: SentenceCheck) -> bool:
    return any(d.code == "CLAIM_COHORT_MISMATCH" for d in check.diagnostics)


def _cohort_diagnostics(check: SentenceCheck, peers: Sequence[SentenceCheck]) -> list[Diagnostic]:
    """A population mismatch stands only if every read paper the sentence cites disagrees with it.

    Reported as a note: a sentence lifted from running text often leaves its population implicit, and the
    subgroup heuristics in ``cohort`` are tuned for deliberate ``--claim`` text.
    """
    read = [c for c in peers if c.report.abstract_available]
    if not _has_cohort_mismatch(check) or not all(_has_cohort_mismatch(c) for c in read):
        return []
    s = check.sentence
    unread = s.uncheckable_refs + (len(s.cited_keys) - len(read))
    out = []
    for d in check.diagnostics:
        if d.code == "CLAIM_COHORT_MISMATCH":
            out.append(
                replace(
                    d,
                    severity="INFO",
                    message=f"{_quote(s)} — {d.message}",
                    details={**d.details, "location": s.location, "unread_refs": unread},
                )
            )
    return out


def _missing_everywhere(check: SentenceCheck, peers: Sequence[SentenceCheck]) -> list[str]:
    """Numbers this paper lacks that no other *read* co-cited paper reports either."""
    if not check.report.abstract_available:
        return []
    grounded = [c for c in peers if c.report.abstract_available]  # an unread paper cannot clear a number
    return [n for n in check.report.missing_numbers if all(n in c.report.missing_numbers for c in grounded)]


def reconcile_sentence_checks(results: Sequence[VerificationResult]) -> list[VerificationResult]:
    """Turn per-paper sentence checks into diagnostics, counting a number as found in any co-cited paper."""
    by_sentence: dict[tuple[str, str], list[SentenceCheck]] = {}
    for res in results:
        for check in res.sentence_checks:
            by_sentence.setdefault(_sentence_id(check.sentence), []).append(check)

    out: list[VerificationResult] = []
    for res in results:
        if not res.sentence_checks:
            out.append(res)
            continue
        checks = [
            replace(c, missing_after_cocitation=_missing_everywhere(c, by_sentence[_sentence_id(c.sentence)]))
            for c in res.sentence_checks
        ]
        added: list[Diagnostic] = []
        seen_codes = {d.code for d in res.diagnostics}
        for check in checks:
            added.extend(_sentence_diagnostics(check, by_sentence))
            if not check.report.extracted_numbers:
                continue  # nothing was looked up, so a missing abstract/full text changes nothing
            for d in check.diagnostics:
                if d.code in _PER_PAPER_CODES and d.code not in seen_codes:
                    seen_codes.add(d.code)
                    added.append(d)
        diagnostics = sorted([*res.diagnostics, *added], key=lambda d: SEVERITY_ORDER.get(d.severity, 99))
        out.append(replace(res, diagnostics=diagnostics, status=status_for(diagnostics), sentence_checks=checks))
    return out
