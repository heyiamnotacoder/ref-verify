"""Verification orchestration: resolves identifiers across sources and assembles diagnostics."""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ref_verify.claims import evaluate_claim
from ref_verify.http_client import HttpClient, HttpResponse, is_transient_http
from ref_verify.identifiers import validate_doi, validate_pmid
from ref_verify.metadata import (
    check_author,
    check_journal,
    check_retraction_and_notices,
    check_title,
    check_year,
    merge_records,
)
from ref_verify.models import (
    SEVERITY_ORDER,
    CitationInput,
    ClaimReport,
    Diagnostic,
    FulltextResult,
    PaperRecord,
    VerificationResult,
)
from ref_verify.sources.crossref import fetch_crossref_doi, fetch_crossref_dois_batch, search_crossref_by_title
from ref_verify.sources.europepmc import fetch_epmc, fetch_epmc_batch
from ref_verify.sources.fulltext import fetch_oa_fulltext_batch, fetch_oa_fulltext_paragraphs
from ref_verify.sources.pubmed import (
    fetch_pubmed_pmid,
    fetch_pubmed_pmids_batch,
    pubmed_esearch_by_doi,
    pubmed_esearch_by_dois_batch,
    search_pubmed_by_title,
)
from ref_verify.text import normalize_text, title_similarity

DEFAULT_WORKERS = 8


@dataclass
class PrefetchCache:
    """In-memory batch prefetch results keyed by normalized DOI / PMID / PMCID."""

    crossref_by_doi: Dict[str, Tuple[Optional[PaperRecord], HttpResponse]] = field(default_factory=dict)
    epmc_by_doi: Dict[str, Tuple[Optional[PaperRecord], HttpResponse]] = field(default_factory=dict)
    epmc_by_pmid: Dict[str, Tuple[Optional[PaperRecord], HttpResponse]] = field(default_factory=dict)
    pubmed_by_pmid: Dict[str, Tuple[Optional[PaperRecord], HttpResponse]] = field(default_factory=dict)
    pubmed_by_doi: Dict[str, PaperRecord] = field(default_factory=dict)
    esearch_checked_dois: set[str] = field(default_factory=set)
    fulltext_by_pmcid: Dict[str, FulltextResult] = field(default_factory=dict)

    def store_crossref_dois(self, http: HttpClient, dois: Sequence[str], max_workers: int = 4) -> None:
        unique = list(dict.fromkeys(d for d in dois if d and d.lower() not in self.crossref_by_doi))
        if not unique:
            return
        if len(unique) == 1:
            rec, resp = fetch_crossref_doi(http, unique[0])
            self.crossref_by_doi[unique[0].lower()] = (rec, resp)
            return
        found, _ = fetch_crossref_dois_batch(http, unique, max_workers=max_workers)
        self.crossref_by_doi.update(found)

    def store_pubmed_pmids(self, http: HttpClient, pmids: Sequence[str]) -> None:
        unique = [p for p in dict.fromkeys(pmids) if p and p not in self.pubmed_by_pmid]
        if not unique:
            return
        found, _ = fetch_pubmed_pmids_batch(http, unique)
        self.pubmed_by_pmid.update(found)
        for rec, _ in found.values():
            if rec is not None and rec.doi:
                self.pubmed_by_doi[rec.doi.lower()] = rec

    def store_epmc_batch(
        self,
        http: HttpClient,
        dois: Sequence[str] = (),
        pmids: Sequence[str] = (),
        max_workers: int = 4,
        *,
        fallback: bool = True,
    ) -> List[str]:
        by_doi, by_pmid = fetch_epmc_batch(http, dois=dois, pmids=pmids, max_workers=max_workers, fallback=fallback)
        self.epmc_by_doi.update(by_doi)
        self.epmc_by_pmid.update(by_pmid)
        return [
            rec.pmid
            for rec, _ in by_doi.values()
            if rec is not None and rec.pmid and rec.pmid not in self.pubmed_by_pmid
        ]

    def crossref_record(self, doi: Optional[str]) -> Optional[PaperRecord]:
        if not doi:
            return None
        pair = self.crossref_by_doi.get(doi.lower())
        return pair[0] if pair else None

    def pubmed_record(self, doi: Optional[str], pmid: Optional[str]) -> Optional[PaperRecord]:
        if pmid and pmid in self.pubmed_by_pmid:
            return self.pubmed_by_pmid[pmid][0]
        if doi and not pmid:
            return self.pubmed_by_doi.get(doi.lower())
        return None

    def epmc_record(self, doi: Optional[str], pmid: Optional[str]) -> Optional[PaperRecord]:
        if doi and doi.lower() in self.epmc_by_doi:
            rec = self.epmc_by_doi[doi.lower()][0]
            if rec is not None:
                return rec
        if pmid and pmid in self.epmc_by_pmid:
            return self.epmc_by_pmid[pmid][0]
        return None

    def oa_flags_by_pmcid(self) -> Dict[str, Optional[bool]]:
        flags: Dict[str, Optional[bool]] = {}
        for store in (self.epmc_by_doi.values(), self.epmc_by_pmid.values()):
            for rec, _ in store:
                if rec is not None and rec.pmcid and rec.is_open_access is not None:
                    flags[rec.pmcid.strip().upper()] = rec.is_open_access
        return flags


def _run_tasks(tasks: Sequence[Any], workers: int) -> None:
    """Run zero-arg callables sequentially when workers == 1, or concurrently otherwise."""
    if not tasks:
        return
    if workers <= 1 or len(tasks) == 1:
        for fn in tasks:
            fn()
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(tasks))) as pool:
        futs = [pool.submit(fn) for fn in tasks]
        for f in futs:
            f.result()


def _search_did_you_mean(http: HttpClient, title: Optional[str], author: Optional[str] = None) -> List[Dict[str, Any]]:
    if not title:
        return []
    candidates: List[Dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_cr = pool.submit(search_crossref_by_title, http, title, author, 8)
        f_pm = pool.submit(search_pubmed_by_title, http, title, 5)
        candidates.extend(f_cr.result())
        candidates.extend(f_pm.result())

    # Merge duplicates by DOI (combining PubMed PMID + Crossref citation count when both return the same paper)
    merged_by_key: Dict[str, Dict[str, Any]] = {}
    for c in candidates:
        key = (c.get("doi") or "").lower() or normalize_text(c.get("title") or "").lower()
        existing = merged_by_key.get(key)
        if existing is None:
            merged_by_key[key] = dict(c)
        else:
            existing["pmid"] = existing.get("pmid") or c.get("pmid")
            existing["doi"] = existing.get("doi") or c.get("doi")
            existing["first_author"] = existing.get("first_author") or c.get("first_author")
            existing["year"] = existing.get("year") or c.get("year")
            existing["journal"] = existing.get("journal") or c.get("journal")
            existing["title_similarity"] = max(existing.get("title_similarity", 0), c.get("title_similarity", 0))
            existing["cited_by_count"] = max(existing.get("cited_by_count", 0), c.get("cited_by_count", 0))

    def _rank_key(x: Dict[str, Any]) -> Tuple[float, int, int, int]:
        sim_bucket = round(float(x.get("title_similarity", 0)), 2)
        has_author = 1 if x.get("first_author") else 0
        cited_by = int(x.get("cited_by_count", 0) or 0)
        has_pmid = 1 if x.get("pmid") else 0
        return (sim_bucket, has_author, cited_by, has_pmid)

    ranked = sorted(merged_by_key.values(), key=_rank_key, reverse=True)
    return ranked[:3]


def verify_citation(
    cit: CitationInput,
    http: HttpClient,
    prefetched: Optional[PrefetchCache] = None,
) -> VerificationResult:
    diagnostics: List[Diagnostic] = []
    did_you_mean: List[Dict[str, Any]] = []

    clean_doi: Optional[str] = None
    doi_had_syntax_error = False
    if cit.doi is not None and cit.doi != "":
        clean_doi, doi_diag = validate_doi(cit.doi)
        if doi_diag is not None:
            doi_had_syntax_error = True
            diagnostics.append(doi_diag)

    clean_pmid: Optional[str] = None
    if cit.pmid is not None and cit.pmid != "":
        clean_pmid, pmid_diag = validate_pmid(cit.pmid)
        if pmid_diag is not None:
            diagnostics.append(pmid_diag)

    # No identifier provided at all
    if not clean_doi and not clean_pmid:
        if cit.title:
            did_you_mean = _search_did_you_mean(http, cit.title, author=cit.author)
            sug_text = (
                "; ".join(
                    f"DOI:{s['doi'] or 'N/A'} | PMID:{s['pmid'] or 'N/A'} ('{s['title']}', {s['year']}, sim={s['title_similarity']})"
                    for s in did_you_mean
                )
                if did_you_mean
                else "no matching papers found"
            )
            diagnostics.append(
                Diagnostic(
                    code="NO_IDENTIFIER_PROVIDED",
                    severity="WARNING",
                    message="No valid DOI or PMID provided; searched Crossref/PubMed by title instead.",
                    expected="Valid --doi and/or --pmid",
                    actual=f'title="{cit.title}"',
                    details={"did_you_mean": did_you_mean},
                    hint="Exact verification requires a DOI or PMID.",
                    fix=f"Re-run ref-verify with the verified identifier. Candidates: {sug_text}",
                )
            )
        elif not diagnostics:
            diagnostics.append(
                Diagnostic(
                    code="NO_IDENTIFIER_PROVIDED",
                    severity="ERROR",
                    message="No DOI, PMID, or title supplied to verify.",
                    expected="At least one of --doi, --pmid, or --title",
                    actual="empty citation input",
                    hint="Nothing to look up.",
                    fix="Provide --doi and/or --pmid (plus optional --title, --author, --year, --journal, --claim).",
                )
            )
        return VerificationResult(
            input=cit,
            status=_compute_status(diagnostics),
            resolved=None,
            diagnostics=diagnostics,
            did_you_mean=did_you_mean,
        )

    doi_low = clean_doi.lower() if clean_doi else None
    cr_rec: Optional[PaperRecord] = None
    epmc_doi_rec: Optional[PaperRecord] = None
    cr_resp: Optional[HttpResponse] = None
    epmc_doi_resp: Optional[HttpResponse] = None
    pm_rec: Optional[PaperRecord] = None
    epmc_pmid_rec: Optional[PaperRecord] = None
    pm_resp: Optional[HttpResponse] = None
    prefetched_ft: Optional[FulltextResult] = None
    want_fulltext = bool(cit.claim and cit.fulltext)

    # Seed from batch PrefetchCache if available
    if prefetched is not None:
        if doi_low and doi_low in prefetched.crossref_by_doi:
            cr_rec, cr_resp = prefetched.crossref_by_doi[doi_low]
        if doi_low and doi_low in prefetched.epmc_by_doi:
            epmc_doi_rec, epmc_doi_resp = prefetched.epmc_by_doi[doi_low]
        if clean_pmid and clean_pmid in prefetched.pubmed_by_pmid:
            pm_rec, pm_resp = prefetched.pubmed_by_pmid[clean_pmid]
        if clean_pmid and clean_pmid in prefetched.epmc_by_pmid:
            epmc_pmid_rec, _ = prefetched.epmc_by_pmid[clean_pmid]
        if doi_low and not clean_pmid and pm_rec is None and doi_low in prefetched.pubmed_by_doi:
            pm_rec = prefetched.pubmed_by_doi[doi_low]

    # Parallel & pipelined live fetches (Crossref + PubMed first; Europe PMC as targeted fallback)
    need_cr = bool(clean_doi and cr_resp is None)
    need_pm = bool(clean_pmid and pm_resp is None)
    need_esearch_parallel = bool(
        clean_doi
        and not clean_pmid
        and pm_rec is None
        and (epmc_doi_rec is None or not epmc_doi_rec.pmid)
        and (prefetched is None or doi_low not in prefetched.esearch_checked_dois)
    )
    esearch_ran = False

    f_cr: Optional[concurrent.futures.Future[Tuple[Optional[PaperRecord], HttpResponse]]] = None
    f_pm: Optional[concurrent.futures.Future[Tuple[Optional[PaperRecord], HttpResponse]]] = None
    f_esearch: Optional[concurrent.futures.Future[Optional[str]]] = None
    f_pm_enrich: Optional[concurrent.futures.Future[Tuple[Optional[PaperRecord], HttpResponse]]] = None
    f_cr_enrich: Optional[concurrent.futures.Future[Tuple[Optional[PaperRecord], HttpResponse]]] = None
    f_ft_eager: Optional[concurrent.futures.Future[FulltextResult]] = None
    eager_ft_pmcid: Optional[str] = None

    if need_cr or need_pm or need_esearch_parallel:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            if need_cr and clean_doi:
                f_cr = pool.submit(fetch_crossref_doi, http, clean_doi)
            if need_pm and clean_pmid:
                f_pm = pool.submit(fetch_pubmed_pmid, http, clean_pmid)
            if need_esearch_parallel and clean_doi:
                esearch_ran = True
                f_esearch = pool.submit(pubmed_esearch_by_doi, http, clean_doi)

            def _maybe_start_eager_ft(rec: Optional[PaperRecord]) -> None:
                nonlocal prefetched_ft, f_ft_eager, eager_ft_pmcid
                if (
                    not want_fulltext
                    or rec is None
                    or not rec.pmcid
                    or f_ft_eager is not None
                    or prefetched_ft is not None
                ):
                    return
                pmcid_u = rec.pmcid.strip().upper()
                if prefetched is not None and pmcid_u in prefetched.fulltext_by_pmcid:
                    prefetched_ft = prefetched.fulltext_by_pmcid[pmcid_u]
                else:
                    eager_ft_pmcid = pmcid_u
                    f_ft_eager = pool.submit(
                        fetch_oa_fulltext_paragraphs,
                        http,
                        rec.pmcid,
                        rec.is_open_access,
                    )

            pending: set[concurrent.futures.Future[Any]] = {f for f in (f_cr, f_pm, f_esearch) if f is not None}
            while pending:
                done, pending = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
                for fut in done:
                    if fut is f_cr:
                        cr_rec, cr_resp = fut.result()
                    elif fut is f_esearch:
                        linked_pmid = fut.result()
                        if linked_pmid and f_pm_enrich is None and pm_rec is None:
                            if prefetched is not None and linked_pmid in prefetched.pubmed_by_pmid:
                                pm_rec, _ = prefetched.pubmed_by_pmid[linked_pmid]
                                _maybe_start_eager_ft(pm_rec)
                            else:
                                f_pm_enrich = pool.submit(fetch_pubmed_pmid, http, linked_pmid)
                                pending.add(f_pm_enrich)
                    elif fut is f_pm_enrich:
                        pm_extra, _ = fut.result()
                        if pm_extra is not None and (
                            not pm_extra.doi or not clean_doi or pm_extra.doi.lower() == clean_doi.lower()
                        ):
                            pm_rec = pm_extra
                            _maybe_start_eager_ft(pm_rec)
                    elif fut is f_pm:
                        pm_rec, pm_resp = fut.result()
                        # Pipeline PMID-only Crossref & OA fulltext immediately!
                        if clean_pmid and not clean_doi and pm_rec is not None:
                            if pm_rec.doi and f_cr_enrich is None and cr_rec is None:
                                if prefetched is not None and pm_rec.doi.lower() in prefetched.crossref_by_doi:
                                    cr_rec, _ = prefetched.crossref_by_doi[pm_rec.doi.lower()]
                                else:
                                    f_cr_enrich = pool.submit(fetch_crossref_doi, http, pm_rec.doi)
                                    pending.add(f_cr_enrich)
                            _maybe_start_eager_ft(pm_rec)
                    elif fut is f_cr_enrich:
                        cr_extra, _ = fut.result()
                        if cr_extra is not None:
                            cr_rec = cr_extra

            if f_ft_eager is not None:
                prefetched_ft = f_ft_eager.result()

    # Targeted Europe PMC fallback (avoids Europe PMC stalls when Crossref + PubMed already resolved!)
    if clean_doi and epmc_doi_resp is None:
        need_epmc_for_doi = (
            cr_rec is None
            or (not clean_pmid and pm_rec is None)
            or (bool(cit.claim) and not (pm_rec and pm_rec.abstract) and not (cr_rec and cr_rec.abstract))
            or (want_fulltext and not (pm_rec and pm_rec.pmcid))
        )
        if need_epmc_for_doi:
            cr_transient = is_transient_http(cr_resp)
            # When Crossref returned a definitive 404 and title is available, overlap EPMC check with DID_YOU_MEAN!
            if cr_rec is None and cr_resp is not None and cr_resp.status == 404 and cit.title and not did_you_mean:
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    f_ep = pool.submit(
                        fetch_epmc, http, clean_doi, None, fallback=True, primary_failed_transient=cr_transient
                    )
                    f_dym = pool.submit(_search_did_you_mean, http, cit.title, cit.author)
                    epmc_doi_rec, epmc_doi_resp = f_ep.result()
                    if epmc_doi_rec is None:
                        did_you_mean = f_dym.result()
            else:
                epmc_doi_rec, epmc_doi_resp = fetch_epmc(
                    http, doi=clean_doi, fallback=True, primary_failed_transient=cr_transient
                )

    if clean_pmid and epmc_pmid_rec is None and (prefetched is None or clean_pmid not in prefetched.epmc_by_pmid):
        already_covered = epmc_doi_rec is not None and epmc_doi_rec.pmid == clean_pmid
        need_epmc_for_pmid = (
            pm_rec is None
            or (bool(cit.claim) and not pm_rec.abstract and not (cr_rec and cr_rec.abstract))
            or (want_fulltext and not pm_rec.pmcid)
        )
        if not already_covered and need_epmc_for_pmid:
            epmc_pmid_rec, _ = fetch_epmc(
                http,
                pmid=clean_pmid,
                fallback=True,
                primary_failed_transient=is_transient_http(pm_resp),
            )

    doi_merged = merge_records([cr_rec, epmc_doi_rec])
    # Fail-closed: if a DOI-only lookup failed both Crossref and Europe PMC, never treat
    # speculative parallel PubMed esearch output as a resolved DOI record alongside DOI_NOT_FOUND.
    if clean_doi and not clean_pmid and doi_merged is None:
        pm_rec = None
        prefetched_ft = None
    pmid_merged = merge_records([pm_rec, epmc_pmid_rec])

    # If DOI had syntax error but cleaned candidate resolved, enrich the syntax diagnostic with proof!
    if doi_had_syntax_error and doi_merged is not None:
        for d in diagnostics:
            if d.code == "DOI_SYNTAX_INVALID":
                d.details["cleaned_candidate_resolved"] = {
                    "doi": doi_merged.doi,
                    "title": doi_merged.title,
                    "year": doi_merged.best_year(),
                }
                d.fix = (
                    f"Replace '{cit.doi}' with '{clean_doi}' "
                    f'(verified live: "{doi_merged.title}", {doi_merged.best_year()}).'
                )

    # Handle DOI not found
    if clean_doi and doi_merged is None:
        cr_err = cr_resp.error if cr_resp and cr_resp.error else f"HTTP {cr_resp.status if cr_resp else '?'}"
        epmc_err = (
            epmc_doi_resp.error
            if epmc_doi_resp and epmc_doi_resp.error
            else f"HTTP {epmc_doi_resp.status if epmc_doi_resp else '?'} (0 results)"
        )
        # Distinguish network outage vs genuine 404
        network_down = (cr_resp and cr_resp.status == 0) and (epmc_doi_resp and epmc_doi_resp.status == 0)
        if network_down:
            diagnostics.append(
                Diagnostic(
                    code="LOOKUP_ERROR",
                    severity="ERROR",
                    message=f"Network error while looking up DOI '{clean_doi}' (Crossref: {cr_err}; Europe PMC: {epmc_err}).",
                    expected="HTTP 200 from Crossref or Europe PMC",
                    actual=f"Crossref={cr_err}; EuropePMC={epmc_err}",
                    hint="Transient network failure, firewall block, or timeout.",
                    fix="Check network connectivity and retry with a larger --timeout.",
                )
            )
        else:
            if cit.title and not did_you_mean:
                did_you_mean = _search_did_you_mean(http, cit.title, author=cit.author)
            fix_msg = f"Remove or replace nonexistent DOI '{clean_doi}'."
            if did_you_mean:
                top = did_you_mean[0]
                fix_msg = (
                    f"DID_YOU_MEAN: DOI '{top['doi']}' "
                    f'("{top["title"]}", {top["first_author"] or "unknown"}, {top["year"]}, '
                    f"title_similarity={top['title_similarity']})?"
                )
            diagnostics.append(
                Diagnostic(
                    code="DOI_NOT_FOUND",
                    severity="ERROR",
                    message=f"DOI '{clean_doi}' does NOT exist in Crossref ({cr_err}) or Europe PMC ({epmc_err}).",
                    expected=f"Resolvable DOI '{clean_doi}'",
                    actual=f"Crossref: {cr_err}; Europe PMC: {epmc_err}",
                    details={"did_you_mean": did_you_mean},
                    hint="Hallucinated, typo'd, or unregistered DOI.",
                    fix=fix_msg,
                )
            )

    # Handle PMID not found
    if clean_pmid and pmid_merged is None:
        pm_err = pm_resp.error if pm_resp and pm_resp.error else "0 PubmedArticle records returned"
        if pm_resp and pm_resp.status == 0:
            diagnostics.append(
                Diagnostic(
                    code="LOOKUP_ERROR",
                    severity="ERROR",
                    message=f"Network error while looking up PMID '{clean_pmid}' ({pm_err}).",
                    expected="HTTP 200 from PubMed E-utilities",
                    actual=pm_err,
                    hint="Transient NCBI network error or timeout.",
                    fix="Retry with --timeout or set NCBI_API_KEY.",
                )
            )
        else:
            if cit.title and not did_you_mean:
                did_you_mean = _search_did_you_mean(http, cit.title, author=cit.author)
            fix_msg = f"Remove or replace nonexistent PMID '{clean_pmid}'."
            if did_you_mean:
                top = did_you_mean[0]
                fix_msg = (
                    f"DID_YOU_MEAN: PMID '{top['pmid'] or 'N/A'}' / DOI '{top['doi'] or 'N/A'}' "
                    f'("{top["title"]}", {top["first_author"] or "unknown"}, {top["year"]}, '
                    f"title_similarity={top['title_similarity']})?"
                )
            diagnostics.append(
                Diagnostic(
                    code="PMID_NOT_FOUND",
                    severity="ERROR",
                    message=f"PMID '{clean_pmid}' does NOT exist in PubMed ({pm_err}) or Europe PMC.",
                    expected=f"Resolvable PMID '{clean_pmid}'",
                    actual=f"PubMed: {pm_err}; Europe PMC: 0 results",
                    details={"did_you_mean": did_you_mean},
                    hint="Hallucinated, typo'd, or invalid PubMed ID.",
                    fix=fix_msg,
                )
            )

    # Cross-check PMID vs DOI conflict when both resolved
    conflict_detected = False
    if clean_doi and clean_pmid and doi_merged is not None and pmid_merged is not None:
        pmid_official_doi = (pmid_merged.doi or "").strip().lower()
        doi_official_pmid = (doi_merged.pmid or "").strip()
        doi_mismatch = bool(pmid_official_doi and pmid_official_doi != clean_doi.lower())
        pmid_mismatch = bool(doi_official_pmid and doi_official_pmid != clean_pmid)
        # Also detect conflict if titles are completely different and neither linked the other
        title_clash = (
            not pmid_official_doi
            and not doi_official_pmid
            and doi_merged.title
            and pmid_merged.title
            and title_similarity(doi_merged.title, pmid_merged.title) < 0.55
        )
        if doi_mismatch or pmid_mismatch or title_clash:
            conflict_detected = True
            diagnostics.append(
                Diagnostic(
                    code="PMID_DOI_CONFLICT",
                    severity="CRITICAL",
                    message=(
                        f"Cited PMID {clean_pmid} resolves to DOI {pmid_merged.doi or 'NONE'} "
                        f"('{pmid_merged.title}', {pmid_merged.best_year()}), "
                        f"NOT cited DOI {clean_doi} ('{doi_merged.title}', {doi_merged.best_year()})."
                    ),
                    expected=f"DOI {clean_doi} and PMID {clean_pmid} refer to the SAME paper",
                    actual=(
                        f"PMID {clean_pmid} -> DOI {pmid_merged.doi or 'NONE'} ('{pmid_merged.title}', {pmid_merged.best_year()}); "
                        f"DOI {clean_doi} -> PMID {doi_merged.pmid or 'NONE'} ('{doi_merged.title}', {doi_merged.best_year()})"
                    ),
                    details={
                        "pmid_record": {
                            "pmid": clean_pmid,
                            "doi": pmid_merged.doi,
                            "title": pmid_merged.title,
                            "year": pmid_merged.best_year(),
                        },
                        "doi_record": {
                            "doi": clean_doi,
                            "pmid": doi_merged.pmid,
                            "title": doi_merged.title,
                            "year": doi_merged.best_year(),
                        },
                    },
                    hint="The cited DOI and PMID belong to two completely different publications.",
                    fix="Check which paper you actually intended to cite and replace the mismatched PMID or DOI.",
                )
            )

    # Enrich DOI-only lookup with PubMed (for structured abstract & retraction PublicationTypes!)
    if clean_doi and not clean_pmid and doi_merged is not None and pm_rec is None:
        linked_pmid = doi_merged.pmid
        if not linked_pmid and prefetched is not None and doi_low in prefetched.pubmed_by_doi:
            pm_rec = prefetched.pubmed_by_doi[doi_low]
        else:
            if (
                not linked_pmid
                and not esearch_ran
                and (prefetched is None or doi_low not in prefetched.esearch_checked_dois)
            ):
                linked_pmid = pubmed_esearch_by_doi(http, clean_doi)
            if linked_pmid:
                if prefetched is not None and linked_pmid in prefetched.pubmed_by_pmid:
                    pm_extra, _ = prefetched.pubmed_by_pmid[linked_pmid]
                else:
                    pm_extra, _ = fetch_pubmed_pmid(http, linked_pmid)
                if pm_extra is not None:
                    # Guard against bogus PubMed esearch match if DOI differs
                    if not pm_extra.doi or pm_extra.doi.lower() == clean_doi.lower():
                        pm_rec = pm_extra

    # Enrich PMID-only lookup with Crossref (for Crossref update-to/updated-by retraction relations & print/online dates)
    if clean_pmid and not clean_doi and pmid_merged is not None and pmid_merged.doi and cr_rec is None:
        pm_doi_low = pmid_merged.doi.lower()
        if prefetched is not None and pm_doi_low in prefetched.crossref_by_doi:
            cr_extra, _ = prefetched.crossref_by_doi[pm_doi_low]
        else:
            cr_extra, _ = fetch_crossref_doi(http, pmid_merged.doi)
        if cr_extra is not None:
            cr_rec = cr_extra

    # Build final resolved PaperRecord
    if conflict_detected:
        # Don't silently blend two conflicting papers; keep DOI record as primary resolved reference
        resolved = doi_merged
    else:
        resolved = merge_records([pm_rec, cr_rec, epmc_doi_rec, epmc_pmid_rec])

    if resolved is None:
        return VerificationResult(
            input=cit,
            status=_compute_status(diagnostics),
            resolved=None,
            diagnostics=diagnostics,
            did_you_mean=did_you_mean,
        )

    # Retraction / Expression of concern / Erratum checks
    diagnostics.extend(check_retraction_and_notices(resolved))

    # Metadata verification checks
    if cit.title and resolved.title:
        t_diag = check_title(cit.title, resolved.title)
        if t_diag:
            diagnostics.append(t_diag)

    if cit.author:
        a_diag = check_author(cit.author, resolved.authors)
        if a_diag:
            diagnostics.append(a_diag)

    if cit.year is not None and str(cit.year).strip() != "":
        y_diag = check_year(str(cit.year), resolved)
        if y_diag:
            diagnostics.append(y_diag)

    if cit.journal:
        j_diag = check_journal(cit.journal, resolved)
        if j_diag:
            diagnostics.append(j_diag)

    # Claim Validity Pack
    claim_report: Optional[ClaimReport] = None
    if cit.claim:
        if prefetched_ft is None and want_fulltext and resolved.pmcid and prefetched is not None:
            prefetched_ft = prefetched.fulltext_by_pmcid.get(resolved.pmcid.strip().upper())
        # Guard against using an eagerly prefetched fulltext if resolved PMCID somehow differed
        if (
            prefetched_ft is not None
            and eager_ft_pmcid is not None
            and resolved.pmcid
            and resolved.pmcid.strip().upper() != eager_ft_pmcid
        ):
            prefetched_ft = None
        claim_report, c_diags = evaluate_claim(
            cit.claim,
            resolved,
            http=http,
            fetch_fulltext=cit.fulltext,
            prefetched_fulltext=prefetched_ft,
        )
        diagnostics.extend(c_diags)

    diagnostics.sort(key=lambda d: SEVERITY_ORDER.get(d.severity, 99))
    return VerificationResult(
        input=cit,
        status=_compute_status(diagnostics),
        resolved=resolved,
        diagnostics=diagnostics,
        claim_report=claim_report,
        did_you_mean=did_you_mean,
    )


def verify_citations_batch(
    citations: Sequence[CitationInput],
    http: HttpClient,
    max_workers: int = DEFAULT_WORKERS,
) -> List[VerificationResult]:
    """Verify multiple citations with multi-ID batch prefetching and concurrent resolution."""
    if not citations:
        return []
    workers = max(1, int(max_workers))
    if len(citations) == 1:
        return [verify_citation(citations[0], http)]

    # Phase 0: Local syntax pre-validation to collect unique DOIs and PMIDs
    parsed_ids: List[Tuple[Optional[str], Optional[str], bool]] = []
    unique_dois: List[str] = []
    seen_dois: set[str] = set()
    unique_pmids: List[str] = []
    seen_pmids: set[str] = set()

    for cit in citations:
        c_doi = validate_doi(cit.doi)[0] if cit.doi is not None and cit.doi != "" else None
        c_pmid = validate_pmid(cit.pmid)[0] if cit.pmid is not None and cit.pmid != "" else None
        parsed_ids.append((c_doi, c_pmid, bool(cit.claim and cit.fulltext)))
        if c_doi and c_doi.lower() not in seen_dois:
            seen_dois.add(c_doi.lower())
            unique_dois.append(c_doi)
        if c_pmid and c_pmid not in seen_pmids:
            seen_pmids.add(c_pmid)
            unique_pmids.append(c_pmid)

    cache = PrefetchCache()
    doi_only_list = [
        d for d in unique_dois if any(cd and cd.lower() == d.lower() and not cp for cd, cp, _ in parsed_ids)
    ]
    esearch_discovered_pmids: List[str] = []

    # Phase 1: Parallel batch prefetch across Crossref + PubMed (esearch for DOIs, efetch for user PMIDs)
    def _phase1_esearch() -> None:
        if not doi_only_list:
            return
        ids, ok = pubmed_esearch_by_dois_batch(http, doi_only_list)
        if ok:
            cache.esearch_checked_dois.update(d.lower() for d in doi_only_list)
        esearch_discovered_pmids.extend(ids)

    _run_tasks(
        [
            lambda: cache.store_crossref_dois(http, unique_dois, max_workers=min(workers, 4)),
            _phase1_esearch,
            lambda: cache.store_pubmed_pmids(http, unique_pmids),
        ],
        workers=min(workers, 3),
    )

    # Phase 2: Parallel PubMed efetch (DOI-discovered PMIDs) + Crossref enrich (PMID-discovered DOIs) + early fulltext
    discovered_dois = [
        pm_rec.doi
        for c_doi, c_pmid, _ in parsed_ids
        if c_pmid and not c_doi and (pm_rec := cache.pubmed_record(None, c_pmid)) and pm_rec.doi
    ]
    early_pmcids = [
        pm_r.pmcid.strip().upper()
        for _, c_pmid, want_ft in parsed_ids
        if want_ft and c_pmid and (pm_r := cache.pubmed_record(None, c_pmid)) and pm_r.pmcid
    ]

    _run_tasks(
        [
            lambda: cache.store_pubmed_pmids(http, esearch_discovered_pmids),
            lambda: cache.store_crossref_dois(http, discovered_dois, max_workers=min(workers, 4)),
            lambda: cache.fulltext_by_pmcid.update(
                fetch_oa_fulltext_batch(http, early_pmcids, max_workers=min(workers, 4))
            ),
        ],
        workers=min(workers, 3),
    )

    # Phase 2b/2c: Targeted Europe PMC fallback batch (including missing PMCIDs for --fulltext)
    # overlapped in parallel with NCBI PMC batch fulltext for already-known PMCIDs.
    epmc_fallback_dois: List[str] = []
    epmc_fallback_pmids: List[str] = []
    needed_pmcids: List[str] = []
    for cit, (c_doi, c_pmid, want_ft) in zip(citations, parsed_ids):
        cr_r = cache.crossref_record(c_doi)
        pm_r = cache.pubmed_record(c_doi, c_pmid)
        if c_doi and (
            cr_r is None
            or (not c_pmid and pm_r is None)
            or (bool(cit.claim) and not (pm_r and pm_r.abstract) and not (cr_r and cr_r.abstract))
            or (want_ft and not (pm_r and pm_r.pmcid))
        ):
            epmc_fallback_dois.append(c_doi)
        if c_pmid and (pm_r is None or (want_ft and not pm_r.pmcid)):
            epmc_fallback_pmids.append(c_pmid)
        if want_ft and pm_r and pm_r.pmcid:
            pmcid_u = pm_r.pmcid.strip().upper()
            if pmcid_u not in cache.fulltext_by_pmcid:
                needed_pmcids.append(pmcid_u)

    def _phase2c_epmc_fallback() -> None:
        if not epmc_fallback_dois and not epmc_fallback_pmids:
            return
        epmc_new_pmids = cache.store_epmc_batch(
            http,
            dois=epmc_fallback_dois,
            pmids=epmc_fallback_pmids,
            max_workers=min(workers, 4),
            fallback=True,
        )
        if epmc_new_pmids:
            cache.store_pubmed_pmids(http, epmc_new_pmids)

    _run_tasks(
        [
            _phase2c_epmc_fallback,
            lambda: cache.fulltext_by_pmcid.update(
                fetch_oa_fulltext_batch(http, needed_pmcids, max_workers=min(workers, 4))
            ),
        ],
        workers=min(workers, 2),
    )

    # If Europe PMC fallback revealed any extra PMCIDs for --fulltext, batch-fetch them with OA flags
    extra_pmcids = [
        epmc_r.pmcid.strip().upper()
        for c_doi, c_pmid, want_ft in parsed_ids
        if want_ft
        and (epmc_r := cache.epmc_record(c_doi, c_pmid))
        and epmc_r.pmcid
        and epmc_r.pmcid.strip().upper() not in cache.fulltext_by_pmcid
    ]
    if extra_pmcids:
        cache.fulltext_by_pmcid.update(
            fetch_oa_fulltext_batch(
                http,
                extra_pmcids,
                oa_flags=cache.oa_flags_by_pmcid(),
                max_workers=min(workers, 4),
            )
        )

    # Phase 3: Final verification (concurrent for any remaining fallbacks / DID_YOU_MEAN), preserving input order
    if workers == 1:
        return [verify_citation(cit, http, prefetched=cache) for cit in citations]

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(citations))) as pool:
        futures = [pool.submit(verify_citation, cit, http, cache) for cit in citations]
        return [f.result() for f in futures]


def _compute_status(diagnostics: Sequence[Diagnostic]) -> str:
    if any(d.severity in ("CRITICAL", "ERROR") for d in diagnostics):
        return "FAIL"
    if any(d.severity == "WARNING" for d in diagnostics):
        return "WARN"
    return "PASS"
