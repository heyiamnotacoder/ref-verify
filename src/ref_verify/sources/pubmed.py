"""NCBI PubMed E-utilities: XML parsing, efetch/esearch by PMID or DOI, and title search."""

from __future__ import annotations

import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from typing import Any

from ref_verify.http_client import HttpClient, HttpResponse
from ref_verify.models import Author, PaperRecord
from ref_verify.text import parse_year_int, strip_tags, title_similarity

PUBMED_BATCH_SIZE = 100


def parse_pubmed_article_node(article_node: ET.Element) -> PaperRecord:
    rec = PaperRecord(sources=["pubmed"])

    pmid_el = article_node.find(".//MedlineCitation/PMID")
    if pmid_el is not None and pmid_el.text:
        rec.pmid = pmid_el.text.strip()

    for aid in article_node.findall(".//PubmedData/ArticleIdList/ArticleId"):
        id_type = (aid.attrib.get("IdType") or "").lower()
        val = (aid.text or "").strip()
        if id_type == "doi" and val:
            rec.doi = val
        elif id_type == "pmc" and val:
            rec.pmcid = val

    title_el = article_node.find(".//Article/ArticleTitle")
    if title_el is not None:
        rec.title = re.sub(r"\s+", " ", "".join(title_el.itertext())).strip()
        # PubMed often appends a trailing period to ArticleTitle
        if rec.title.endswith(".") and not rec.title.endswith(".."):
            rec.title = rec.title[:-1].strip()

    # Structured abstract
    abs_parts: list[str] = []
    for ab in article_node.findall(".//Article/Abstract/AbstractText"):
        label = (ab.attrib.get("Label") or "").strip()
        txt = re.sub(r"\s+", " ", "".join(ab.itertext())).strip()
        if not txt:
            continue
        if label:
            abs_parts.append(f"{label}: {txt}")
        else:
            abs_parts.append(txt)
    if abs_parts:
        rec.abstract = "\n".join(abs_parts)

    # Authors
    for au in article_node.findall(".//Article/AuthorList/Author"):
        collective = au.findtext("CollectiveName")
        if collective and collective.strip():
            rec.authors.append(Author(family="", collective=collective.strip()))
            continue
        last = (au.findtext("LastName") or "").strip()
        fore = (au.findtext("ForeName") or "").strip()
        inits = (au.findtext("Initials") or "").strip()
        if last:
            rec.authors.append(Author(family=last, given=fore, initials=inits))

    # Journal
    j_title = article_node.findtext(".//Article/Journal/Title")
    j_iso = article_node.findtext(".//Article/Journal/ISOAbbreviation")
    j_ta = article_node.findtext(".//MedlineCitation/MedlineJournalInfo/MedlineTA")
    if j_title:
        rec.journal_full = j_title.strip()
    if j_ta:
        rec.journal_abbrev = j_ta.strip()
    elif j_iso:
        rec.journal_abbrev = j_iso.strip()

    # Years: journal PubDate (print/issue) & ArticleDate[@DateType='Electronic'] (epub)
    pub_year = article_node.findtext(".//Article/Journal/JournalIssue/PubDate/Year")
    medline_date = article_node.findtext(".//Article/Journal/JournalIssue/PubDate/MedlineDate")
    rec.published_print = parse_year_int(pub_year) or parse_year_int(medline_date)

    for ad in article_node.findall(".//Article/ArticleDate"):
        if (ad.attrib.get("DateType") or "").lower() == "electronic":
            ey = parse_year_int(ad.findtext("Year"))
            if ey:
                rec.published_online = ey
                break
    rec.issued_year = rec.published_print or rec.published_online

    # Publication types (retraction detection!)
    for pt in article_node.findall(".//Article/PublicationTypeList/PublicationType"):
        pt_text = (pt.text or "").strip()
        pt_low = pt_text.lower()
        if pt_low == "retracted publication":
            rec.retracted = True
            rec.retraction_reasons.append("PubMed PublicationType: Retracted Publication")
        elif pt_low == "retraction of publication":
            rec.is_retraction_notice = True
        elif "expression of concern" in pt_low:
            rec.expression_of_concern.append(f"PubMed PublicationType: {pt_text}")

    # CommentsCorrectionsList
    for cc in article_node.findall(".//MedlineCitation/CommentsCorrectionsList/CommentsCorrections"):
        rtype = (cc.attrib.get("RefType") or "").strip()
        ref_pmid = (cc.findtext("PMID") or "").strip()
        ref_src = (cc.findtext("RefSource") or "").strip()
        label = f"PMID:{ref_pmid} ({ref_src})" if ref_pmid else ref_src
        if rtype == "RetractionIn":
            rec.retracted = True
            rec.retraction_reasons.append(f"PubMed RetractionIn: {label}")
            if ref_pmid:
                rec.retraction_notice_ids.append(f"PMID:{ref_pmid}")
        elif rtype == "RetractionOf":
            rec.is_retraction_notice = True
        elif rtype == "ExpressionOfConcernIn":
            rec.expression_of_concern.append(f"PubMed ExpressionOfConcernIn: {label}")
        elif rtype == "ErratumIn":
            rec.correction_notices.append(f"PubMed ErratumIn: {label}")

    return rec


def parse_pubmed_xml(xml_text: str) -> PaperRecord | None:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    article_node = root.find(".//PubmedArticle")
    if article_node is None:
        return None

    return parse_pubmed_article_node(article_node)


def _parse_pubmed_xml_many_checked(xml_text: str) -> tuple[dict[str, PaperRecord], bool]:
    """Parse all <PubmedArticle> elements, returning (records_by_pmid, xml_valid)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}, False
    out: dict[str, PaperRecord] = {}
    for article_node in root.findall(".//PubmedArticle"):
        rec = parse_pubmed_article_node(article_node)
        if rec.pmid:
            out[rec.pmid] = rec
    return out, True


def parse_pubmed_xml_many(xml_text: str) -> dict[str, PaperRecord]:
    """Parse all <PubmedArticle> elements in a batched efetch XML response, keyed by PMID."""
    parsed, _ = _parse_pubmed_xml_many_checked(xml_text)
    return parsed


def fetch_pubmed_pmid(http: HttpClient, pmid: str) -> tuple[PaperRecord | None, HttpResponse]:
    url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id={urllib.parse.quote(pmid)}&retmode=xml"
    )
    resp = http.get(url)
    if resp.status != 200:
        return None, resp
    rec = parse_pubmed_xml(resp.body)
    return rec, resp


def fetch_pubmed_pmids_batch(
    http: HttpClient, pmids: Sequence[str]
) -> tuple[dict[str, tuple[PaperRecord | None, HttpResponse]], bool]:
    """Batch-fetch up to PUBMED_BATCH_SIZE PMIDs per single efetch XML request."""
    unique = list(dict.fromkeys(p for p in pmids if p))
    if not unique:
        return {}, True
    if len(unique) == 1:
        rec, resp = fetch_pubmed_pmid(http, unique[0])
        return {unique[0]: (rec, resp)}, resp.status == 200

    out: dict[str, tuple[PaperRecord | None, HttpResponse]] = {}
    all_ok = True
    for i in range(0, len(unique), PUBMED_BATCH_SIZE):
        chunk = unique[i : i + PUBMED_BATCH_SIZE]
        id_str = ",".join(chunk)
        url = (
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?"
            f"db=pubmed&id={urllib.parse.quote(id_str, safe=',')}&retmode=xml"
        )
        resp = http.get(url)
        if resp.status != 200:
            all_ok = False
            continue
        parsed, xml_ok = _parse_pubmed_xml_many_checked(resp.body)
        if not xml_ok:
            # Corrupt/truncated XML: do not cache false misses so single-PMID fallback can retry.
            all_ok = False
            continue
        for pid in chunk:
            out[pid] = (parsed.get(pid), resp)
    return out, all_ok


ESEARCH_DOI_CHUNK = 25


def _esearch(http: HttpClient, term: str, retmax: int) -> tuple[list[str], int] | None:
    """Run one PubMed esearch; returns (ids, total_count) or None on HTTP/JSON failure."""
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urllib.parse.urlencode(
        {"db": "pubmed", "term": term, "retmode": "json", "retmax": str(retmax)}
    )
    resp = http.get(url)
    if resp.status != 200:
        return None
    try:
        es_res = json.loads(resp.body).get("esearchresult", {})
        ids = [str(i) for i in es_res.get("idlist", [])]
        return ids, int(es_res.get("count", len(ids)) or 0)
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return None


def pubmed_esearch_by_doi(http: HttpClient, doi: str) -> str | None:
    found = _esearch(http, f'"{doi}"[DOI]', retmax=2)
    return found[0][0] if found and found[0] else None


def pubmed_esearch_by_dois_batch(http: HttpClient, dois: Sequence[str]) -> tuple[list[str], list[str]]:
    """Batch-discover PMIDs for DOIs via OR-combined PubMed esearch queries.

    Returns (pmids, checked_dois): ``checked_dois`` lists only DOIs from chunks that answered completely,
    so a failed or truncated chunk sends just its own DOIs back to per-citation esearch.
    """
    unique = list(dict.fromkeys(d for d in dois if d))
    pmids: list[str] = []
    checked: list[str] = []
    for i in range(0, len(unique), ESEARCH_DOI_CHUNK):
        chunk = unique[i : i + ESEARCH_DOI_CHUNK]
        retmax = len(chunk) * 2
        found = _esearch(http, " OR ".join(f'"{d}"[DOI]' for d in chunk), retmax)
        if found is None:
            continue
        ids, total_count = found
        pmids.extend(ids)
        if total_count <= retmax:
            checked.extend(chunk)
    return list(dict.fromkeys(pmids)), checked


def search_pubmed_by_title(http: HttpClient, title: str, retmax: int = 3) -> list[dict[str, Any]]:
    # Clean special chars that break PubMed boolean queries
    clean_q = re.sub(r"[\[\]():\"']", " ", title)
    clean_q = re.sub(r"\s+", " ", clean_q).strip()
    if not clean_q:
        return []
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urllib.parse.urlencode(
        {"db": "pubmed", "term": f"{clean_q}[Title]", "retmode": "json", "retmax": str(retmax)}
    )
    resp = http.get(url)
    if resp.status != 200:
        return []
    try:
        ids = json.loads(resp.body).get("esearchresult", {}).get("idlist", [])
    except json.JSONDecodeError:
        return []
    if not ids:
        return []

    sum_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?" + urllib.parse.urlencode(
        {"db": "pubmed", "id": ",".join(ids), "retmode": "json"}
    )
    sresp = http.get(sum_url)
    if sresp.status != 200:
        return []
    try:
        sdata = json.loads(sresp.body).get("result", {})
    except json.JSONDecodeError:
        return []

    out: list[dict[str, Any]] = []
    for pid in ids:
        item = sdata.get(str(pid))
        if not isinstance(item, dict):
            continue
        t = strip_tags(str(item.get("title", ""))).rstrip(".")
        if not t:
            continue
        doi = None
        for aid in item.get("articleids") or []:
            if isinstance(aid, dict) and aid.get("idtype") == "doi":
                doi = aid.get("value")
                break
        authors = item.get("authors") or []
        first_au = authors[0].get("name") if authors and isinstance(authors[0], dict) else None
        out.append(
            {
                "source": "pubmed",
                "doi": doi,
                "pmid": str(pid),
                "title": t,
                "first_author": first_au,
                "year": parse_year_int(item.get("pubdate")),
                "journal": item.get("source"),
                "title_similarity": round(title_similarity(title, t), 3),
            }
        )
    return out
