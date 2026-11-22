"""Europe PMC REST API: result parsing and single/batch DOI or PMID lookup."""

from __future__ import annotations

import concurrent.futures
import json
import urllib.parse
from collections.abc import Sequence
from typing import Any

from ref_verify.http_client import HttpClient, HttpResponse
from ref_verify.models import Author, PaperRecord
from ref_verify.text import parse_year_int, strip_tags

EPMC_BATCH_SIZE = 20


def parse_epmc_result(item: dict[str, Any]) -> PaperRecord:
    rec = PaperRecord(sources=["europepmc"])
    if item.get("doi"):
        rec.doi = str(item["doi"]).strip()
    if item.get("pmid"):
        rec.pmid = str(item["pmid"]).strip()
    if item.get("pmcid"):
        rec.pmcid = str(item["pmcid"]).strip()
    if item.get("title"):
        rec.title = strip_tags(str(item["title"])).rstrip(".")
    if item.get("abstractText"):
        rec.abstract = strip_tags(str(item["abstractText"]))

    au_list = (item.get("authorList") or {}).get("author") or []
    for a in au_list:
        if not isinstance(a, dict):
            continue
        if a.get("collectiveName") and not a.get("lastName"):
            rec.authors.append(Author(family="", collective=str(a["collectiveName"]).strip()))
        else:
            last = str(a.get("lastName") or "").strip()
            first = str(a.get("firstName") or "").strip()
            inits = str(a.get("initials") or "").strip()
            if last:
                rec.authors.append(Author(family=last, given=first, initials=inits))

    jinfo = item.get("journalInfo") or {}
    journal = jinfo.get("journal") or {}
    if journal.get("title"):
        rec.journal_full = str(journal["title"]).strip()
    if journal.get("medlineAbbreviation"):
        rec.journal_abbrev = str(journal["medlineAbbreviation"]).strip()
    elif journal.get("isoabbreviation"):
        rec.journal_abbrev = str(journal["isoabbreviation"]).strip()

    rec.published_print = parse_year_int(jinfo.get("yearOfPublication")) or parse_year_int(item.get("pubYear"))
    rec.published_online = parse_year_int(item.get("firstPublicationDate"))
    rec.issued_year = rec.published_print or rec.published_online

    is_oa = str(item.get("isOpenAccess", "")).upper()
    if is_oa in ("Y", "N"):
        rec.is_open_access = is_oa == "Y"

    pub_types = (item.get("pubTypeList") or {}).get("pubType") or []
    for pt in pub_types:
        pt_low = str(pt).lower()
        if "retracted publication" in pt_low:
            rec.retracted = True
            rec.retraction_reasons.append(f"Europe PMC pubType: {pt}")
        elif "retraction of publication" in pt_low or pt_low == "retraction":
            rec.is_retraction_notice = True
        elif "expression of concern" in pt_low:
            rec.expression_of_concern.append(f"Europe PMC pubType: {pt}")

    cc_list = (item.get("commentCorrectionList") or {}).get("commentCorrection") or []
    for cc in cc_list:
        if not isinstance(cc, dict):
            continue
        ctype = str(cc.get("type", "")).lower()
        cid = str(cc.get("id", "")).strip()
        if "retraction in" in ctype or ctype == "retractionin":
            rec.retracted = True
            rec.retraction_reasons.append(f"Europe PMC {cc.get('type')}: {cid}")
            if cid:
                rec.retraction_notice_ids.append(cid)
        elif "expression of concern in" in ctype:
            rec.expression_of_concern.append(f"Europe PMC {cc.get('type')}: {cid}")
        elif "erratum in" in ctype:
            rec.correction_notices.append(f"Europe PMC {cc.get('type')}: {cid}")

    return rec


def fetch_epmc(
    http: HttpClient,
    doi: str | None = None,
    pmid: str | None = None,
    *,
    fallback: bool = False,
    primary_failed_transient: bool = False,
) -> tuple[PaperRecord | None, HttpResponse]:
    if doi:
        query = f'DOI:"{doi}"'
    elif pmid:
        query = f"EXT_ID:{pmid} AND SRC:MED"
    else:
        return None, HttpResponse(status=0, body="", url="", error="No identifier for Europe PMC")
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urllib.parse.urlencode(
        {"query": query, "format": "json", "resultType": "core", "pageSize": "3"}
    )
    if fallback:
        resp = http.get_fallback(
            url, headers={"Accept": "application/json"}, primary_failed_transient=primary_failed_transient
        )
    else:
        resp = http.get(url, headers={"Accept": "application/json"})
    if resp.status != 200:
        return None, resp
    try:
        results = json.loads(resp.body).get("resultList", {}).get("result", [])
    except json.JSONDecodeError as exc:
        resp.error = f"Invalid JSON from Europe PMC: {exc}"
        return None, resp
    if not results:
        return None, resp
    # Prefer exact DOI or PMID match
    for item in results:
        if doi and str(item.get("doi", "")).strip().lower() == doi.lower():
            return parse_epmc_result(item), resp
        if pmid and str(item.get("pmid", "")).strip() == pmid:
            return parse_epmc_result(item), resp
    return parse_epmc_result(results[0]), resp


def fetch_epmc_batch(
    http: HttpClient,
    dois: Sequence[str] = (),
    pmids: Sequence[str] = (),
    max_workers: int = 4,
    *,
    fallback: bool = False,
) -> tuple[
    dict[str, tuple[PaperRecord | None, HttpResponse]],
    dict[str, tuple[PaperRecord | None, HttpResponse]],
]:
    """Batch-fetch Europe PMC core records for DOIs and PMIDs using OR queries.

    Returns (by_doi_lower, by_pmid). Definitive misses from complete HTTP 200 pages
    are stored as (None, resp) so callers avoid redundant single-item re-queries.
    """
    terms: list[tuple[str, str, str]] = []
    seen_dois: set[str] = set()
    for d in dois:
        low = d.lower()
        if low not in seen_dois:
            seen_dois.add(low)
            terms.append(("doi", low, f'DOI:"{d}"'))
    seen_pmids: set[str] = set()
    for p in pmids:
        if p not in seen_pmids:
            seen_pmids.add(p)
            terms.append(("pmid", p, f"(EXT_ID:{p} AND SRC:MED)"))

    if not terms:
        return {}, {}

    chunks = [terms[i : i + EPMC_BATCH_SIZE] for i in range(0, len(terms), EPMC_BATCH_SIZE)]

    def _fetch_chunk(
        chunk: list[tuple[str, str, str]],
    ) -> tuple[
        dict[str, tuple[PaperRecord | None, HttpResponse]],
        dict[str, tuple[PaperRecord | None, HttpResponse]],
    ]:
        page_size = len(chunk) + 10
        query = " OR ".join(t[2] for t in chunk)
        url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urllib.parse.urlencode(
            {"query": query, "format": "json", "resultType": "core", "pageSize": str(page_size)}
        )
        if fallback:
            resp = http.get_fallback(url, headers={"Accept": "application/json"})
        else:
            resp = http.get(url, headers={"Accept": "application/json"})
        if resp.status != 200:
            return {}, {}
        try:
            payload = json.loads(resp.body)
            results = payload.get("resultList", {}).get("result", [])
        except json.JSONDecodeError:
            return {}, {}

        c_doi: dict[str, tuple[PaperRecord | None, HttpResponse]] = {}
        c_pmid: dict[str, tuple[PaperRecord | None, HttpResponse]] = {}
        for item in results:
            if not isinstance(item, dict):
                continue
            rec = parse_epmc_result(item)
            if rec.doi:
                c_doi.setdefault(rec.doi.lower(), (rec, resp))
            if rec.pmid:
                c_pmid.setdefault(rec.pmid, (rec, resp))

        # Only record definitive misses when the response wasn't truncated by pageSize
        if len(results) < page_size:
            for kind, key, _ in chunk:
                if kind == "doi" and key not in c_doi:
                    c_doi[key] = (None, resp)
                elif kind == "pmid" and key not in c_pmid:
                    c_pmid[key] = (None, resp)
        return c_doi, c_pmid

    by_doi: dict[str, tuple[PaperRecord | None, HttpResponse]] = {}
    by_pmid: dict[str, tuple[PaperRecord | None, HttpResponse]] = {}
    if len(chunks) == 1:
        c_doi, c_pmid = _fetch_chunk(chunks[0])
        by_doi.update(c_doi)
        by_pmid.update(c_pmid)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(chunks))) as pool:
            for c_doi, c_pmid in pool.map(_fetch_chunk, chunks):
                by_doi.update(c_doi)
                by_pmid.update(c_pmid)
    return by_doi, by_pmid
