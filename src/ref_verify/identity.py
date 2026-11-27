"""Pure identity diagnostics: whether cited DOIs/PMIDs resolve, and whether they name the same paper."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ref_verify.models import CitationInput, Diagnostic, HttpResponse, PaperRecord
from ref_verify.text import title_similarity

Candidates = Sequence[dict[str, Any]]


def format_candidates_inline(did_you_mean: Candidates) -> str:
    """One-line candidate list for the NO_IDENTIFIER_PROVIDED fix text."""
    if not did_you_mean:
        return "no matching papers found"
    return "; ".join(
        f"DOI:{s['doi'] or 'N/A'} | PMID:{s['pmid'] or 'N/A'} ('{s['title']}', {s['year']}, sim={s['title_similarity']})"
        for s in did_you_mean
    )


def did_you_mean_fix(top: dict[str, Any], *, with_pmid: bool) -> str:
    ids = f"PMID '{top['pmid'] or 'N/A'}' / DOI '{top['doi'] or 'N/A'}'" if with_pmid else f"DOI '{top['doi']}'"
    return (
        f"DID_YOU_MEAN: {ids} "
        f'("{top["title"]}", {top["first_author"] or "unknown"}, {top["year"]}, '
        f"title_similarity={top['title_similarity']})?"
    )


def no_identifier_diagnostic(title: str | None, did_you_mean: Candidates) -> Diagnostic:
    if title:
        return Diagnostic(
            code="NO_IDENTIFIER_PROVIDED",
            severity="WARNING",
            message=(
                "No valid DOI or PMID provided; searched Crossref/PubMed by title, but no single paper matched "
                "the cited title, first author, and year."
            ),
            expected="A valid DOI/PMID, or a cited title + first author/year that identify one indexed paper",
            actual=f'title="{title}"',
            details={"did_you_mean": list(did_you_mean)},
            hint="The reference may be misquoted, not indexed (book, report, website), or fabricated.",
            fix=f"Check the reference and add its DOI or PMID. Candidates: {format_candidates_inline(did_you_mean)}",
        )
    return Diagnostic(
        code="NO_IDENTIFIER_PROVIDED",
        severity="ERROR",
        message="No DOI, PMID, or title supplied to verify.",
        expected="At least one of --doi, --pmid, or --title",
        actual="empty citation input",
        hint="Nothing to look up.",
        fix="Provide --doi and/or --pmid (plus optional --title, --author, --year, --journal, --claim).",
    )


def resolved_by_search_diagnostic(match: dict[str, Any]) -> Diagnostic:
    """INFO: the cited reference had no DOI/PMID, and search identified the paper the checks then ran against."""
    doi, pmid = match.get("doi"), match.get("pmid")
    found = " / ".join(p for p in (f"DOI {doi}" if doi else "", f"PMID {pmid}" if pmid else "") if p)
    add = f"doi:{doi}" if doi else f"PMID: {pmid}"
    return Diagnostic(
        code="IDENTIFIER_RESOLVED_BY_SEARCH",
        severity="INFO",
        message=f"No DOI or PMID was cited; identified the paper by title, first author, and year as {found}.",
        expected="A DOI or PMID in the reference (optional for most journals)",
        actual=(
            f'{found} ("{match.get("title")}", {match.get("first_author") or "unknown"}, {match.get("year")}, '
            f"title_similarity={match.get('title_similarity')})"
        ),
        details={"match": dict(match)},
        hint="The identifier was found by search; every other check ran against that paper.",
        fix=f"Optionally add {add} to the reference so readers can locate it.",
    )


def doi_lookup_down(cr_resp: HttpResponse | None, epmc_resp: HttpResponse | None) -> bool:
    """True when both DOI sources failed at the network level (not a genuine miss)."""
    return bool(cr_resp and cr_resp.status == 0 and epmc_resp and epmc_resp.status == 0)


def pmid_lookup_down(pm_resp: HttpResponse | None) -> bool:
    return bool(pm_resp and pm_resp.status == 0)


def doi_not_found_diagnostic(
    doi: str,
    cr_resp: HttpResponse | None,
    epmc_resp: HttpResponse | None,
    did_you_mean: Candidates,
) -> Diagnostic:
    cr_err = cr_resp.error if cr_resp and cr_resp.error else f"HTTP {cr_resp.status if cr_resp else '?'}"
    epmc_err = (
        epmc_resp.error
        if epmc_resp and epmc_resp.error
        else f"HTTP {epmc_resp.status if epmc_resp else '?'} (0 results)"
    )
    if doi_lookup_down(cr_resp, epmc_resp):
        return Diagnostic(
            code="LOOKUP_ERROR",
            severity="ERROR",
            message=f"Network error while looking up DOI '{doi}' (Crossref: {cr_err}; Europe PMC: {epmc_err}).",
            expected="HTTP 200 from Crossref or Europe PMC",
            actual=f"Crossref={cr_err}; EuropePMC={epmc_err}",
            hint="Transient network failure, firewall block, or timeout.",
            fix="Check network connectivity and retry with a larger --timeout.",
        )
    return Diagnostic(
        code="DOI_NOT_FOUND",
        severity="ERROR",
        message=f"DOI '{doi}' does NOT exist in Crossref ({cr_err}) or Europe PMC ({epmc_err}).",
        expected=f"Resolvable DOI '{doi}'",
        actual=f"Crossref: {cr_err}; Europe PMC: {epmc_err}",
        details={"did_you_mean": list(did_you_mean)},
        hint="Hallucinated, typo'd, or unregistered DOI.",
        fix=did_you_mean_fix(did_you_mean[0], with_pmid=False)
        if did_you_mean
        else f"Remove or replace nonexistent DOI '{doi}'.",
    )


def pmid_not_found_diagnostic(pmid: str, pm_resp: HttpResponse | None, did_you_mean: Candidates) -> Diagnostic:
    pm_err = pm_resp.error if pm_resp and pm_resp.error else "0 PubmedArticle records returned"
    if pmid_lookup_down(pm_resp):
        return Diagnostic(
            code="LOOKUP_ERROR",
            severity="ERROR",
            message=f"Network error while looking up PMID '{pmid}' ({pm_err}).",
            expected="HTTP 200 from PubMed E-utilities",
            actual=pm_err,
            hint="Transient NCBI network error or timeout.",
            fix="Retry with --timeout or set NCBI_API_KEY.",
        )
    return Diagnostic(
        code="PMID_NOT_FOUND",
        severity="ERROR",
        message=f"PMID '{pmid}' does NOT exist in PubMed ({pm_err}) or Europe PMC.",
        expected=f"Resolvable PMID '{pmid}'",
        actual=f"PubMed: {pm_err}; Europe PMC: 0 results",
        details={"did_you_mean": list(did_you_mean)},
        hint="Hallucinated, typo'd, or invalid PubMed ID.",
        fix=did_you_mean_fix(did_you_mean[0], with_pmid=True)
        if did_you_mean
        else f"Remove or replace nonexistent PMID '{pmid}'.",
    )


def enrich_doi_syntax_fix(diagnostics: list[Diagnostic], cited_doi: str, clean_doi: str, rec: PaperRecord) -> None:
    """Attach live proof that the cleaned DOI candidate resolved to the DOI syntax diagnostic."""
    for d in diagnostics:
        if d.code == "DOI_SYNTAX_INVALID":
            d.details["cleaned_candidate_resolved"] = {"doi": rec.doi, "title": rec.title, "year": rec.best_year()}
            d.fix = f"Replace '{cited_doi}' with '{clean_doi}' (verified live: \"{rec.title}\", {rec.best_year()})."


def duplicate_reference_diagnostic(cit: CitationInput) -> Diagnostic:
    """The same paper (DOI/PMID) appears as more than one entry in the reference list."""
    locations = [cit.location or "?", *cit.duplicate_locations]
    ident = cit.doi or f"PMID:{cit.pmid}"
    return Diagnostic(
        code="DUPLICATE_REFERENCE",
        severity="WARNING",
        message=f"{ident} is listed {len(locations)} times in the reference list ({', '.join(locations)}).",
        expected="Each cited paper appears once in the reference list",
        actual=f"same paper at {', '.join(locations)}",
        details={"locations": locations},
        hint="Duplicated references usually come from merging drafts or re-inserting a citation by hand.",
        fix=(
            f"Keep one entry ({locations[0]}), delete the other {len(locations) - 1}, and renumber the in-text "
            "citations that pointed to them."
        ),
    )


def id_conflict_diagnostic(doi: str, pmid: str, doi_rec: PaperRecord, pmid_rec: PaperRecord) -> Diagnostic | None:
    """CRITICAL diagnostic when the cited DOI and PMID resolve to different papers."""
    pmid_official_doi = (pmid_rec.doi or "").strip().lower()
    doi_official_pmid = (doi_rec.pmid or "").strip()
    doi_mismatch = bool(pmid_official_doi and pmid_official_doi != doi.lower())
    pmid_mismatch = bool(doi_official_pmid and doi_official_pmid != pmid)
    # Also a conflict when titles are completely different and neither record links the other
    title_clash = bool(
        not pmid_official_doi
        and not doi_official_pmid
        and doi_rec.title
        and pmid_rec.title
        and title_similarity(doi_rec.title, pmid_rec.title) < 0.55
    )
    if not (doi_mismatch or pmid_mismatch or title_clash):
        return None
    return Diagnostic(
        code="PMID_DOI_CONFLICT",
        severity="CRITICAL",
        message=(
            f"Cited PMID {pmid} resolves to DOI {pmid_rec.doi or 'NONE'} "
            f"('{pmid_rec.title}', {pmid_rec.best_year()}), "
            f"NOT cited DOI {doi} ('{doi_rec.title}', {doi_rec.best_year()})."
        ),
        expected=f"DOI {doi} and PMID {pmid} refer to the SAME paper",
        actual=(
            f"PMID {pmid} -> DOI {pmid_rec.doi or 'NONE'} ('{pmid_rec.title}', {pmid_rec.best_year()}); "
            f"DOI {doi} -> PMID {doi_rec.pmid or 'NONE'} ('{doi_rec.title}', {doi_rec.best_year()})"
        ),
        details={
            "pmid_record": {"pmid": pmid, "doi": pmid_rec.doi, "title": pmid_rec.title, "year": pmid_rec.best_year()},
            "doi_record": {"doi": doi, "pmid": doi_rec.pmid, "title": doi_rec.title, "year": doi_rec.best_year()},
        },
        hint="The cited DOI and PMID belong to two completely different publications.",
        fix="Check which paper you actually intended to cite and replace the mismatched PMID or DOI.",
    )
