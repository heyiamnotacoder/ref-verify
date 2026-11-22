"""Claim evaluation: grounds a claim's numbers and cohort against the abstract and OA full text."""

from __future__ import annotations

from ref_verify.cohort import check_claim_cohort
from ref_verify.http_client import HttpClient
from ref_verify.models import ClaimReport, Diagnostic, FulltextResult, PaperRecord
from ref_verify.quant import (
    extract_abstract_numbers,
    extract_claim_keywords,
    extract_claim_numbers,
    find_top_matching_sentences,
    normalize_for_quant,
    quant_token_in_norm_text,
    split_sentences,
)
from ref_verify.sources.fulltext import fetch_oa_fulltext_paragraphs
from ref_verify.text import normalize_text


def evaluate_claim(
    claim: str,
    rec: PaperRecord,
    http: HttpClient | None = None,
    fetch_fulltext: bool = False,
    prefetched_fulltext: FulltextResult | None = None,
) -> tuple[ClaimReport, list[Diagnostic]]:
    report = ClaimReport(
        claim=claim,
        extracted_numbers=extract_claim_numbers(claim),
        extracted_keywords=extract_claim_keywords(claim),
        abstract_available=bool(rec.abstract),
    )
    diags: list[Diagnostic] = []
    ft_paragraphs: list[str] = []

    if not rec.abstract:
        diags.append(
            Diagnostic(
                code="ABSTRACT_UNAVAILABLE",
                severity="WARNING",
                message="No abstract returned by PubMed, Europe PMC, or Crossref — cannot ground claim against abstract.",
                expected="Abstract text from authoritative registry",
                actual="abstract=None",
                hint="Some letters, editorials, older articles, or publisher-restricted works omit abstracts in API metadata.",
                fix="Inspect the full-text PDF directly (or re-run with --fulltext if PMCID is Open Access) and mark claim NEEDS_FULLTEXT until verified.",
            )
        )
    else:
        report.abstract_numbers = extract_abstract_numbers(rec.abstract)
        abs_low, abs_no_commas = normalize_for_quant(rec.abstract)
        for tok in report.extracted_numbers:
            if quant_token_in_norm_text(tok, abs_low, abs_no_commas):
                report.matched_numbers.append(tok)
            else:
                report.missing_numbers.append(tok)

        for kw in report.extracted_keywords:
            if normalize_text(kw).lower() in abs_low:
                report.matched_keywords.append(kw)

        report.matched_sentences = find_top_matching_sentences(
            split_sentences(rec.abstract), report.extracted_numbers, report.extracted_keywords, max_items=4
        )

    # Optional OA full-text check (NCBI PMC with Europe PMC fallback)
    if fetch_fulltext and (http is not None or prefetched_fulltext is not None):
        if not rec.pmcid:
            report.fulltext_status = "unavailable: no PMCID linked to this paper"
            diags.append(
                Diagnostic(
                    code="FULLTEXT_UNAVAILABLE",
                    severity="INFO",
                    message="OA full text requested (--fulltext), but no PMCID is linked to this paper.",
                    expected="PMCID with Open Access JATS full-text XML (NCBI PMC / Europe PMC)",
                    actual="pmcid=None",
                    hint="Paper is likely not deposited in PubMed Central.",
                    fix="Verify non-abstract numbers against the publisher PDF.",
                )
            )
        else:
            ft_res = (
                prefetched_fulltext
                if prefetched_fulltext is not None
                else fetch_oa_fulltext_paragraphs(http, rec.pmcid, is_open_access=rec.is_open_access)  # type: ignore[arg-type]
            )
            if ft_res.error or not ft_res.paragraphs:
                err_text = ft_res.error or "empty body"
                report.fulltext_status = f"unavailable: {err_text}"
                hint = (
                    "Transient network timeout or upstream 5xx error fetching OA full text — retry or increase --timeout."
                    if ft_res.is_transient
                    else "Article may be embargoed or not licensed for OA full-text XML distribution."
                )
                diags.append(
                    Diagnostic(
                        code="FULLTEXT_UNAVAILABLE",
                        severity="INFO",
                        message=f"OA full-text XML unavailable for {rec.pmcid}: {err_text}.",
                        expected=f"Open Access fullTextXML for {rec.pmcid}",
                        actual=err_text,
                        hint=hint,
                        fix="Verify non-abstract numbers against the publisher PDF (or re-run with a larger --timeout if transient).",
                    )
                )
            else:
                ft_paragraphs = list(ft_res.paragraphs)
                src_tag = f" via {ft_res.source}" if ft_res.source else ""
                report.fulltext_status = f"fetched ({len(ft_res.paragraphs)} paragraphs from {rec.pmcid}{src_tag})"
                if report.missing_numbers:
                    full_low, full_no_commas = normalize_for_quant("\n".join(ft_res.paragraphs))
                    still_missing: list[str] = []
                    for tok in report.missing_numbers:
                        if quant_token_in_norm_text(tok, full_low, full_no_commas):
                            report.matched_numbers.append(f"{tok} [fulltext]")
                        else:
                            still_missing.append(tok)
                    report.missing_numbers = still_missing
                report.fulltext_excerpts = find_top_matching_sentences(
                    ft_res.paragraphs, report.extracted_numbers, report.extracted_keywords, max_items=4
                )

    if report.abstract_available and report.missing_numbers:
        diags.append(
            Diagnostic(
                code="CLAIM_NUMBER_NOT_IN_ABSTRACT",
                severity="WARNING",
                message=(
                    f"Claim contains quantitative value(s) NOT found in the official abstract: "
                    f"{', '.join(report.missing_numbers)}."
                ),
                expected=f"claim_numbers={report.extracted_numbers}",
                actual=f"matched={report.matched_numbers}; missing={report.missing_numbers}; abstract_numbers={report.abstract_numbers[:15]}",
                details={
                    "missing_numbers": report.missing_numbers,
                    "matched_numbers": report.matched_numbers,
                    "abstract_numbers": report.abstract_numbers[:20],
                },
                hint=(
                    "Either the claim misstates/hallucinates the paper's numbers, expresses a derived calculation "
                    "(e.g. RRR from HR), or cites subgroup/secondary results found only in the full text/tables."
                ),
                fix=(
                    "Compare the claim word-for-word against the printed OFFICIAL ABSTRACT below "
                    "(and run with --fulltext or inspect the PDF if needed), then assign verdict: "
                    "SUPPORTED / PARTIAL / CONTRADICTED / NEEDS_FULLTEXT."
                ),
            )
        )

    cohort_diag = check_claim_cohort(claim, rec, report.matched_numbers, fulltext_paragraphs=ft_paragraphs)
    if cohort_diag is not None:
        diags.append(cohort_diag)

    return report, diags
