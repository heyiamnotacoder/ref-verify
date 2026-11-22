"""Open Access JATS full text: NCBI PMC first, Europe PMC fallback."""

from __future__ import annotations

import concurrent.futures
import re
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Sequence

from ref_verify.http_client import HttpClient, is_transient_http
from ref_verify.models import FulltextResult, HttpResponse

PMC_FULLTEXT_BATCH_SIZE = 12


def extract_paragraphs_from_article_node(node: ET.Element, source_label: str) -> tuple[list[str], str | None, bool]:
    body = node.find(".//body")
    if body is None:
        return [], f"{source_label} XML has no <body> element (likely not licensed for OA full-text XML)", True

    paragraphs: list[str] = []
    for p in body.findall(".//p"):
        txt = re.sub(r"\s+", " ", "".join(p.itertext())).strip()
        if len(txt) >= 30:
            paragraphs.append(txt)
    if not paragraphs:
        return [], f"{source_label} XML <body> contained no text paragraphs", False
    return paragraphs, None, False


def _parse_jats_body_paragraphs(xml_body: str, source_label: str) -> tuple[list[str], str | None, bool]:
    """Extract body paragraphs from JATS full-text XML (Europe PMC or NCBI PMC)."""
    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError as exc:
        return [], f"Failed to parse {source_label} XML: {exc}", False
    return extract_paragraphs_from_article_node(root, source_label)


def parse_pmc_xml_many(xml_body: str) -> dict[str, FulltextResult]:
    """Parse multiple <article> elements from a batched NCBI PMC efetch XML response, keyed by PMCID."""
    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError:
        return {}

    articles = root.findall("./article")
    if not articles and root.tag == "article":
        articles = [root]

    out: dict[str, FulltextResult] = {}
    for art in articles:
        pmcid: str | None = None
        for aid in art.findall(".//front//article-id"):
            ptype = (aid.attrib.get("pub-id-type") or "").lower()
            if ptype in ("pmc", "pmcid") and aid.text:
                raw_id = aid.text.strip().upper()
                pmcid = raw_id if raw_id.startswith("PMC") else f"PMC{raw_id}"
                break
        if not pmcid:
            continue
        paragraphs, err, missing_body = extract_paragraphs_from_article_node(art, "NCBI PMC efetch")
        if err or not paragraphs:
            out[pmcid] = FulltextResult(
                error=err or "NCBI PMC efetch returned empty body",
                missing_body=missing_body,
            )
        else:
            out[pmcid] = FulltextResult(paragraphs=paragraphs, source="NCBI PMC")
    return out


def _format_http_failure(source_label: str, resp: HttpResponse) -> str:
    if resp.status == 0:
        detail = resp.error or "network/timeout error"
        return f"{source_label} request failed ({detail})"
    return f"{source_label} returned HTTP {resp.status}"


def _fetch_jats_fulltext(
    http: HttpClient,
    url: str,
    source_label: str,
    source_name: str,
    *,
    fallback: bool = False,
    primary_failed_transient: bool = False,
) -> FulltextResult:
    if fallback:
        resp = http.get_fallback(url, primary_failed_transient=primary_failed_transient)
    else:
        resp = http.get(url)
    if resp.status != 200:
        return FulltextResult(error=_format_http_failure(source_label, resp), is_transient=is_transient_http(resp))
    paragraphs, err, missing_body = _parse_jats_body_paragraphs(resp.body, source_label)
    if err or not paragraphs:
        return FulltextResult(error=err or f"{source_label} returned empty body", missing_body=missing_body)
    return FulltextResult(paragraphs=paragraphs, source=source_name)


def fetch_epmc_fulltext_paragraphs(http: HttpClient, pmcid: str) -> tuple[list[str], str | None]:
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{urllib.parse.quote(pmcid)}/fullTextXML"
    res = _fetch_jats_fulltext(http, url, "Europe PMC fullTextXML", "Europe PMC")
    return res.paragraphs, res.error


def _resolve_fulltext_with_epmc_fallback(
    http: HttpClient,
    pmcid: str,
    ncbi_res: FulltextResult,
    is_open_access: bool | None = None,
) -> FulltextResult:
    """Resolve OA full-text when NCBI PMC did not yield body paragraphs."""
    if ncbi_res.paragraphs:
        return ncbi_res

    # Skip Europe PMC fullTextXML if Europe PMC already explicitly marked the article non-OA
    # and NCBI PMC returned valid XML without a <body>.
    if is_open_access is False and ncbi_res.missing_body and not ncbi_res.is_transient:
        return FulltextResult(
            error=f"{ncbi_res.error}; Europe PMC marks article non-open-access",
            is_transient=False,
            missing_body=True,
        )

    epmc_url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{urllib.parse.quote(pmcid)}/fullTextXML"
    epmc_res = _fetch_jats_fulltext(
        http,
        epmc_url,
        "Europe PMC fullTextXML",
        "Europe PMC",
        fallback=True,
        primary_failed_transient=ncbi_res.is_transient,
    )
    if epmc_res.paragraphs:
        return epmc_res

    errors = [e for e in (epmc_res.error, ncbi_res.error) if e]
    return FulltextResult(
        error="; ".join(errors) if errors else "empty body",
        is_transient=epmc_res.is_transient or ncbi_res.is_transient,
        missing_body=epmc_res.missing_body or ncbi_res.missing_body,
    )


def fetch_oa_fulltext_paragraphs(http: HttpClient, pmcid: str, is_open_access: bool | None = None) -> FulltextResult:
    """Fetch OA JATS full-text paragraphs from NCBI PMC first, falling back to Europe PMC."""
    ncbi_url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id={urllib.parse.quote(pmcid)}&retmode=xml"
    )
    ncbi_res = _fetch_jats_fulltext(http, ncbi_url, "NCBI PMC efetch", "NCBI PMC")
    return _resolve_fulltext_with_epmc_fallback(http, pmcid, ncbi_res, is_open_access=is_open_access)


def fetch_oa_fulltext_batch(
    http: HttpClient,
    pmcids: Sequence[str],
    oa_flags: dict[str, bool | None] | None = None,
    max_workers: int = 4,
) -> dict[str, FulltextResult]:
    """Batch-fetch OA full-text JATS XML via NCBI PMC efetch, with concurrent Europe PMC fallback."""
    unique = list(dict.fromkeys(p.strip().upper() for p in pmcids if p and p.strip()))
    if not unique:
        return {}
    if len(unique) == 1:
        pid = unique[0]
        return {pid: fetch_oa_fulltext_paragraphs(http, pid, is_open_access=(oa_flags or {}).get(pid))}

    out: dict[str, FulltextResult] = {}
    need_fallback: list[tuple[str, FulltextResult]] = []

    for i in range(0, len(unique), PMC_FULLTEXT_BATCH_SIZE):
        chunk = unique[i : i + PMC_FULLTEXT_BATCH_SIZE]
        id_str = ",".join(chunk)
        ncbi_url = (
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?"
            f"db=pmc&id={urllib.parse.quote(id_str, safe=',')}&retmode=xml"
        )
        resp = http.get(ncbi_url)
        if resp.status != 200:
            err_res = FulltextResult(
                error=_format_http_failure("NCBI PMC efetch", resp),
                is_transient=is_transient_http(resp),
            )
            for pid in chunk:
                need_fallback.append((pid, err_res))
            continue

        parsed = parse_pmc_xml_many(resp.body)
        for pid in chunk:
            ncbi_res = parsed.get(pid) or FulltextResult(error="NCBI PMC efetch returned no article for PMCID")
            if ncbi_res.paragraphs:
                out[pid] = ncbi_res
            else:
                need_fallback.append((pid, ncbi_res))

    if need_fallback:

        def _fallback_one(item: tuple[str, FulltextResult]) -> tuple[str, FulltextResult]:
            pid, ncbi_res = item
            return pid, _resolve_fulltext_with_epmc_fallback(
                http, pid, ncbi_res, is_open_access=(oa_flags or {}).get(pid)
            )

        if len(need_fallback) == 1:
            pid, res = _fallback_one(need_fallback[0])
            out[pid] = res
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(need_fallback))) as pool:
                for pid, res in pool.map(_fallback_one, need_fallback):
                    out[pid] = res

    return out
