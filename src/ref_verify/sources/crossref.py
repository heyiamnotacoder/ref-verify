"""Crossref REST API: work parsing, single/batch DOI lookup, and title search."""

from __future__ import annotations

import concurrent.futures
import json
import re
import urllib.parse
from collections.abc import Sequence
from typing import Any

from ref_verify.http_client import HttpClient
from ref_verify.models import Author, HttpResponse, PaperRecord
from ref_verify.text import strip_tags, title_similarity

CROSSREF_SELECT_FIELDS = (
    "DOI,title,author,published-print,published-online,issued,published,"
    "container-title,short-container-title,abstract,update-to,updated-by,"
    "relation,is-referenced-by-count"
)


CROSSREF_BATCH_SIZE = 25


def _extract_crossref_year(item: dict[str, Any], key: str) -> int | None:
    part = item.get(key)
    if isinstance(part, dict):
        dp = part.get("date-parts")
        if isinstance(dp, list) and dp and isinstance(dp[0], list) and dp[0]:
            try:
                return int(dp[0][0])
            except (TypeError, ValueError):
                return None
    return None


def parse_crossref_work(msg: dict[str, Any]) -> PaperRecord:
    rec = PaperRecord(sources=["crossref"])
    doi = msg.get("DOI")
    if doi:
        rec.doi = str(doi).strip()

    titles = msg.get("title") or []
    if isinstance(titles, list) and titles:
        rec.title = strip_tags(str(titles[0]))

    for a in msg.get("author") or []:
        if not isinstance(a, dict):
            continue
        if a.get("name") and not a.get("family"):
            rec.authors.append(Author(family="", collective=strip_tags(str(a["name"]))))
        else:
            family = strip_tags(str(a.get("family", "")))
            given = strip_tags(str(a.get("given", "")))
            if family:
                rec.authors.append(Author(family=family, given=given))

    rec.published_print = _extract_crossref_year(msg, "published-print")
    rec.published_online = _extract_crossref_year(msg, "published-online")
    rec.issued_year = _extract_crossref_year(msg, "issued") or _extract_crossref_year(msg, "published")

    containers = msg.get("container-title") or []
    if isinstance(containers, list) and containers:
        rec.journal_full = strip_tags(str(containers[0]))
    short_containers = msg.get("short-container-title") or []
    if isinstance(short_containers, list) and short_containers:
        rec.journal_abbrev = strip_tags(str(short_containers[0]))

    if msg.get("abstract"):
        rec.abstract = strip_tags(str(msg["abstract"]))

    # Retraction / Correction detection in Crossref
    # 1. Is this work itself a retraction notice?
    for upd in msg.get("update-to") or []:
        if not isinstance(upd, dict):
            continue
        utype = str(upd.get("type", "")).lower()
        ulabel = str(upd.get("label", "")).lower()
        if "retract" in utype or "retract" in ulabel:
            rec.is_retraction_notice = True

    # 2. Has this work been retracted or corrected by another work?
    for upd in msg.get("updated-by") or []:
        if not isinstance(upd, dict):
            continue
        utype = str(upd.get("type", "")).lower()
        ulabel = str(upd.get("label", "")).lower()
        target_doi = str(upd.get("DOI", "")).strip()
        if "retract" in utype or "retract" in ulabel:
            rec.retracted = True
            rec.retraction_reasons.append(f"Crossref updated-by ({upd.get('label') or upd.get('type')})")
            if target_doi:
                rec.retraction_notice_ids.append(f"DOI:{target_doi}")
        elif "concern" in utype or "concern" in ulabel:
            rec.expression_of_concern.append(
                f"Crossref updated-by ({upd.get('label') or upd.get('type')}): {target_doi}"
            )
        elif "correct" in utype or "errat" in utype or "correct" in ulabel or "errat" in ulabel:
            rec.correction_notices.append(f"Crossref {upd.get('label') or upd.get('type')}: DOI:{target_doi}")

    relation = msg.get("relation") or {}
    if isinstance(relation, dict):
        for rel_key, rel_items in relation.items():
            rk_low = rel_key.lower()
            if not isinstance(rel_items, list):
                continue
            for item in rel_items:
                if not isinstance(item, dict):
                    continue
                rid = str(item.get("id", "")).strip()
                if "is-retracted-by" in rk_low or ("retract" in rk_low and "by" in rk_low):
                    rec.retracted = True
                    rec.retraction_reasons.append(f"Crossref relation '{rel_key}'")
                    if rid:
                        rec.retraction_notice_ids.append(rid)
                elif "concern" in rk_low:
                    rec.expression_of_concern.append(f"Crossref relation '{rel_key}': {rid}")
                elif "is-corrected-by" in rk_low:
                    rec.correction_notices.append(f"Crossref relation '{rel_key}': {rid}")

    if rec.title and re.match(r"^(?:\[?\s*retracted\b|retraction\s+note\s*:)", rec.title, flags=re.IGNORECASE):
        if re.match(r"^\[?\s*retracted\b", rec.title, flags=re.IGNORECASE):
            rec.retracted = True
            rec.retraction_reasons.append("Title prefixed with RETRACTED")
        else:
            rec.is_retraction_notice = True

    return rec


def fetch_crossref_doi(http: HttpClient, doi: str) -> tuple[PaperRecord | None, HttpResponse]:
    quoted = urllib.parse.quote(doi, safe="/")
    url = f"https://api.crossref.org/works/{quoted}"
    resp = http.get(url, headers={"Accept": "application/json"})
    if resp.status != 200:
        return None, resp
    try:
        data = json.loads(resp.body)
        msg = data.get("message")
        if not isinstance(msg, dict):
            return None, resp
        return parse_crossref_work(msg), resp
    except json.JSONDecodeError as exc:
        resp.error = f"Invalid JSON from Crossref: {exc}"
        return None, resp


def fetch_crossref_dois_batch(
    http: HttpClient, dois: Sequence[str], max_workers: int = 4
) -> tuple[dict[str, tuple[PaperRecord, HttpResponse]], bool]:
    """Batch-fetch Crossref works via filter=doi:...&select=... in chunks.

    Returns (doi_lower -> (PaperRecord, HttpResponse), all_chunks_ok).
    DOIs containing commas (Crossref filter delimiter) are skipped for single-DOI fallback.
    """
    unique: list[str] = []
    seen: set[str] = set()
    for d in dois:
        low = d.lower()
        if "," in d or low in seen:
            continue
        seen.add(low)
        unique.append(d)

    if not unique:
        return {}, True

    chunks = [unique[i : i + CROSSREF_BATCH_SIZE] for i in range(0, len(unique), CROSSREF_BATCH_SIZE)]

    def _fetch_chunk(chunk: list[str]) -> tuple[dict[str, tuple[PaperRecord, HttpResponse]], bool]:
        filt = ",".join(f"doi:{d}" for d in chunk)
        params = {
            "filter": filt,
            "rows": str(len(chunk) + 5),
            "select": CROSSREF_SELECT_FIELDS,
        }
        url = f"https://api.crossref.org/works?{urllib.parse.urlencode(params)}"
        resp = http.get(url, headers={"Accept": "application/json"})
        if resp.status != 200:
            return {}, False
        try:
            items = json.loads(resp.body).get("message", {}).get("items", [])
        except json.JSONDecodeError:
            return {}, False
        out: dict[str, tuple[PaperRecord, HttpResponse]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            rec = parse_crossref_work(item)
            if rec.doi:
                out[rec.doi.lower()] = (rec, resp)
        return out, True

    found: dict[str, tuple[PaperRecord, HttpResponse]] = {}
    all_ok = True
    if len(chunks) == 1:
        chunk_found, ok = _fetch_chunk(chunks[0])
        found.update(chunk_found)
        all_ok = ok
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(chunks))) as pool:
            for chunk_found, ok in pool.map(_fetch_chunk, chunks):
                found.update(chunk_found)
                if not ok:
                    all_ok = False
    return found, all_ok


def search_crossref_by_title(
    http: HttpClient, title: str, author: str | None = None, rows: int = 8
) -> tuple[list[dict[str, Any]], bool]:
    """(candidates, whether Crossref answered): an outage must not read as "no such paper"."""
    params = {"query.title": title, "rows": str(rows), "select": CROSSREF_SELECT_FIELDS}
    if author:
        params["query.author"] = author
    url = f"https://api.crossref.org/works?{urllib.parse.urlencode(params)}"
    resp = http.get(url, headers={"Accept": "application/json"})
    if resp.status != 200:
        return [], False
    try:
        items = json.loads(resp.body).get("message", {}).get("items", [])
    except json.JSONDecodeError:
        return [], False

    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rec = parse_crossref_work(item)
        if not rec.title:
            continue
        sim = title_similarity(title, rec.title)
        cited_by = item.get("is-referenced-by-count")
        cited_by_int = int(cited_by) if isinstance(cited_by, int) else 0
        out.append(
            {
                "source": "crossref",
                "doi": rec.doi,
                "pmid": None,
                "title": rec.title,
                "first_author": rec.authors[0].display if rec.authors else None,
                "year": rec.best_year(),
                "journal": rec.journal_abbrev or rec.journal_full,
                "title_similarity": round(sim, 3),
                "cited_by_count": cited_by_int,
            }
        )
    return out, True
