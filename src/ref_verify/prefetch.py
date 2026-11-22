"""Batch prefetching: fetch the records a citation batch needs in as few round-trips as possible."""

from __future__ import annotations

import concurrent.futures
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ref_verify.http_client import HttpClient, HttpResponse
from ref_verify.models import CitationInput, FulltextResult, PaperRecord
from ref_verify.sources.crossref import fetch_crossref_doi, fetch_crossref_dois_batch
from ref_verify.sources.europepmc import fetch_epmc_batch
from ref_verify.sources.fulltext import fetch_oa_fulltext_batch
from ref_verify.sources.pubmed import fetch_pubmed_pmids_batch, pubmed_esearch_by_dois_batch


def needs_epmc_for_doi(
    claim: str | None,
    cr_rec: PaperRecord | None,
    pm_rec: PaperRecord | None,
    *,
    has_pmid: bool,
    want_fulltext: bool,
) -> bool:
    """Whether Europe PMC must be asked about a DOI given what Crossref and PubMed returned."""
    return (
        cr_rec is None
        or (not has_pmid and pm_rec is None)
        or (bool(claim) and not (pm_rec and pm_rec.abstract) and not (cr_rec and cr_rec.abstract))
        or (want_fulltext and not (pm_rec and pm_rec.pmcid))
    )


def needs_epmc_for_pmid(
    claim: str | None,
    pm_rec: PaperRecord | None,
    cr_rec: PaperRecord | None,
    *,
    want_fulltext: bool,
) -> bool:
    """Whether Europe PMC must be asked about a PMID given what PubMed and Crossref returned."""
    return (
        pm_rec is None
        or (bool(claim) and not pm_rec.abstract and not (cr_rec and cr_rec.abstract))
        or (want_fulltext and not pm_rec.pmcid)
    )


@dataclass
class PrefetchCache:
    """In-memory batch prefetch results keyed by normalized DOI / PMID / PMCID."""

    crossref_by_doi: dict[str, tuple[PaperRecord | None, HttpResponse]] = field(default_factory=dict)
    epmc_by_doi: dict[str, tuple[PaperRecord | None, HttpResponse]] = field(default_factory=dict)
    epmc_by_pmid: dict[str, tuple[PaperRecord | None, HttpResponse]] = field(default_factory=dict)
    pubmed_by_pmid: dict[str, tuple[PaperRecord | None, HttpResponse]] = field(default_factory=dict)
    pubmed_by_doi: dict[str, PaperRecord] = field(default_factory=dict)
    esearch_checked_dois: set[str] = field(default_factory=set)
    fulltext_by_pmcid: dict[str, FulltextResult] = field(default_factory=dict)

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

    def confirm_crossref_misses(self, http: HttpClient, dois: Sequence[str], max_workers: int = 4) -> None:
        """Re-query batch misses one by one; cache only definitive answers (200/404) so outages still retry."""

        def _confirm(doi: str) -> None:
            rec, resp = fetch_crossref_doi(http, doi)
            if rec is not None or resp.status == 404:
                self.crossref_by_doi[doi.lower()] = (rec, resp)

        run_tasks([lambda d=d: _confirm(d) for d in dois], workers=max_workers)

    def unfetched_epmc_pmids(self) -> list[str]:
        """PMIDs linked from Europe PMC DOI records that PubMed has not been asked for yet."""
        return [
            rec.pmid
            for rec, _ in self.epmc_by_doi.values()
            if rec is not None and rec.pmid and rec.pmid not in self.pubmed_by_pmid
        ]

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
    ) -> None:
        by_doi, by_pmid = fetch_epmc_batch(http, dois=dois, pmids=pmids, max_workers=max_workers, fallback=fallback)
        self.epmc_by_doi.update(by_doi)
        self.epmc_by_pmid.update(by_pmid)

    def crossref_record(self, doi: str | None) -> PaperRecord | None:
        if not doi:
            return None
        pair = self.crossref_by_doi.get(doi.lower())
        return pair[0] if pair else None

    def pubmed_record(self, doi: str | None, pmid: str | None) -> PaperRecord | None:
        if pmid and pmid in self.pubmed_by_pmid:
            return self.pubmed_by_pmid[pmid][0]
        if doi and not pmid:
            return self.pubmed_by_doi.get(doi.lower())
        return None

    def epmc_record(self, doi: str | None, pmid: str | None) -> PaperRecord | None:
        if doi and doi.lower() in self.epmc_by_doi:
            rec = self.epmc_by_doi[doi.lower()][0]
            if rec is not None:
                return rec
        if pmid and pmid in self.epmc_by_pmid:
            return self.epmc_by_pmid[pmid][0]
        return None

    def oa_flags_by_pmcid(self) -> dict[str, bool | None]:
        flags: dict[str, bool | None] = {}
        for store in (self.epmc_by_doi.values(), self.epmc_by_pmid.values()):
            for rec, _ in store:
                if rec is not None and rec.pmcid and rec.is_open_access is not None:
                    flags[rec.pmcid.strip().upper()] = rec.is_open_access
        return flags


def run_tasks(tasks: Sequence[Any], workers: int) -> None:
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


def prefetch_batch(
    citations: Sequence[CitationInput],
    parsed_ids: Sequence[tuple[str | None, str | None, bool]],
    unique_dois: Sequence[str],
    unique_pmids: Sequence[str],
    http: HttpClient,
    workers: int,
) -> PrefetchCache:
    """Batch-prefetch every source record the citations will need, minimizing round-trips.

    ``parsed_ids`` holds each citation's syntax-cleaned (doi, pmid, wants_fulltext).
    """
    cache = PrefetchCache()
    seen_dois = {d.lower() for d in unique_dois}
    doi_only_keys = {c_doi.lower() for c_doi, c_pmid, _ in parsed_ids if c_doi and not c_pmid}
    doi_only_list = [d for d in unique_dois if d.lower() in doi_only_keys]

    user_pmids = set(unique_pmids)
    # DOIs already sent to Europe PMC; a failed (e.g. timed-out) query is left for the per-citation
    # path to retry with full policy rather than being re-batched here.
    epmc_tried_dois: set[str] = set()
    fanout = min(workers, 4)

    # Phase 1: three independent chains run concurrently; each starts its follow-up fetch the moment
    # its own input arrives instead of waiting for the slowest chain (no phase barrier).
    def _crossref_chain() -> None:
        cache.store_crossref_dois(http, unique_dois, max_workers=fanout)
        # A Crossref miss is decided here, so confirm it and ask Europe PMC now instead of after phase 1.
        missing = [d for d in unique_dois if d.lower() not in cache.crossref_by_doi]
        epmc_tried_dois.update(d.lower() for d in missing)
        run_tasks(
            [
                lambda: cache.confirm_crossref_misses(http, missing, max_workers=fanout),
                lambda: cache.store_epmc_batch(http, dois=missing, max_workers=fanout, fallback=True),
            ]
            if missing
            else [],
            workers=min(workers, 2),
        )

    def _esearch_chain() -> None:
        if not doi_only_list:
            return
        ids, checked = pubmed_esearch_by_dois_batch(http, doi_only_list)
        cache.esearch_checked_dois.update(d.lower() for d in checked)
        # User-supplied PMIDs are fetched by the PMID chain; only fetch the newly discovered ones.
        cache.store_pubmed_pmids(http, [p for p in ids if p not in user_pmids])
        # DOI-only --fulltext citations: their PMCID is known now, so fetch the full text immediately.
        ft_pmcids = [
            pm_r.pmcid.strip().upper()
            for c_doi, c_pmid, want_ft in parsed_ids
            if want_ft and c_doi and not c_pmid and (pm_r := cache.pubmed_record(c_doi, None)) and pm_r.pmcid
        ]
        if ft_pmcids:
            cache.fulltext_by_pmcid.update(fetch_oa_fulltext_batch(http, ft_pmcids, max_workers=fanout))

    def _pmid_chain() -> None:
        cache.store_pubmed_pmids(http, unique_pmids)
        pmid_only_recs = [
            pm_rec
            for c_doi, c_pmid, _ in parsed_ids
            if c_pmid and not c_doi and (pm_rec := cache.pubmed_record(None, c_pmid)) is not None
        ]
        discovered_dois = [r.doi for r in pmid_only_recs if r.doi and r.doi.lower() not in seen_dois]
        early_pmcids = [
            pm_r.pmcid.strip().upper()
            for _, c_pmid, want_ft in parsed_ids
            if want_ft and c_pmid and (pm_r := cache.pubmed_record(None, c_pmid)) and pm_r.pmcid
        ]
        run_tasks(
            [
                lambda: cache.store_crossref_dois(http, discovered_dois, max_workers=fanout),
                lambda: cache.fulltext_by_pmcid.update(fetch_oa_fulltext_batch(http, early_pmcids, max_workers=fanout)),
            ],
            workers=min(workers, 2),
        )

    run_tasks([_crossref_chain, _esearch_chain, _pmid_chain], workers=min(workers, 3))

    # Phase 2: targeted Europe PMC fallback (including missing PMCIDs for --fulltext),
    # overlapped with NCBI PMC fulltext for already-known PMCIDs.
    epmc_fallback_dois: list[str] = []
    epmc_fallback_pmids: list[str] = []
    needed_pmcids: list[str] = []
    for cit, (c_doi, c_pmid, want_ft) in zip(citations, parsed_ids):
        cr_r = cache.crossref_record(c_doi)
        pm_r = cache.pubmed_record(c_doi, c_pmid)
        if (
            c_doi
            and c_doi.lower() not in epmc_tried_dois
            and needs_epmc_for_doi(cit.claim, cr_r, pm_r, has_pmid=bool(c_pmid), want_fulltext=want_ft)
        ):
            epmc_fallback_dois.append(c_doi)
        if (
            c_pmid
            and c_pmid not in cache.epmc_by_pmid
            and needs_epmc_for_pmid(cit.claim, pm_r, cr_r, want_fulltext=want_ft)
        ):
            epmc_fallback_pmids.append(c_pmid)
        if want_ft and pm_r and pm_r.pmcid:
            pmcid_u = pm_r.pmcid.strip().upper()
            if pmcid_u not in cache.fulltext_by_pmcid:
                needed_pmcids.append(pmcid_u)

    def _phase2c_epmc_fallback() -> None:
        if epmc_fallback_dois or epmc_fallback_pmids:
            cache.store_epmc_batch(
                http,
                dois=epmc_fallback_dois,
                pmids=epmc_fallback_pmids,
                max_workers=fanout,
                fallback=True,
            )
        new_pmids = cache.unfetched_epmc_pmids()
        if new_pmids:
            cache.store_pubmed_pmids(http, new_pmids)

    run_tasks(
        [
            _phase2c_epmc_fallback,
            lambda: cache.fulltext_by_pmcid.update(fetch_oa_fulltext_batch(http, needed_pmcids, max_workers=fanout)),
        ],
        workers=min(workers, 2),
    )

    # Phase 3: if Europe PMC fallback revealed any extra PMCIDs for --fulltext, batch-fetch them with OA flags
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
                max_workers=fanout,
            )
        )

    return cache
