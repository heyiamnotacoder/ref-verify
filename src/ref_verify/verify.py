"""Verification orchestration: resolves identifiers across sources and assembles diagnostics."""

from __future__ import annotations

import concurrent.futures
from collections.abc import Sequence
from typing import Any

from ref_verify.claims import evaluate_claim
from ref_verify.grounding import ground_sentence, reconcile_sentence_checks
from ref_verify.http_client import HttpClient, is_transient_http
from ref_verify.identifiers import clean_identifiers
from ref_verify.identity import (
    doi_lookup_down,
    doi_not_found_diagnostic,
    enrich_doi_syntax_fix,
    id_conflict_diagnostic,
    no_identifier_diagnostic,
    pmid_lookup_down,
    pmid_not_found_diagnostic,
)
from ref_verify.metadata import check_metadata, merge_records
from ref_verify.models import (
    SEVERITY_ORDER,
    CitationInput,
    ClaimReport,
    Diagnostic,
    FulltextResult,
    HttpResponse,
    PaperRecord,
    SentenceCheck,
    VerificationResult,
    status_for,
)
from ref_verify.prefetch import PrefetchCache, needs_epmc_for_doi, needs_epmc_for_pmid, prefetch_batch
from ref_verify.sources.crossref import fetch_crossref_doi, search_crossref_by_title
from ref_verify.sources.europepmc import fetch_epmc
from ref_verify.sources.fulltext import fetch_oa_fulltext_paragraphs
from ref_verify.sources.pubmed import (
    fetch_pubmed_pmid,
    pubmed_esearch_by_doi,
    search_pubmed_by_title,
)
from ref_verify.text import normalize_text

DEFAULT_WORKERS = 8


def _search_did_you_mean(http: HttpClient, title: str | None, author: str | None = None) -> list[dict[str, Any]]:
    if not title:
        return []
    candidates: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_cr = pool.submit(search_crossref_by_title, http, title, author, 8)
        f_pm = pool.submit(search_pubmed_by_title, http, title, 5)
        candidates.extend(f_cr.result())
        candidates.extend(f_pm.result())

    # Merge duplicates by DOI (combining PubMed PMID + Crossref citation count when both return the same paper)
    merged_by_key: dict[str, dict[str, Any]] = {}
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

    def _rank_key(x: dict[str, Any]) -> tuple[float, int, int, int]:
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
    prefetched: PrefetchCache | None = None,
) -> VerificationResult:
    clean_doi, clean_pmid, diagnostics = clean_identifiers(cit)
    did_you_mean: list[dict[str, Any]] = []

    if not clean_doi and not clean_pmid:
        if cit.title:
            did_you_mean = _search_did_you_mean(http, cit.title, author=cit.author)
        if cit.title or not diagnostics:
            diagnostics.append(no_identifier_diagnostic(cit.title, did_you_mean))
        return _result(cit, diagnostics, did_you_mean=did_you_mean)

    doi_low = clean_doi.lower() if clean_doi else None
    cr_rec: PaperRecord | None = None
    epmc_doi_rec: PaperRecord | None = None
    cr_resp: HttpResponse | None = None
    epmc_doi_resp: HttpResponse | None = None
    pm_rec: PaperRecord | None = None
    epmc_pmid_rec: PaperRecord | None = None
    pm_resp: HttpResponse | None = None
    prefetched_ft: FulltextResult | None = None
    want_fulltext = bool(cit.has_claims and cit.fulltext)

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

    f_cr: concurrent.futures.Future[tuple[PaperRecord | None, HttpResponse]] | None = None
    f_pm: concurrent.futures.Future[tuple[PaperRecord | None, HttpResponse]] | None = None
    f_esearch: concurrent.futures.Future[str | None] | None = None
    f_pm_enrich: concurrent.futures.Future[tuple[PaperRecord | None, HttpResponse]] | None = None
    f_cr_enrich: concurrent.futures.Future[tuple[PaperRecord | None, HttpResponse]] | None = None
    f_ft_eager: concurrent.futures.Future[FulltextResult] | None = None
    f_epmc_pmid: concurrent.futures.Future[tuple[PaperRecord | None, HttpResponse]] | None = None
    eager_ft_pmcid: str | None = None
    epmc_pmid_checked = prefetched is not None and bool(clean_pmid) and clean_pmid in prefetched.epmc_by_pmid

    if need_cr or need_pm or need_esearch_parallel:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            if need_cr and clean_doi:
                f_cr = pool.submit(fetch_crossref_doi, http, clean_doi)
            if need_pm and clean_pmid:
                f_pm = pool.submit(fetch_pubmed_pmid, http, clean_pmid)
            if need_esearch_parallel and clean_doi:
                esearch_ran = True
                f_esearch = pool.submit(pubmed_esearch_by_doi, http, clean_doi)

            def _maybe_start_eager_ft(rec: PaperRecord | None) -> None:
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
                        # Overlap the Europe PMC PMID fallback with Crossref enrichment when its need is
                        # already decided by the PubMed record alone (missing record or missing PMCID).
                        if (
                            clean_pmid
                            and not clean_doi
                            and not epmc_pmid_checked
                            and (pm_rec is None or (want_fulltext and not pm_rec.pmcid))
                        ):
                            epmc_pmid_checked = True
                            f_epmc_pmid = pool.submit(
                                fetch_epmc,
                                http,
                                pmid=clean_pmid,
                                fallback=True,
                                primary_failed_transient=is_transient_http(pm_resp),
                            )
                            pending.add(f_epmc_pmid)
                    elif fut is f_epmc_pmid:
                        epmc_pmid_rec, _ = fut.result()
                        _maybe_start_eager_ft(epmc_pmid_rec)
                    elif fut is f_cr_enrich:
                        cr_extra, _ = fut.result()
                        if cr_extra is not None:
                            cr_rec = cr_extra

            if f_ft_eager is not None:
                prefetched_ft = f_ft_eager.result()

    # Targeted Europe PMC fallback (avoids Europe PMC stalls when Crossref + PubMed already resolved!)
    if clean_doi and epmc_doi_resp is None:
        need_epmc_for_doi = needs_epmc_for_doi(
            cit.has_claims, cr_rec, pm_rec, has_pmid=bool(clean_pmid), want_fulltext=want_fulltext
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

    if clean_pmid and not epmc_pmid_checked:
        already_covered = epmc_doi_rec is not None and epmc_doi_rec.pmid == clean_pmid
        need_epmc_for_pmid = needs_epmc_for_pmid(cit.has_claims, pm_rec, cr_rec, want_fulltext=want_fulltext)
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

    if clean_doi and doi_merged is not None and cit.doi:
        enrich_doi_syntax_fix(diagnostics, cit.doi, clean_doi, doi_merged)

    doi_missing = bool(clean_doi and doi_merged is None)
    pmid_missing = bool(clean_pmid and pmid_merged is None)
    genuine_miss = (doi_missing and not doi_lookup_down(cr_resp, epmc_doi_resp)) or (
        pmid_missing and not pmid_lookup_down(pm_resp)
    )
    if genuine_miss and cit.title and not did_you_mean:
        did_you_mean = _search_did_you_mean(http, cit.title, author=cit.author)
    if doi_missing and clean_doi:
        diagnostics.append(doi_not_found_diagnostic(clean_doi, cr_resp, epmc_doi_resp, did_you_mean))
    if pmid_missing and clean_pmid:
        diagnostics.append(pmid_not_found_diagnostic(clean_pmid, pm_resp, did_you_mean))

    conflict: Diagnostic | None = None
    if clean_doi and clean_pmid and doi_merged is not None and pmid_merged is not None:
        conflict = id_conflict_diagnostic(clean_doi, clean_pmid, doi_merged, pmid_merged)
        if conflict is not None:
            diagnostics.append(conflict)

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

    # Never silently blend two conflicting papers; keep the DOI record as the resolved reference.
    resolved = doi_merged if conflict is not None else merge_records([pm_rec, cr_rec, epmc_doi_rec, epmc_pmid_rec])
    if resolved is None:
        return _result(cit, diagnostics, did_you_mean=did_you_mean)

    diagnostics.extend(check_metadata(cit, resolved))

    # Claim Validity Pack: an explicit --claim and/or the document's own citing sentences
    claim_report: ClaimReport | None = None
    sentence_checks: list[SentenceCheck] = []
    if cit.has_claims:
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
        if prefetched_ft is None and cit.fulltext and resolved.pmcid:
            prefetched_ft = fetch_oa_fulltext_paragraphs(http, resolved.pmcid, is_open_access=resolved.is_open_access)
        if cit.claim:
            claim_report, c_diags = evaluate_claim(
                cit.claim, resolved, fulltext_requested=cit.fulltext, fulltext=prefetched_ft
            )
            diagnostics.extend(c_diags)
        # Sentence diagnostics are added by reconcile_sentence_checks once every co-cited paper is known.
        sentence_checks = [
            ground_sentence(s, resolved, fulltext_requested=cit.fulltext, fulltext=prefetched_ft)
            for s in cit.citing_sentences
        ]

    diagnostics.sort(key=lambda d: SEVERITY_ORDER.get(d.severity, 99))
    result = _result(cit, diagnostics, resolved, claim_report, did_you_mean)
    result.sentence_checks = sentence_checks
    return result


def _result(
    cit: CitationInput,
    diagnostics: list[Diagnostic],
    resolved: PaperRecord | None = None,
    claim_report: ClaimReport | None = None,
    did_you_mean: list[dict[str, Any]] | None = None,
) -> VerificationResult:
    return VerificationResult(
        input=cit,
        status=status_for(diagnostics),
        resolved=resolved,
        diagnostics=diagnostics,
        claim_report=claim_report,
        did_you_mean=did_you_mean or [],
    )


def verify_citations_batch(
    citations: Sequence[CitationInput],
    http: HttpClient,
    max_workers: int = DEFAULT_WORKERS,
) -> list[VerificationResult]:
    """Verify multiple citations with multi-ID batch prefetching and concurrent resolution."""
    if not citations:
        return []
    workers = max(1, int(max_workers))
    if len(citations) == 1:
        return reconcile_sentence_checks([verify_citation(citations[0], http)])

    # Phase 0: Local syntax pre-validation to collect unique DOIs and PMIDs
    parsed_ids: list[tuple[str | None, str | None, bool]] = []
    unique_dois: list[str] = []
    seen_dois: set[str] = set()
    unique_pmids: list[str] = []
    seen_pmids: set[str] = set()

    for cit in citations:
        c_doi, c_pmid, _ = clean_identifiers(cit)
        parsed_ids.append((c_doi, c_pmid, bool(cit.has_claims and cit.fulltext)))
        if c_doi and c_doi.lower() not in seen_dois:
            seen_dois.add(c_doi.lower())
            unique_dois.append(c_doi)
        if c_pmid and c_pmid not in seen_pmids:
            seen_pmids.add(c_pmid)
            unique_pmids.append(c_pmid)

    if workers == 1:
        cache = prefetch_batch(citations, parsed_ids, unique_dois, unique_pmids, http, workers)
        return reconcile_sentence_checks([verify_citation(cit, http, prefetched=cache) for cit in citations])

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(citations))) as pool:
        # Citations with no usable identifier never read the prefetch cache, so their title
        # searches start now and overlap the whole prefetch instead of waiting behind it.
        early = {
            i: pool.submit(verify_citation, cit, http)
            for i, (cit, (c_doi, c_pmid, _)) in enumerate(zip(citations, parsed_ids))
            if not c_doi and not c_pmid
        }
        cache = prefetch_batch(citations, parsed_ids, unique_dois, unique_pmids, http, workers)
        futures = [early.get(i) or pool.submit(verify_citation, cit, http, cache) for i, cit in enumerate(citations)]
        return reconcile_sentence_checks([f.result() for f in futures])
