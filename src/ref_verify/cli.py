#!/usr/bin/env python3
"""ref-verify: Live scientific reference & claim verifier with pinpointing diagnostics.

Zero external dependencies (pure Python 3 standard library).
Authoritative lookups: Crossref REST API, NCBI PubMed E-utilities, Europe PMC REST API.
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from ref_verify import __version__
except ImportError:
    __version__ = "0.1.0"

USER_AGENT = f"ref-verify/{__version__} (scientific-citation-verifier; https://github.com/heyiamnotacoder/ref-verify)"
DEFAULT_TIMEOUT = 15.0

SEVERITY_ORDER = {"CRITICAL": 0, "ERROR": 1, "WARNING": 2, "INFO": 3}

# Common biomedical journal aliases (normalized lowercase alnum/space -> canonical key)
JOURNAL_ALIASES: Dict[str, Tuple[str, ...]] = {
    "nejm": ("new england journal of medicine", "n engl j med", "nejm"),
    "jama": ("jama", "journal of the american medical association", "j am med assoc"),
    "lancet": ("lancet", "the lancet"),
    "bmj": ("bmj", "british medical journal", "bmj clinical research ed"),
    "ann_intern_med": ("annals of internal medicine", "ann intern med"),
    "circulation": ("circulation",),
    "ehj": ("european heart journal", "eur heart j"),
    "jacc": ("journal of the american college of cardiology", "j am coll cardiol", "jacc"),
    "nature": ("nature",),
    "nat_med": ("nature medicine", "nat med"),
    "science": ("science",),
    "cell": ("cell",),
    "pnas": (
        "pnas",
        "proceedings of the national academy of sciences",
        "proceedings of the national academy of sciences of the united states of america",
        "proc natl acad sci u s a",
        "proc natl acad sci usa",
    ),
    "plos_one": ("plos one", "plos 1"),
    "plos_med": ("plos medicine", "plos med"),
    "cochrane": ("cochrane database of systematic reviews", "cochrane database syst rev"),
    "diabetes_care": ("diabetes care",),
    "jama_cardiol": ("jama cardiology", "jama cardiol"),
    "jama_intern_med": ("jama internal medicine", "jama intern med"),
    "jama_oncol": ("jama oncology", "jama oncol"),
    "lancet_oncol": ("lancet oncology", "the lancet oncology", "lancet oncol"),
    "lancet_diab": (
        "lancet diabetes & endocrinology",
        "the lancet diabetes & endocrinology",
        "lancet diabetes endocrinol",
    ),
    "jco": ("journal of clinical oncology", "j clin oncol", "jco"),
    "blood": ("blood",),
    "gastroenterology": ("gastroenterology",),
    "hepatology": ("hepatology",),
    "chest": ("chest",),
    "neurology": ("neurology",),
    "radiology": ("radiology",),
    "pediatrics": ("pediatrics",),
    "anesthesiology": ("anesthesiology",),
    "crit_care_med": ("critical care medicine", "crit care med"),
    "am_j_med": ("american journal of medicine", "am j med"),
    "br_j_clin_pharmacol": ("british journal of clinical pharmacology", "br j clin pharmacol"),
    "clin_pharmacol_ther": ("clinical pharmacology and therapeutics", "clin pharmacol ther"),
    "eur_j_clin_pharmacol": ("european journal of clinical pharmacology", "eur j clin pharmacol"),
    "drug_saf": ("drug safety", "drug saf"),
    "pharmacoepidemiol_drug_saf": (
        "pharmacoepidemiology and drug safety",
        "pharmacoepidemiol drug saf",
    ),
}

PHARMA_STEMS = (
    "flozin",
    "glutide",
    "statin",
    "pril",
    "sartan",
    "olol",
    "mab",
    "nib",
    "vir",
    "cillin",
    "mycin",
    "prazole",
    "dipine",
    "xaban",
    "gatran",
    "gliptin",
    "tidine",
    "setron",
    "triptan",
    "zepam",
    "zolam",
    "caine",
    "asone",
    "olone",
    "lukast",
    "parin",
    "plase",
    "vaptan",
    "tadine",
    "conazole",
    "cycline",
    "floxacin",
    "platin",
    "taxel",
    "rubicin",
    "mustine",
    "zumab",
    "ximab",
    "umab",
)

STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "were",
    "was",
    "are",
    "have",
    "has",
    "had",
    "been",
    "not",
    "but",
    "into",
    "over",
    "than",
    "also",
    "their",
    "there",
    "these",
    "those",
    "which",
    "while",
    "where",
    "when",
    "what",
    "about",
    "after",
    "before",
    "between",
    "during",
    "through",
    "under",
    "among",
    "versus",
    "compared",
    "patient",
    "patients",
    "study",
    "trial",
    "results",
    "showed",
    "found",
    "reported",
    "associated",
    "significant",
    "significantly",
    "reduced",
    "increased",
    "lower",
    "higher",
    "group",
    "groups",
    "primary",
    "outcome",
    "outcomes",
    "risk",
    "rate",
    "rates",
    "median",
    "mean",
    "years",
    "months",
    "weeks",
    "days",
    "total",
    "overall",
    "analysis",
    "data",
}

SURNAME_PARTICLES = {
    "de",
    "del",
    "della",
    "der",
    "den",
    "des",
    "di",
    "da",
    "dos",
    "du",
    "la",
    "le",
    "van",
    "von",
    "ter",
    "ten",
    "st",
    "saint",
    "al",
    "el",
    "bin",
}


@dataclass
class Diagnostic:
    code: str
    severity: str  # CRITICAL | ERROR | WARNING | INFO
    message: str
    expected: str = ""
    actual: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    hint: str = ""
    fix: str = ""


@dataclass
class Author:
    family: str
    given: str = ""
    initials: str = ""
    collective: str = ""

    @property
    def display(self) -> str:
        if self.collective:
            return self.collective
        if self.initials:
            return f"{self.family} {self.initials}".strip()
        if self.given:
            inits = "".join(p[0] for p in re.split(r"[\s-]+", self.given) if p)
            return f"{self.family} {inits}".strip()
        return self.family


@dataclass
class PaperRecord:
    doi: Optional[str] = None
    pmid: Optional[str] = None
    pmcid: Optional[str] = None
    title: Optional[str] = None
    authors: List[Author] = field(default_factory=list)
    published_print: Optional[int] = None
    published_online: Optional[int] = None
    issued_year: Optional[int] = None
    journal_full: Optional[str] = None
    journal_abbrev: Optional[str] = None
    abstract: Optional[str] = None
    is_open_access: Optional[bool] = None
    retracted: bool = False
    retraction_reasons: List[str] = field(default_factory=list)
    retraction_notice_ids: List[str] = field(default_factory=list)
    is_retraction_notice: bool = False
    expression_of_concern: List[str] = field(default_factory=list)
    correction_notices: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)

    def all_years(self) -> List[int]:
        yrs: List[int] = []
        for y in (self.published_print, self.published_online, self.issued_year):
            if y is not None and y not in yrs:
                yrs.append(y)
        return yrs

    def best_year(self) -> Optional[int]:
        return self.published_print or self.issued_year or self.published_online

    def journal_variants(self) -> List[str]:
        out: List[str] = []
        for j in (self.journal_full, self.journal_abbrev):
            if j and j not in out:
                out.append(j)
        return out


@dataclass
class CitationInput:
    doi: Optional[str] = None
    pmid: Optional[str] = None
    title: Optional[str] = None
    author: Optional[str] = None
    year: Optional[str] = None
    journal: Optional[str] = None
    claim: Optional[str] = None
    label: Optional[str] = None
    location: Optional[str] = None
    fulltext: bool = False


@dataclass
class ClaimReport:
    claim: str
    extracted_numbers: List[str] = field(default_factory=list)
    matched_numbers: List[str] = field(default_factory=list)
    missing_numbers: List[str] = field(default_factory=list)
    extracted_keywords: List[str] = field(default_factory=list)
    matched_keywords: List[str] = field(default_factory=list)
    abstract_numbers: List[str] = field(default_factory=list)
    matched_sentences: List[str] = field(default_factory=list)
    fulltext_excerpts: List[str] = field(default_factory=list)
    fulltext_status: str = "not_requested"
    abstract_available: bool = False


@dataclass
class VerificationResult:
    input: CitationInput
    status: str  # PASS | WARN | FAIL
    resolved: Optional[PaperRecord] = None
    diagnostics: List[Diagnostic] = field(default_factory=list)
    claim_report: Optional[ClaimReport] = None
    did_you_mean: List[Dict[str, Any]] = field(default_factory=list)

    def has_severity(self, *severities: str) -> bool:
        return any(d.severity in severities for d in self.diagnostics)


# ---------------------------------------------------------------------------
# Text / XML normalization helpers
# ---------------------------------------------------------------------------


def strip_tags(text: Optional[str]) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def normalize_text(text: str) -> str:
    text = strip_accents(html.unescape(text))
    text = text.replace("\u2010", "-").replace("\u2011", "-").replace("\u2012", "-")
    text = text.replace("\u2013", "-").replace("\u2014", "-").replace("\u2212", "-")
    text = text.replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    # Normalize British/medical middle-dot decimals (e.g. "-81·7%", "P < 0·001")
    text = re.sub(r"(?<=\d)[\u00b7\u2219](?=\d)", ".", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def tokenize_words(text: str) -> List[str]:
    norm = normalize_text(text).lower()
    return re.findall(r"[a-z0-9]+", norm)


def normalize_journal(name: str) -> str:
    norm = normalize_text(name).lower()
    norm = norm.replace("&", " and ")
    norm = re.sub(r"[^a-z0-9\s]", " ", norm)
    norm = re.sub(r"\s+", " ", norm).strip()
    if norm.startswith("the "):
        norm = norm[4:]
    return norm


def journal_alias_group(name: str) -> Optional[str]:
    norm = normalize_journal(name)
    for key, variants in JOURNAL_ALIASES.items():
        for v in variants:
            if norm == normalize_journal(v):
                return key
    return None


def parse_year_int(val: Any) -> Optional[int]:
    if val is None:
        return None
    m = re.search(r"\b(1[5-9]\d{2}|20\d{2}|21\d{2})\b", str(val))
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# DOI / PMID syntax validation
# ---------------------------------------------------------------------------

DOI_CORE_RE = re.compile(r"^10\.\d{4,9}/\S+$")
TRAILING_PUNCT_CHARS = ".,;:'\"<>?!"


def _strip_unbalanced_closing(s: str) -> Tuple[str, str]:
    """Strip trailing unbalanced ), ], } and punctuation characters."""
    removed = []
    cur = s
    pairs = {")": "(", "]": "[", "}": "{"}
    while cur:
        ch = cur[-1]
        if ch in TRAILING_PUNCT_CHARS:
            removed.append(ch)
            cur = cur[:-1]
        elif ch in pairs:
            opener = pairs[ch]
            if cur.count(ch) > cur.count(opener):
                removed.append(ch)
                cur = cur[:-1]
            else:
                break
        else:
            break
    return cur, "".join(reversed(removed))


def validate_doi(raw_doi: str) -> Tuple[Optional[str], Optional[Diagnostic]]:
    """Validate DOI syntax. Returns (canonical_doi_or_cleaned, optional_diagnostic)."""
    original = raw_doi
    s = original.strip()
    if not s:
        return None, Diagnostic(
            code="DOI_SYNTAX_INVALID",
            severity="ERROR",
            message="Empty DOI string provided.",
            expected="10.<4-9 digits>/<suffix> (e.g. 10.1056/NEJMoa1911303)",
            actual=repr(original),
            hint="DOI argument is blank.",
            fix="Provide a valid DOI starting with '10.'.",
        )

    # Strip standard URL / label prefixes cleanly (accepted convention)
    s = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", s, flags=re.IGNORECASE).strip()

    issues: List[str] = []
    candidate = s

    # URL query/fragment junk (e.g. ?via%3Dihub or #sec1)
    if re.search(r"[?#]", candidate):
        junk = re.search(r"([?#].*)$", candidate)
        if junk:
            issues.append(f"stray URL query/fragment '{junk.group(1)}'")
            candidate = candidate[: junk.start()]

    # Embedded whitespace
    if re.search(r"\s", candidate):
        issues.append("contains embedded whitespace")
        candidate = re.sub(r"\s+", "", candidate)

    # Trailing sentence/citation punctuation
    stripped, trailing = _strip_unbalanced_closing(candidate)
    if trailing:
        issues.append(f"trailing punctuation '{trailing}'")
        candidate = stripped

    # Missing '10.' prefix (e.g. '1056/NEJMoa1911303')
    if not candidate.startswith("10."):
        if re.match(r"^\d{4,9}/\S+$", candidate):
            issues.append("missing '10.' prefix")
            candidate = f"10.{candidate}"
        else:
            issues.append("does not start with '10.<registrant>/'")

    # Validate registrant & slash
    if "/" not in candidate:
        issues.append("missing '/' separator between registrant and suffix")
    else:
        prefix, suffix = candidate.split("/", 1)
        if not re.match(r"^10\.\d{4,9}$", prefix):
            if "does not start with '10.<registrant>/'" not in issues:
                issues.append(f"invalid registrant prefix '{prefix}' (expected 10.<4-9 digits>)")
        if not suffix:
            issues.append("empty suffix after '/'")

    cleaned_valid = bool(DOI_CORE_RE.match(candidate))

    if issues:
        issue_str = "; ".join(issues)
        fix_str = (
            f"Replace '{original}' with cleaned DOI '{candidate}' and re-verify."
            if cleaned_valid
            else f"Fix malformed DOI '{original}' to match '10.<4-9 digits>/<suffix>'."
        )
        diag = Diagnostic(
            code="DOI_SYNTAX_INVALID",
            severity="ERROR",
            message=f"Malformed DOI '{original}': {issue_str}.",
            expected="10.<4-9 digits>/<suffix>",
            actual=original,
            details={
                "issues": issues,
                "cleaned_candidate": candidate if cleaned_valid else None,
            },
            hint="Common copy-paste error: trailing sentence punctuation, missing '10.' prefix, spaces, or URL parameters.",
            fix=fix_str,
        )
        return (candidate if cleaned_valid else None), diag

    return candidate, None


def validate_pmid(raw_pmid: str) -> Tuple[Optional[str], Optional[Diagnostic]]:
    """Validate PMID syntax. Returns (digits_or_cleaned, optional_diagnostic)."""
    original = str(raw_pmid)
    s = original.strip()
    if not s:
        return None, Diagnostic(
            code="PMID_SYNTAX_INVALID",
            severity="ERROR",
            message="Empty PMID provided.",
            expected="1-9 digit PubMed ID (e.g. 31535829)",
            actual=repr(original),
            hint="PMID argument is blank.",
            fix="Provide a numeric PubMed ID.",
        )

    # Strip accepted standard wrappers
    s = re.sub(
        r"^(?:https?://(?:www\.)?(?:pubmed\.ncbi\.nlm\.nih\.gov|ncbi\.nlm\.nih\.gov/pubmed)/|PMID\s*[:#]?\s*)",
        "",
        s,
        flags=re.IGNORECASE,
    ).strip()
    s = s.rstrip("/")

    if re.fullmatch(r"[1-9]\d{0,9}", s):
        return s, None

    digits = re.findall(r"\d+", s)
    cleaned = digits[0].lstrip("0") if digits else None
    if cleaned and not re.fullmatch(r"[1-9]\d{0,9}", cleaned):
        cleaned = None

    diag = Diagnostic(
        code="PMID_SYNTAX_INVALID",
        severity="ERROR",
        message=f"Malformed PMID '{original}': contains non-numeric characters or invalid digits.",
        expected="1-9 digit PubMed ID (e.g. 31535829)",
        actual=original,
        details={"cleaned_candidate": cleaned},
        hint="PMIDs are strictly positive integers without letters, trailing punctuation, or 'PMC' prefix (PMC IDs are PMCIDs, not PMIDs).",
        fix=(
            f"Replace '{original}' with numeric PMID '{cleaned}'."
            if cleaned
            else f"Look up the numeric PubMed ID for this paper instead of '{original}'."
        ),
    )
    return cleaned, diag


# ---------------------------------------------------------------------------
# HTTP Client with retries & polite NCBI pacing
# ---------------------------------------------------------------------------


@dataclass
class HttpResponse:
    status: int
    body: str
    url: str
    error: Optional[str] = None


class HttpClient:
    def __init__(self, timeout: float = DEFAULT_TIMEOUT, retries: int = 2) -> None:
        self.timeout = timeout
        self.retries = retries
        self.ncbi_api_key = os.environ.get("NCBI_API_KEY", "").strip()
        self._last_ncbi_call = 0.0

    def get(self, url: str, headers: Optional[Dict[str, str]] = None) -> HttpResponse:
        if "eutils.ncbi.nlm.nih.gov" in url:
            if self.ncbi_api_key and "api_key=" not in url:
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}api_key={urllib.parse.quote(self.ncbi_api_key)}"
            min_gap = 0.12 if self.ncbi_api_key else 0.36
            elapsed = time.monotonic() - self._last_ncbi_call
            if elapsed < min_gap:
                time.sleep(min_gap - elapsed)
            self._last_ncbi_call = time.monotonic()

        req_headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if headers:
            req_headers.update(headers)

        attempt = 0
        last_err = ""
        while attempt <= self.retries:
            req = urllib.request.Request(url, headers=req_headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    charset = resp.headers.get_content_charset() or "utf-8"
                    return HttpResponse(
                        status=getattr(resp, "status", 200),
                        body=raw.decode(charset, errors="replace"),
                        url=url,
                    )
            except urllib.error.HTTPError as exc:
                status = exc.code
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    body = ""
                if status in (429, 500, 502, 503, 504) and attempt < self.retries:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else (0.7 * (2**attempt))
                    time.sleep(min(delay, 5.0))
                    attempt += 1
                    last_err = f"HTTP {status}"
                    continue
                return HttpResponse(status=status, body=body, url=url, error=f"HTTP {status} {exc.reason}")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_err = str(exc)
                if attempt < self.retries:
                    time.sleep(0.6 * (2**attempt))
                    attempt += 1
                    continue
                return HttpResponse(status=0, body="", url=url, error=last_err)

        return HttpResponse(status=0, body="", url=url, error=last_err or "request failed")


# ---------------------------------------------------------------------------
# Authoritative API Clients: Crossref, PubMed E-utilities, Europe PMC
# ---------------------------------------------------------------------------


def _extract_crossref_year(item: Dict[str, Any], key: str) -> Optional[int]:
    part = item.get(key)
    if isinstance(part, dict):
        dp = part.get("date-parts")
        if isinstance(dp, list) and dp and isinstance(dp[0], list) and dp[0]:
            try:
                return int(dp[0][0])
            except (TypeError, ValueError):
                return None
    return None


def parse_crossref_work(msg: Dict[str, Any]) -> PaperRecord:
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


def fetch_crossref_doi(http: HttpClient, doi: str) -> Tuple[Optional[PaperRecord], HttpResponse]:
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


def search_crossref_by_title(
    http: HttpClient, title: str, author: Optional[str] = None, rows: int = 8
) -> List[Dict[str, Any]]:
    params = {"query.title": title, "rows": str(rows)}
    if author:
        params["query.author"] = author
    url = f"https://api.crossref.org/works?{urllib.parse.urlencode(params)}"
    resp = http.get(url, headers={"Accept": "application/json"})
    if resp.status != 200:
        return []
    try:
        items = json.loads(resp.body).get("message", {}).get("items", [])
    except json.JSONDecodeError:
        return []

    out: List[Dict[str, Any]] = []
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
    return out


def parse_pubmed_xml(xml_text: str) -> Optional[PaperRecord]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    article_node = root.find(".//PubmedArticle")
    if article_node is None:
        return None

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
    abs_parts: List[str] = []
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


def fetch_pubmed_pmid(http: HttpClient, pmid: str) -> Tuple[Optional[PaperRecord], HttpResponse]:
    url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id={urllib.parse.quote(pmid)}&retmode=xml"
    )
    resp = http.get(url)
    if resp.status != 200:
        return None, resp
    rec = parse_pubmed_xml(resp.body)
    return rec, resp


def pubmed_esearch_by_doi(http: HttpClient, doi: str) -> Optional[str]:
    term = f'"{doi}"[DOI]'
    url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urllib.parse.urlencode(
        {"db": "pubmed", "term": term, "retmode": "json", "retmax": "2"}
    )
    resp = http.get(url)
    if resp.status != 200:
        return None
    try:
        ids = json.loads(resp.body).get("esearchresult", {}).get("idlist", [])
        return str(ids[0]) if ids else None
    except json.JSONDecodeError:
        return None


def search_pubmed_by_title(http: HttpClient, title: str, retmax: int = 3) -> List[Dict[str, Any]]:
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

    out: List[Dict[str, Any]] = []
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


def parse_epmc_result(item: Dict[str, Any]) -> PaperRecord:
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
    http: HttpClient, doi: Optional[str] = None, pmid: Optional[str] = None
) -> Tuple[Optional[PaperRecord], HttpResponse]:
    if doi:
        query = f'DOI:"{doi}"'
    elif pmid:
        query = f"EXT_ID:{pmid} AND SRC:MED"
    else:
        return None, HttpResponse(status=0, body="", url="", error="No identifier for Europe PMC")
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urllib.parse.urlencode(
        {"query": query, "format": "json", "resultType": "core", "pageSize": "3"}
    )
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


@dataclass
class FulltextResult:
    paragraphs: List[str] = field(default_factory=list)
    source: Optional[str] = None
    error: Optional[str] = None
    is_transient: bool = False


def _parse_jats_body_paragraphs(xml_body: str, source_label: str) -> Tuple[List[str], Optional[str]]:
    """Extract body paragraphs from JATS full-text XML (Europe PMC or NCBI PMC)."""
    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError as exc:
        return [], f"Failed to parse {source_label} XML: {exc}"

    body = root.find(".//body")
    if body is None:
        return [], f"{source_label} XML has no <body> element (likely not licensed for OA full-text XML)"

    paragraphs: List[str] = []
    for p in body.findall(".//p"):
        txt = re.sub(r"\s+", " ", "".join(p.itertext())).strip()
        if len(txt) >= 30:
            paragraphs.append(txt)
    if not paragraphs:
        return [], f"{source_label} XML <body> contained no text paragraphs"
    return paragraphs, None


def _format_http_failure(source_label: str, resp: HttpResponse) -> str:
    if resp.status == 0:
        detail = resp.error or "network/timeout error"
        return f"{source_label} request failed ({detail})"
    return f"{source_label} returned HTTP {resp.status}"


def _fetch_jats_fulltext(http: HttpClient, url: str, source_label: str, source_name: str) -> FulltextResult:
    resp = http.get(url)
    if resp.status != 200:
        transient = resp.status == 0 or resp.status in (429, 500, 502, 503, 504)
        return FulltextResult(error=_format_http_failure(source_label, resp), is_transient=transient)
    paragraphs, err = _parse_jats_body_paragraphs(resp.body, source_label)
    if err or not paragraphs:
        return FulltextResult(error=err or f"{source_label} returned empty body")
    return FulltextResult(paragraphs=paragraphs, source=source_name)


def fetch_epmc_fulltext_paragraphs(http: HttpClient, pmcid: str) -> Tuple[List[str], Optional[str]]:
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{urllib.parse.quote(pmcid)}/fullTextXML"
    res = _fetch_jats_fulltext(http, url, "Europe PMC fullTextXML", "Europe PMC")
    return res.paragraphs, res.error


def fetch_oa_fulltext_paragraphs(http: HttpClient, pmcid: str) -> FulltextResult:
    """Fetch OA JATS full-text paragraphs from Europe PMC, falling back to NCBI PMC on failure."""
    epmc_url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{urllib.parse.quote(pmcid)}/fullTextXML"
    epmc_res = _fetch_jats_fulltext(http, epmc_url, "Europe PMC fullTextXML", "Europe PMC")
    if epmc_res.paragraphs:
        return epmc_res

    ncbi_url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id={urllib.parse.quote(pmcid)}&retmode=xml"
    )
    ncbi_res = _fetch_jats_fulltext(http, ncbi_url, "NCBI PMC efetch", "NCBI PMC")
    if ncbi_res.paragraphs:
        return ncbi_res

    errors = [e for e in (epmc_res.error, ncbi_res.error) if e]
    return FulltextResult(
        error="; ".join(errors) if errors else "empty body",
        is_transient=epmc_res.is_transient or ncbi_res.is_transient,
    )


def merge_records(records: Sequence[Optional[PaperRecord]]) -> Optional[PaperRecord]:
    valid = [r for r in records if r is not None]
    if not valid:
        return None

    # Order priority for primary fields: PubMed > Crossref > Europe PMC
    priority = {"pubmed": 0, "crossref": 1, "europepmc": 2}
    valid.sort(key=lambda r: min(priority.get(s, 99) for s in r.sources) if r.sources else 99)

    merged = PaperRecord()
    for r in valid:
        for s in r.sources:
            if s not in merged.sources:
                merged.sources.append(s)
        merged.doi = merged.doi or r.doi
        merged.pmid = merged.pmid or r.pmid
        merged.pmcid = merged.pmcid or r.pmcid
        merged.title = merged.title or r.title
        if not merged.authors and r.authors:
            merged.authors = list(r.authors)
        merged.published_print = merged.published_print or r.published_print
        merged.published_online = merged.published_online or r.published_online
        merged.issued_year = merged.issued_year or r.issued_year
        merged.journal_full = merged.journal_full or r.journal_full
        merged.journal_abbrev = merged.journal_abbrev or r.journal_abbrev
        # Prefer structured / longer abstract (PubMed first because of sort order)
        if not merged.abstract and r.abstract:
            merged.abstract = r.abstract
        elif (
            merged.abstract
            and r.abstract
            and len(r.abstract) > len(merged.abstract) * 1.4
            and "pubmed" not in merged.sources
        ):
            merged.abstract = r.abstract
        if merged.is_open_access is None and r.is_open_access is not None:
            merged.is_open_access = r.is_open_access
        elif r.is_open_access is True:
            merged.is_open_access = True

        if r.retracted:
            merged.retracted = True
        for reason in r.retraction_reasons:
            if reason not in merged.retraction_reasons:
                merged.retraction_reasons.append(reason)
        for nid in r.retraction_notice_ids:
            if nid not in merged.retraction_notice_ids:
                merged.retraction_notice_ids.append(nid)
        if r.is_retraction_notice:
            merged.is_retraction_notice = True
        for eoc in r.expression_of_concern:
            if eoc not in merged.expression_of_concern:
                merged.expression_of_concern.append(eoc)
        for cn in r.correction_notices:
            if cn not in merged.correction_notices:
                merged.correction_notices.append(cn)

    return merged


# ---------------------------------------------------------------------------
# Metadata comparison & pinpointing diagnostics
# ---------------------------------------------------------------------------


def title_similarity(cited: str, official: str) -> float:
    a = " ".join(tokenize_words(cited))
    b = " ".join(tokenize_words(official))
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def check_title(cited_title: str, official_title: str) -> Optional[Diagnostic]:
    sim = title_similarity(cited_title, official_title)
    cited_toks = tokenize_words(cited_title)
    off_toks = tokenize_words(official_title)
    cited_set = set(cited_toks)
    off_set = set(off_toks)

    extra_in_cited = [t for t in cited_toks if t not in off_set]
    missing_from_cited = [t for t in off_toks if t not in cited_set]

    # Exact or near-exact match (ignoring minor punctuation/hyphens/case)
    if sim >= 0.90:
        return None

    # Check if cited title is a clean prefix / main-title truncation (e.g. before subtitle ':')
    official_main = official_title.split(":", 1)[0].strip()
    is_prefix_truncation = len(cited_toks) >= 3 and (
        off_toks[: len(cited_toks)] == cited_toks
        or (":" in official_title and title_similarity(cited_title, official_main) >= 0.90)
    )

    if is_prefix_truncation:
        return Diagnostic(
            code="TITLE_MISMATCH",
            severity="WARNING",
            message=f"Cited title appears truncated (missing subtitle/ending); similarity={sim:.2f}.",
            expected=f'cited="{cited_title}"',
            actual=f'official="{official_title}"',
            details={
                "similarity": round(sim, 3),
                "truncation": True,
                "extra_tokens_in_cited": extra_in_cited,
                "missing_tokens_from_cited": missing_from_cited,
            },
            hint="Cited title matches the leading portion/main title of the official title but omits the subtitle or trailing words.",
            fix=f'Update cited title to the full official title: "{official_title}".',
        )

    severity = "ERROR" if sim < 0.65 else "WARNING"
    hint = (
        "Very low title overlap — likely wrong DOI/PMID, hallucinated title, or different paper."
        if sim < 0.65
        else "Moderate title discrepancy — check for misquoted words, paraphrasing, or edition/version differences."
    )
    return Diagnostic(
        code="TITLE_MISMATCH",
        severity=severity,
        message=f"Title mismatch (similarity={sim:.2f}).",
        expected=f'cited="{cited_title}"',
        actual=f'official="{official_title}"',
        details={
            "similarity": round(sim, 3),
            "truncation": False,
            "extra_tokens_in_cited": extra_in_cited[:15],
            "missing_tokens_from_cited": missing_from_cited[:15],
        },
        hint=hint,
        fix=f'Verify this is the intended paper; if so, replace title with "{official_title}". Otherwise fix the DOI/PMID.',
    )


def _extract_cited_surname_candidates(cited_author: str) -> List[str]:
    s = normalize_text(cited_author)
    s = re.sub(r"\bet\s+al\.?$", "", s, flags=re.IGNORECASE).strip(" ,.;")
    if not s:
        return []
    # "Last, First" format
    if "," in s:
        before = s.split(",", 1)[0].strip()
        return [before.lower()] if before else []

    parts = [p.strip(".") for p in s.split() if p.strip(".")]
    if not parts:
        return []
    if len(parts) == 1:
        return [parts[0].lower()]

    cands: List[str] = []
    # Check if trailing token(s) are initials (e.g. "McMurray JJV" or "Patel R" or "Smith J A")
    last_tok = parts[-1]
    looks_like_initials = (len(last_tok) <= 4 and last_tok.isupper() and last_tok.isalpha()) or (
        len(last_tok) == 1 and last_tok.isalpha()
    )
    if looks_like_initials:
        # Walk backwards skipping initials
        idx = len(parts) - 1
        while idx > 0 and (
            (len(parts[idx]) <= 4 and parts[idx].isupper() and parts[idx].isalpha()) or len(parts[idx]) == 1
        ):
            idx -= 1
        cands.append(" ".join(parts[: idx + 1]).lower())
        cands.append(parts[idx].lower())
    else:
        # "First Last" or "First van der Last"
        cands.append(parts[-1].lower())
        if len(parts) >= 2 and parts[-2].lower() in SURNAME_PARTICLES:
            idx = len(parts) - 2
            while idx > 0 and parts[idx - 1].lower() in SURNAME_PARTICLES:
                idx -= 1
            cands.append(" ".join(parts[idx:]).lower())
        # Also keep first token as fallback in case of "Surname Initials" in lowercase ("smith j")
        if len(parts[-1]) <= 3 and parts[-1].isalpha():
            cands.append(parts[0].lower())

    # Deduplicate preserving order
    seen = set()
    uniq = []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def _author_matches(cited_cands: List[str], cited_full_norm: str, author: Author) -> bool:
    if author.collective:
        coll = normalize_text(author.collective).lower()
        return cited_full_norm in coll or coll in cited_full_norm
    fam = normalize_text(author.family).lower()
    if not fam:
        return False
    fam_tokens = fam.split()
    fam_last = fam_tokens[-1] if fam_tokens else fam
    for cand in cited_cands:
        if cand == fam or cand == fam_last:
            return True
        # Hyphenated surnames (e.g. "Garcia-Lopez" vs "Garcia" or "Lopez")
        if "-" in fam and cand in fam.split("-"):
            return True
        if len(cand) >= 4 and len(fam) >= 4 and difflib.SequenceMatcher(None, cand, fam).ratio() >= 0.88:
            return True
    return False


def check_author(cited_author: str, authors: List[Author]) -> Optional[Diagnostic]:
    if not authors:
        return Diagnostic(
            code="AUTHOR_MISMATCH",
            severity="WARNING",
            message="Author list unavailable from authoritative metadata; could not verify cited author.",
            expected=f'cited_author="{cited_author}"',
            actual="authors=[]",
            hint="Source metadata returned no authors (common for editorials or certain book chapters).",
            fix="Verify author list manually against the publisher page.",
        )

    cands = _extract_cited_surname_candidates(cited_author)
    cited_norm = normalize_text(re.sub(r"\bet\s+al\.?$", "", cited_author, flags=re.IGNORECASE)).strip(" ,.").lower()

    if _author_matches(cands, cited_norm, authors[0]):
        return None

    display_list = [a.display for a in authors]
    preview_list = display_list[:10] + ([f"... ({len(display_list)} total)"] if len(display_list) > 10 else [])

    # Check if cited author appears as middle/last author
    matched_positions: List[Tuple[int, str]] = []
    for idx, au in enumerate(authors[1:], start=2):
        if _author_matches(cands, cited_norm, au):
            matched_positions.append((idx, au.display))

    if matched_positions:
        pos, matched_name = matched_positions[0]
        role = "last author" if pos == len(authors) else f"author #{pos}"
        return Diagnostic(
            code="AUTHOR_MISMATCH",
            severity="WARNING",
            message=(
                f'Cited author "{cited_author}" is {role} ("{matched_name}"), NOT first author '
                f'("{authors[0].display}").'
            ),
            expected=f'cited_author="{cited_author}"',
            actual=f'actual_first_author="{authors[0].display}"; full_authors={preview_list}',
            details={
                "actual_first_author": authors[0].display,
                "matched_position": pos,
                "matched_author": matched_name,
                "author_count": len(authors),
                "authors_preview": preview_list,
            },
            hint=f'"{cited_author}" is on the paper as {role}, not the first author.',
            fix=f'Replace cited first author with "{authors[0].display}" (or cite "{authors[0].family} et al.").',
        )

    return Diagnostic(
        code="AUTHOR_MISMATCH",
        severity="ERROR",
        message=(
            f'Cited author "{cited_author}" does NOT appear anywhere among the {len(authors)} authors '
            f'(first author: "{authors[0].display}").'
        ),
        expected=f'cited_author="{cited_author}"',
        actual=f'actual_first_author="{authors[0].display}"; full_authors={preview_list}',
        details={
            "actual_first_author": authors[0].display,
            "matched_position": None,
            "author_count": len(authors),
            "authors_preview": preview_list,
        },
        hint="Author mismatch with zero matches across the full author list strongly suggests wrong DOI/PMID or misattributed paper.",
        fix=f'Verify whether this DOI/PMID is the right paper. If yes, change author to "{authors[0].display}".',
    )


def check_year(cited_year_raw: str, rec: PaperRecord) -> Optional[Diagnostic]:
    cited_yr = parse_year_int(cited_year_raw)
    if cited_yr is None:
        return Diagnostic(
            code="YEAR_MISMATCH",
            severity="ERROR",
            message=f'Could not parse cited year "{cited_year_raw}".',
            expected="4-digit publication year (e.g. 2019)",
            actual=str(cited_year_raw),
            hint="Non-numeric or malformed year field.",
            fix=f"Use official publication year {rec.best_year()}.",
        )

    all_yrs = rec.all_years()
    if not all_yrs:
        return None

    if cited_yr in all_yrs:
        return None

    min_diff = min(abs(cited_yr - y) for y in all_yrs)
    date_parts = []
    if rec.published_print:
        date_parts.append(f"published_print={rec.published_print}")
    if rec.published_online:
        date_parts.append(f"published_online={rec.published_online}")
    if rec.issued_year and rec.issued_year not in (rec.published_print, rec.published_online):
        date_parts.append(f"issued={rec.issued_year}")
    actual_str = " / ".join(date_parts)

    if min_diff == 1:
        return Diagnostic(
            code="YEAR_MISMATCH",
            severity="WARNING",
            message=f"Cited year {cited_yr} differs by ±1 year from official dates ({actual_str}).",
            expected=f"cited={cited_yr}",
            actual=actual_str,
            details={
                "cited_year": cited_yr,
                "published_print": rec.published_print,
                "published_online": rec.published_online,
                "issued_year": rec.issued_year,
                "offset_years": min_diff,
                "likely_epub_offset": True,
            },
            hint="±1 year difference is commonly caused by epub-ahead-of-print vs print issue year.",
            fix=f"Update citation year to {rec.best_year()} to match the official record.",
        )

    return Diagnostic(
        code="YEAR_MISMATCH",
        severity="ERROR",
        message=f"Cited year {cited_yr} mismatches official publication year by {min_diff} years ({actual_str}).",
        expected=f"cited={cited_yr}",
        actual=actual_str,
        details={
            "cited_year": cited_yr,
            "published_print": rec.published_print,
            "published_online": rec.published_online,
            "issued_year": rec.issued_year,
            "offset_years": min_diff,
            "likely_epub_offset": False,
        },
        hint=f"A {min_diff}-year discrepancy is not an epub-ahead-of-print offset — check for typo or wrong paper.",
        fix=f"Confirm the DOI/PMID matches the intended paper, then correct the year to {rec.best_year()}.",
    )


def check_journal(cited_journal: str, rec: PaperRecord) -> Optional[Diagnostic]:
    variants = rec.journal_variants()
    if not variants:
        return None

    cited_norm = normalize_journal(cited_journal)
    cited_alias = journal_alias_group(cited_journal)

    best_sim = 0.0
    for v in variants:
        v_norm = normalize_journal(v)
        if cited_norm == v_norm:
            return None
        if cited_alias and cited_alias == journal_alias_group(v):
            return None
        # Check abbreviation match: every token in shorter name is a prefix of corresponding token in longer name
        c_toks = cited_norm.split()
        v_toks = v_norm.split()
        if c_toks and v_toks and len(c_toks) == len(v_toks):
            if all(a.startswith(b) or b.startswith(a) for a, b in zip(c_toks, v_toks)):
                return None
        sim = difflib.SequenceMatcher(None, cited_norm, v_norm).ratio()
        if sim > best_sim:
            best_sim = sim

    if best_sim >= 0.85:
        return None

    actual_parts = []
    if rec.journal_full:
        actual_parts.append(f'full="{rec.journal_full}"')
    if rec.journal_abbrev and rec.journal_abbrev != rec.journal_full:
        actual_parts.append(f'abbrev="{rec.journal_abbrev}"')
    actual_str = " / ".join(actual_parts)

    severity = "ERROR" if best_sim < 0.5 else "WARNING"
    return Diagnostic(
        code="JOURNAL_MISMATCH",
        severity=severity,
        message=f'Cited journal "{cited_journal}" does not match official journal ({actual_str}); similarity={best_sim:.2f}.',
        expected=f'cited="{cited_journal}"',
        actual=actual_str,
        details={
            "cited_journal": cited_journal,
            "official_full": rec.journal_full,
            "official_abbrev": rec.journal_abbrev,
            "similarity": round(best_sim, 3),
        },
        hint=(
            "Low journal similarity — either wrong paper/DOI or non-standard journal name."
            if best_sim < 0.5
            else "Journal name differs from official full/abbreviated title."
        ),
        fix=f'Replace journal with "{rec.journal_abbrev or rec.journal_full}" (official: "{rec.journal_full or rec.journal_abbrev}").',
    )


def check_retraction_and_notices(rec: PaperRecord) -> List[Diagnostic]:
    diags: List[Diagnostic] = []
    if rec.retracted:
        reasons = (
            "; ".join(rec.retraction_reasons)
            if rec.retraction_reasons
            else "Flagged retracted by authoritative registry"
        )
        notices = ", ".join(rec.retraction_notice_ids) if rec.retraction_notice_ids else "check publisher page"
        diags.append(
            Diagnostic(
                code="RETRACTED_PAPER",
                severity="CRITICAL",
                message=f"CRITICAL: This paper has been RETRACTED ({reasons}).",
                expected="Valid, non-retracted peer-reviewed publication",
                actual=f"RETRACTED — {reasons} (Notice: {notices})",
                details={
                    "reasons": rec.retraction_reasons,
                    "notice_ids": rec.retraction_notice_ids,
                },
                hint="Authoritative registry (PubMed/Crossref/Europe PMC) flags this publication as retracted.",
                fix="REMOVE this citation from evidence synthesis/claims immediately or explicitly label it as retracted if discussing the retraction itself.",
            )
        )
    if rec.is_retraction_notice:
        diags.append(
            Diagnostic(
                code="RETRACTION_NOTICE",
                severity="WARNING",
                message="Cited identifier resolves to a RETRACTION NOTICE rather than an original research paper.",
                expected="Original research article",
                actual="Retraction notice / Retraction of Publication",
                hint="The DOI/PMID points to the retraction notice itself.",
                fix="Confirm whether you intended to cite the retraction notice; remove if used to support a scientific claim.",
            )
        )
    if rec.expression_of_concern:
        diags.append(
            Diagnostic(
                code="EXPRESSION_OF_CONCERN",
                severity="WARNING",
                message=f"Editorial Expression of Concern issued: {'; '.join(rec.expression_of_concern)}.",
                expected="Uncontested peer-reviewed publication",
                actual="; ".join(rec.expression_of_concern),
                hint="Journal editors have published an expression of concern questioning data integrity or validity.",
                fix="Inspect the editorial notice before relying on this paper's conclusions.",
            )
        )
    if rec.correction_notices:
        diags.append(
            Diagnostic(
                code="HAS_CORRECTION_NOTICE",
                severity="INFO",
                message=f"Published erratum/correction exists: {'; '.join(rec.correction_notices)}.",
                expected="Original published values",
                actual="; ".join(rec.correction_notices),
                hint="A corrigendum/erratum was published; verify whether any cited figures or numbers were updated.",
                fix="Check the correction notice if citing exact numbers from this paper.",
            )
        )
    return diags


# ---------------------------------------------------------------------------
# Claim Validity Pack: number & keyword extraction + abstract/fulltext grounding
# ---------------------------------------------------------------------------

# Patterns for quantitative expressions in scientific/biomedical claims
QUANT_PATTERNS = [
    # p-values: p < 0.001, P = 0.02, p<=0.05
    re.compile(r"\b[pP]\s*(?:<=|>=|<|>|=)\s*0?\.\d+(?:[eE][+-]?\d+)?"),
    # Confidence intervals: 95% CI 0.65-0.85, 95% confidence interval [CI], 0.65 to 0.85
    re.compile(
        r"\b\d{2}%\s*(?:confidence\s+interval(?:\s*\[CI\])?|CI)\s*[:=,]?\s*\(?\s*-?\d+(?:\.\d+)?\s*(?:-|–|—|to|,)\s*-?\d+(?:\.\d+)?\s*\)?",
        re.IGNORECASE,
    ),
    # Effect sizes: HR 0.74, hazard ratio, 0.74, odds ratio 1.45, relative risk 0.79, NNT = 21
    re.compile(
        r"\b(?:hazard\s+ratio|odds\s+ratio|relative\s+risk|risk\s+ratio|incidence\s+rate\s+ratio|a?HR|a?OR|a?RR|IRR|RRR|ARR|SMD|WMD|MD|NNT|NNH)\s*[:=,]?\s*-?\d+(?:\.\d+)?",
        re.IGNORECASE,
    ),
    # Sample sizes: N = 4,744 / n=4744
    re.compile(r"\b[nN]\s*=\s*\d{1,3}(?:,\d{3})+|\b[nN]\s*=\s*\d+"),
    # Percentages: 26%, 16.3%
    re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?\s*%"),
    # Numbers with common biomedical units
    re.compile(
        r"\b\d+(?:,\d{3})*(?:\.\d+)?\s*(?:mg/kg|mg/dL|mg/L|mmol/L|mL/min|mmHg|µg|ug|mcg|mg|g|kg|mL|L|IU|months?|weeks?|days?|years?|patients?|participants?|subjects?|trials?|studies)\b",
        re.IGNORECASE,
    ),
]

NUMBER_LEFT_BOUND = r"(?<![A-Za-z0-9.])"
NUMBER_RIGHT_BOUND = r"(?![A-Za-z0-9])(?!\.\d)"
BARE_NUMBER_RE = re.compile(
    rf"{NUMBER_LEFT_BOUND}(?:\d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?|\d+\.\d+|\d+){NUMBER_RIGHT_BOUND}"
)


def extract_claim_numbers(claim: str) -> List[str]:
    """Extract quantitative tokens (numbers, %, HRs, CIs, p-values, sample sizes) cleanly."""
    norm = normalize_text(claim)
    tokens: List[str] = []
    seen_core: set[str] = set()

    def _add(tok: str) -> None:
        t = re.sub(r"\s+", " ", tok).strip()
        if not t:
            return
        key = t.lower().replace(",", "")
        if key not in seen_core:
            seen_core.add(key)
            tokens.append(t)

    # First collect rich structured tokens (so output displays "HR 0.74", "p < 0.001", "N=4,744", "16.3%")
    covered_spans: List[Tuple[int, int]] = []
    for pat in QUANT_PATTERNS:
        for m in pat.finditer(norm):
            # Skip standalone "95%" if it is part of a "95% CI ..." match already captured
            if any(s <= m.start() and m.end() <= e for s, e in covered_spans):
                continue
            covered_spans.append((m.start(), m.end()))
            _add(m.group(0))

    # Also capture any remaining standalone numbers not inside already-captured spans
    for m in BARE_NUMBER_RE.finditer(norm):
        if any(s <= m.start() and m.end() <= e for s, e in covered_spans):
            continue
        num = m.group(0)
        # Skip single-digit numbers that are part of chemical/receptor names like SGLT2, GLP-1, IL-6, COVID-19
        before = norm[max(0, m.start() - 6) : m.start()]
        if re.search(r"[A-Za-z]-?$", before):
            continue
        _add(num)

    return tokens


def _numeric_subvalues(token: str) -> List[str]:
    """Return normalized numeric values inside a quantitative token (e.g. '95% CI 0.65-0.85' -> ['0.65', '0.85'])."""
    t = normalize_text(token).replace(",", "")
    # For '95% CI 0.65-0.85' or '95% confidence interval [CI], 0.65 to 0.85', check the interval endpoints
    ci_m = re.match(
        r"^\d{2}%\s*(?:confidence\s+interval(?:\s*\[CI\])?|CI)\s*[:=,]?\s*\(?\s*(-?\d+(?:\.\d+)?)\s*(?:-|to|,)\s*(-?\d+(?:\.\d+)?)\s*\)?$",
        t,
        re.IGNORECASE,
    )
    if ci_m:
        return [ci_m.group(1), ci_m.group(2)]
    # For p-value like 'p < 0.001'
    p_m = re.match(r"^[pP]\s*(?:<=|>=|<|>|=)\s*(0?\.\d+(?:[eE][+-]?\d+)?)$", t)
    if p_m:
        val = p_m.group(1)
        if val.startswith("."):
            val = "0" + val
        return [val]
    nums = re.findall(r"-?\d+(?:\.\d+)?", t)
    return nums


def _number_present_in_text(num_str: str, norm_text_no_commas: str, is_percent: bool = False) -> bool:
    """Check whether exact numeric value appears in normalized text (handling .74 vs 0.74, % vs percent)."""
    val = num_str.lstrip("+")
    alts = [re.escape(val)]
    # Handle leading zero equivalence: 0.74 <-> .74
    if val.startswith("0."):
        alts.append(re.escape(val[1:]))
    elif val.startswith("-0."):
        alts.append(re.escape("-" + val[2:]))
    # Handle trailing .0 equivalence: 26.0 -> 26
    if "." in val and val.rstrip("0").endswith("."):
        alts.append(re.escape(val.rstrip("0").rstrip(".")))

    num_group = "(?:" + "|".join(alts) + ")"
    left_bound = rf"{NUMBER_LEFT_BOUND}(?<![A-Za-z]-)"
    if is_percent:
        pat = rf"{left_bound}{num_group}(?:\s*(?:%|percent|per\s+cent))(?![A-Za-z])"
        if re.search(pat, norm_text_no_commas, flags=re.IGNORECASE):
            return True
        # Also allow bare numbers paired in a percentage range/comparison (e.g. "16.3 versus 21.2%", "0.96 to 22.6%")
        pat_paired = (
            rf"{left_bound}{num_group}{NUMBER_RIGHT_BOUND}"
            r"\s*(?:-|to|vs\.?|versus)\s*"
            r"-?\d+(?:\.\d+)?\s*(?:%|percent|per\s+cent)(?![A-Za-z])"
        )
        return bool(re.search(pat_paired, norm_text_no_commas, flags=re.IGNORECASE))

    pat = rf"{left_bound}{num_group}{NUMBER_RIGHT_BOUND}"
    return bool(re.search(pat, norm_text_no_commas))


def quant_token_in_text(token: str, text: str) -> bool:
    """Check if a quantitative token (including all its key numeric values) is present in text."""
    norm_text = normalize_text(text)
    norm_no_commas = re.sub(r"(?<=\d),(?=\d{3}\b)", "", norm_text)
    is_pct = "%" in token and "ci" not in token.lower()
    subvals = _numeric_subvalues(token)
    if not subvals:
        return token.lower() in norm_text.lower()
    return all(_number_present_in_text(v, norm_no_commas, is_percent=is_pct) for v in subvals)


def extract_abstract_numbers(text: str) -> List[str]:
    """Extract all notable quantitative expressions from the official abstract for side-by-side comparison."""
    return extract_claim_numbers(text)[:30]


def extract_claim_keywords(claim: str) -> List[str]:
    """Extract drug names, trial acronyms, and high-signal domain terms."""
    norm = normalize_text(claim)
    keywords: List[str] = []
    seen: set[str] = set()

    # Trial acronyms / uppercase biomedical tokens (e.g. DAPA-HF, EMPA-REG, HFrEF, SGLT2, HbA1c)
    for m in re.finditer(r"\b[A-Z][A-Za-z0-9-]{2,}\b", norm):
        tok = m.group(0)
        low = tok.lower()
        if low in STOPWORDS or re.fullmatch(r"(?:HR|OR|RR|CI|NNT|NNH|SMD|IRR)", tok):
            continue
        if any(c.isupper() for c in tok[1:]) or "-" in tok or any(c.isdigit() for c in tok):
            if low not in seen:
                seen.add(low)
                keywords.append(tok)

    # Pharmacological stems & substantial domain words
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9-]{3,}", norm):
        low = tok.lower()
        if low in STOPWORDS or low in seen:
            continue
        if any(low.endswith(stem) for stem in PHARMA_STEMS) or len(low) >= 7:
            seen.add(low)
            keywords.append(tok)

    return keywords[:15]


def split_sentences(text: str) -> List[str]:
    # Split on newlines or sentence-ending punctuation followed by whitespace and uppercase/digit
    parts: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(])", line):
            s = sent.strip()
            if s:
                parts.append(s)
    return parts


def find_top_matching_sentences(
    text_blocks: Sequence[str], quant_tokens: Sequence[str], keywords: Sequence[str], max_items: int = 4
) -> List[str]:
    scored: List[Tuple[int, int, str]] = []
    for idx, sent in enumerate(text_blocks):
        score = 0
        for qt in quant_tokens:
            if quant_token_in_text(qt, sent):
                score += 3
        low = sent.lower()
        for kw in keywords:
            if kw.lower() in low:
                score += 1
        if score > 0:
            scored.append((score, -idx, sent))
    scored.sort(reverse=True)
    return [s for _, _, s in scored[:max_items]]


def evaluate_claim(
    claim: str,
    rec: PaperRecord,
    http: Optional[HttpClient] = None,
    fetch_fulltext: bool = False,
) -> Tuple[ClaimReport, List[Diagnostic]]:
    report = ClaimReport(
        claim=claim,
        extracted_numbers=extract_claim_numbers(claim),
        extracted_keywords=extract_claim_keywords(claim),
        abstract_available=bool(rec.abstract),
    )
    diags: List[Diagnostic] = []

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
        for tok in report.extracted_numbers:
            if quant_token_in_text(tok, rec.abstract):
                report.matched_numbers.append(tok)
            else:
                report.missing_numbers.append(tok)

        abs_low = normalize_text(rec.abstract).lower()
        for kw in report.extracted_keywords:
            if normalize_text(kw).lower() in abs_low:
                report.matched_keywords.append(kw)

        report.matched_sentences = find_top_matching_sentences(
            split_sentences(rec.abstract), report.extracted_numbers, report.extracted_keywords, max_items=4
        )

    # Optional OA full-text check (Europe PMC with NCBI PMC fallback)
    if fetch_fulltext and http is not None:
        if not rec.pmcid:
            report.fulltext_status = "unavailable: no PMCID linked to this paper"
            diags.append(
                Diagnostic(
                    code="FULLTEXT_UNAVAILABLE",
                    severity="INFO",
                    message="OA full text requested (--fulltext), but no PMCID is linked to this paper.",
                    expected="PMCID with Open Access JATS full-text XML (Europe PMC / NCBI PMC)",
                    actual="pmcid=None",
                    hint="Paper is likely not deposited in PubMed Central.",
                    fix="Verify non-abstract numbers against the publisher PDF.",
                )
            )
        else:
            ft_res = fetch_oa_fulltext_paragraphs(http, rec.pmcid)
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
                src_tag = f" via {ft_res.source}" if ft_res.source else ""
                report.fulltext_status = f"fetched ({len(ft_res.paragraphs)} paragraphs from {rec.pmcid}{src_tag})"
                full_joined = "\n".join(ft_res.paragraphs)
                # Re-check any numbers missing from abstract against OA full text
                still_missing: List[str] = []
                for tok in report.missing_numbers:
                    if quant_token_in_text(tok, full_joined):
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

    return report, diags


# ---------------------------------------------------------------------------
# Core single-citation verification orchestration
# ---------------------------------------------------------------------------


def _search_did_you_mean(http: HttpClient, title: Optional[str], author: Optional[str] = None) -> List[Dict[str, Any]]:
    if not title:
        return []
    candidates: List[Dict[str, Any]] = []
    candidates.extend(search_crossref_by_title(http, title, author=author, rows=8))
    candidates.extend(search_pubmed_by_title(http, title, retmax=5))

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


def verify_citation(cit: CitationInput, http: HttpClient) -> VerificationResult:
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

    # Fetch DOI sources
    cr_rec: Optional[PaperRecord] = None
    epmc_doi_rec: Optional[PaperRecord] = None
    cr_resp: Optional[HttpResponse] = None
    epmc_doi_resp: Optional[HttpResponse] = None
    if clean_doi:
        cr_rec, cr_resp = fetch_crossref_doi(http, clean_doi)
        epmc_doi_rec, epmc_doi_resp = fetch_epmc(http, doi=clean_doi)

    # Fetch PMID sources
    pm_rec: Optional[PaperRecord] = None
    epmc_pmid_rec: Optional[PaperRecord] = None
    pm_resp: Optional[HttpResponse] = None
    if clean_pmid:
        pm_rec, pm_resp = fetch_pubmed_pmid(http, clean_pmid)
        if pm_rec is None or not pm_rec.pmcid or pm_rec.is_open_access is None:
            epmc_pmid_rec, _ = fetch_epmc(http, pmid=clean_pmid)

    doi_merged = merge_records([cr_rec, epmc_doi_rec])
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
    if clean_doi and not clean_pmid and doi_merged is not None:
        linked_pmid = doi_merged.pmid or pubmed_esearch_by_doi(http, clean_doi)
        if linked_pmid:
            pm_extra, _ = fetch_pubmed_pmid(http, linked_pmid)
            if pm_extra is not None:
                # Guard against bogus PubMed esearch match if DOI differs
                if not pm_extra.doi or pm_extra.doi.lower() == clean_doi.lower():
                    pm_rec = pm_extra

    # Enrich PMID-only lookup with Crossref (for Crossref update-to/updated-by retraction relations & print/online dates)
    if clean_pmid and not clean_doi and pmid_merged is not None and pmid_merged.doi:
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
        claim_report, c_diags = evaluate_claim(cit.claim, resolved, http=http, fetch_fulltext=cit.fulltext)
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


def _compute_status(diagnostics: Sequence[Diagnostic]) -> str:
    if any(d.severity in ("CRITICAL", "ERROR") for d in diagnostics):
        return "FAIL"
    if any(d.severity == "WARNING" for d in diagnostics):
        return "WARN"
    return "PASS"


# ---------------------------------------------------------------------------
# Document scanning (.md, .txt, .docx, .pptx, etc.)
# ---------------------------------------------------------------------------

SCAN_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s<>\"'{}|\\^\[\]`]+)")
SCAN_PMID_RE = re.compile(
    r"(?:\bPMID\s*[:#]?\s*(\d{5,9})\b|pubmed\.ncbi\.nlm\.nih\.gov/(\d{5,9})(?:/|\b))",
    flags=re.IGNORECASE,
)


def _extract_segments_from_docx(path: Path) -> List[Tuple[str, str]]:
    segments: List[Tuple[str, str]] = []
    with zipfile.ZipFile(path, "r") as zf:
        names = [
            n
            for n in zf.namelist()
            if n == "word/document.xml"
            or n in ("word/footnotes.xml", "word/endnotes.xml")
            or re.match(r"^word/(?:header|footer)\d+\.xml$", n)
        ]
        # Ensure document.xml comes first
        names.sort(key=lambda n: (0 if n == "word/document.xml" else 1, n))
        for name in names:
            try:
                root = ET.fromstring(zf.read(name))
            except ET.ParseError:
                continue
            part_label = Path(name).stem
            p_idx = 0
            for elem in root.iter():
                if elem.tag.endswith("}p") or elem.tag == "p":
                    texts = [t.text for t in elem.iter() if (t.tag.endswith("}t") or t.tag == "t") and t.text]
                    joined = "".join(texts).strip()
                    if joined:
                        p_idx += 1
                        segments.append((f"{part_label}:p{p_idx}", joined))
    return segments


def _extract_segments_from_pptx(path: Path) -> List[Tuple[str, str]]:
    segments: List[Tuple[str, str]] = []
    with zipfile.ZipFile(path, "r") as zf:
        slide_files: List[Tuple[int, str, str]] = []
        for name in zf.namelist():
            m_slide = re.match(r"^ppt/slides/slide(\d+)\.xml$", name)
            if m_slide:
                slide_files.append((int(m_slide.group(1)), "slide", name))
                continue
            m_note = re.match(r"^ppt/notesSlides/notesSlide(\d+)\.xml$", name)
            if m_note:
                slide_files.append((int(m_note.group(1)), "notes", name))
        slide_files.sort(key=lambda x: (x[0], 0 if x[1] == "slide" else 1))

        for num, kind, name in slide_files:
            try:
                root = ET.fromstring(zf.read(name))
            except ET.ParseError:
                continue
            label = f"Slide {num}" if kind == "slide" else f"Slide {num} notes"
            paras: List[str] = []
            for elem in root.iter():
                if elem.tag.endswith("}p") or elem.tag == "p":
                    texts = [t.text for t in elem.iter() if (t.tag.endswith("}t") or t.tag == "t") and t.text]
                    joined = "".join(texts).strip()
                    if joined:
                        paras.append(joined)
            if paras:
                segments.append((label, "\n".join(paras)))
    return segments


def extract_text_segments(path: Path) -> List[Tuple[str, str]]:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return _extract_segments_from_docx(path)
    if suffix == ".pptx":
        return _extract_segments_from_pptx(path)
    raw = path.read_text(encoding="utf-8", errors="replace")
    return [(f"line {i}", line) for i, line in enumerate(raw.splitlines(), start=1) if line.strip()]


def extract_citations_from_segments(segments: Sequence[Tuple[str, str]]) -> List[CitationInput]:
    """Extract deduplicated DOIs and PMIDs from text segments, pairing co-located DOI+PMID pairs."""
    extracted: List[CitationInput] = []
    seen_dois: set[str] = set()
    seen_pmids: set[str] = set()

    for loc, text in segments:
        dois_found: List[Tuple[int, str]] = []
        for m in SCAN_DOI_RE.finditer(text):
            cleaned, _ = _strip_unbalanced_closing(m.group(1))
            if cleaned and DOI_CORE_RE.match(cleaned):
                dois_found.append((m.start(), cleaned))

        pmids_found: List[Tuple[int, str]] = []
        for m in SCAN_PMID_RE.finditer(text):
            pid = m.group(1) or m.group(2)
            if pid:
                pmids_found.append((m.start(), pid))

        # Pair a DOI and PMID if they appear near each other (within 160 chars) in the same segment
        used_pmid_idxs: set[int] = set()
        for d_pos, doi in dois_found:
            d_key = doi.lower()
            paired_pmid: Optional[str] = None
            for idx, (p_pos, pmid) in enumerate(pmids_found):
                if idx in used_pmid_idxs:
                    continue
                if abs(p_pos - d_pos) <= 160:
                    paired_pmid = pmid
                    used_pmid_idxs.add(idx)
                    break

            if d_key in seen_dois and (not paired_pmid or paired_pmid in seen_pmids):
                continue
            seen_dois.add(d_key)
            if paired_pmid:
                seen_pmids.add(paired_pmid)
            extracted.append(
                CitationInput(
                    doi=doi,
                    pmid=paired_pmid,
                    location=loc,
                    label=f"{loc} ({doi}" + (f", PMID:{paired_pmid})" if paired_pmid else ")"),
                )
            )

        for idx, (_, pmid) in enumerate(pmids_found):
            if idx in used_pmid_idxs or pmid in seen_pmids:
                continue
            seen_pmids.add(pmid)
            extracted.append(
                CitationInput(
                    pmid=pmid,
                    location=loc,
                    label=f"{loc} (PMID:{pmid})",
                )
            )

    return extracted


BIBLIOGRAPHY_HEADING_RE = re.compile(
    r"^(?:#{1,6}\s+)?(?:\d+(?:\.\d+)*\.?\s+)?(?:references|bibliography|works\s+cited|literature\s+cited)\s*:?$",
    flags=re.IGNORECASE,
)
POST_BIBLIOGRAPHY_HEADING_RE = re.compile(
    r"^(?:#{1,6}\s+)?(?:\d+(?:\.\d+)*\.?\s+)?"
    r"(?:appendix|appendices|supplementary|supplemental|tables?|figures?|"
    r"acknowledg(?:e)?ments?|funding|conflicts?\s+of\s+interest|competing\s+interests?|"
    r"author\s+contributions?|abbreviations?|glossary|annexure|case\s+record\s+form)\b",
    flags=re.IGNORECASE,
)
NUMBERED_ENTRY_RE = re.compile(r"^(?:\[\d+\]|\d+[.)])\s*\S")
YEAR_OR_URL_RE = re.compile(r"(?:\b(?:19|20)\d{2}\b|https?://)", flags=re.IGNORECASE)


def _is_bibliography_section_end(line: str) -> bool:
    if BIBLIOGRAPHY_HEADING_RE.match(line):
        return False
    if line.startswith("#"):
        return True
    if len(line) <= 60 and not YEAR_OR_URL_RE.search(line):
        if POST_BIBLIOGRAPHY_HEADING_RE.match(line):
            return True
        letters = [ch for ch in line if ch.isalpha()]
        if len(letters) >= 3 and all(ch.isupper() for ch in letters) and not re.search(r"\d", line):
            return True
    return False


@dataclass
class UnlinkedEntry:
    location: str
    text: str


def _segment_part_key(loc: str) -> str:
    if ":" in loc:
        return loc.split(":", 1)[0]
    if loc.endswith(" notes"):
        return "notes"
    return "body"


def extract_unlinked_bibliography_entries(segments: Sequence[Tuple[str, str]]) -> List[UnlinkedEntry]:
    """Find bibliography entries under a References/Bibliography heading that contain no DOI or PMID."""
    unlinked: List[UnlinkedEntry] = []
    in_bib = False
    prev_part: Optional[str] = None

    for loc, text in segments:
        part = _segment_part_key(loc)
        if prev_part is not None and part != prev_part:
            in_bib = False
        prev_part = part

        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            if BIBLIOGRAPHY_HEADING_RE.match(stripped):
                in_bib = True
                continue
            if not in_bib:
                continue
            if _is_bibliography_section_end(stripped):
                in_bib = False
                continue
            if SCAN_DOI_RE.search(stripped) or SCAN_PMID_RE.search(stripped):
                continue
            looks_like_entry = (NUMBERED_ENTRY_RE.match(stripped) and len(stripped) >= 15) or (
                YEAR_OR_URL_RE.search(stripped) and len(stripped) >= 20
            )
            if looks_like_entry:
                unlinked.append(UnlinkedEntry(location=loc, text=stripped))

    return unlinked


def build_unlinked_bibliography_diagnostic(entries: Sequence[UnlinkedEntry]) -> Optional[Diagnostic]:
    if not entries:
        return None
    count = len(entries)
    previews = [f"{e.location}: {_truncate(e.text, 110)}" for e in entries[:5]]
    return Diagnostic(
        code="UNLINKED_BIBLIOGRAPHY_ENTRIES",
        severity="WARNING",
        message=(
            f"Found {count} bibliography entr{'y' if count == 1 else 'ies'} without a DOI or PMID; "
            f"`ref-verify scan` could not verify {'it' if count == 1 else 'them'} automatically."
        ),
        expected="Every journal reference includes a DOI or PMID (or is verified via `ref-verify batch`)",
        actual=f"{count} unlinked bibliography entries (first {len(previews)}: {previews})",
        details={"count": count, "locations": [e.location for e in entries]},
        hint="Plain-text citations (e.g. Vancouver references without DOIs/PMIDs) are invisible to identifier-only scanning.",
        fix="Add DOIs/PMIDs to bibliography entries or export them to JSON and run `ref-verify batch <refs.json>`.",
    )


def format_unlinked_bibliography_text(diag: Diagnostic, entries: Sequence[UnlinkedEntry]) -> str:
    lines = [
        "-" * 78,
        f"[*] [{diag.severity}] {diag.code}: {diag.message}",
    ]
    for entry in entries[:10]:
        lines.append(f"    - {entry.location}: {_truncate(entry.text, 110)}")
    if len(entries) > 10:
        lines.append(f"    ... and {len(entries) - 10} more")
    if diag.hint:
        lines.append(f"    Hint: {diag.hint}")
    if diag.fix:
        lines.append(f"    Fix : {diag.fix}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Formatting: Structured Agent-Readable Report + JSON
# ---------------------------------------------------------------------------


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def format_result_text(res: VerificationResult, index: Optional[int] = None, show_abstract: bool = True) -> str:
    lines: List[str] = []
    prefix = f"[{index}] " if index is not None else ""
    label = (
        res.input.label
        or res.input.doi
        or (f"PMID:{res.input.pmid}" if res.input.pmid else res.input.title or "citation")
    )
    lines.append("=" * 78)
    lines.append(f"{prefix}STATUS: {res.status}  |  TARGET: {label}")
    if res.input.location:
        lines.append(f"LOCATION: {res.input.location}")

    # Resolved paper metadata block
    if res.resolved is not None:
        r = res.resolved
        lines.append("-" * 78)
        lines.append("OFFICIAL METADATA (Sources: " + ", ".join(r.sources) + "):")
        lines.append(f"  Title   : {r.title or 'N/A'}")
        if r.authors:
            au_preview = ", ".join(a.display for a in r.authors[:6])
            if len(r.authors) > 6:
                au_preview += f", ... ({len(r.authors)} authors)"
            lines.append(f"  Authors : {au_preview}")
        yr_parts = []
        if r.published_print:
            yr_parts.append(f"print={r.published_print}")
        if r.published_online:
            yr_parts.append(f"online={r.published_online}")
        if r.issued_year and r.issued_year not in (r.published_print, r.published_online):
            yr_parts.append(f"issued={r.issued_year}")
        lines.append(f"  Year    : {' / '.join(yr_parts) if yr_parts else 'N/A'}")
        j_str = r.journal_full or "N/A"
        if r.journal_abbrev and r.journal_abbrev != r.journal_full:
            j_str += f" ({r.journal_abbrev})"
        lines.append(f"  Journal : {j_str}")
        id_parts = []
        if r.doi:
            id_parts.append(f"DOI:{r.doi}")
        if r.pmid:
            id_parts.append(f"PMID:{r.pmid}")
        if r.pmcid:
            oa_tag = " [OA]" if r.is_open_access else ""
            id_parts.append(f"PMCID:{r.pmcid}{oa_tag}")
        lines.append(f"  IDs     : {' | '.join(id_parts) if id_parts else 'N/A'}")
        if r.retracted:
            lines.append("  *** RETRACTION STATUS: RETRACTED ***")

    # Diagnostics block
    if res.diagnostics:
        lines.append("-" * 78)
        lines.append(f"DIAGNOSTICS ({len(res.diagnostics)}):")
        for d in res.diagnostics:
            banner = (
                "!!!"
                if d.severity == "CRITICAL"
                else ("[!]" if d.severity == "ERROR" else ("[*]" if d.severity == "WARNING" else "[i]"))
            )
            lines.append(f"  {banner} [{d.severity}] {d.code}: {d.message}")
            if d.expected:
                lines.append(f"      Expected : {d.expected}")
            if d.actual:
                lines.append(f"      Actual   : {d.actual}")
            # Pinpointing diff extras
            if d.code == "TITLE_MISMATCH":
                extra = d.details.get("extra_tokens_in_cited") or []
                missing = d.details.get("missing_tokens_from_cited") or []
                if extra or missing:
                    lines.append(f"      TokenDiff: extra_in_cited={extra} | missing_from_cited={missing}")
            if d.code == "DOI_SYNTAX_INVALID" and d.details.get("cleaned_candidate"):
                lines.append(f"      Cleaned  : {d.details['cleaned_candidate']}")
            if d.hint:
                lines.append(f"      Hint     : {d.hint}")
            if d.fix:
                lines.append(f"      Fix      : {d.fix}")
    else:
        lines.append("-" * 78)
        lines.append("DIAGNOSTICS: None (all checked fields match authoritative metadata).")

    # DID_YOU_MEAN suggestions block
    if res.did_you_mean:
        lines.append("-" * 78)
        lines.append("DID_YOU_MEAN SUGGESTIONS:")
        for idx, s in enumerate(res.did_you_mean, start=1):
            lines.append(
                f"  {idx}. DOI: {s.get('doi') or 'N/A'} | PMID: {s.get('pmid') or 'N/A'} "
                f"| sim={s.get('title_similarity')} | {s.get('first_author') or '?'} ({s.get('year') or '?'})"
            )
            lines.append(f"     Title  : {s.get('title')}")
            if s.get("journal"):
                lines.append(f"     Journal: {s.get('journal')}")

    # Claim validity block
    if res.claim_report is not None:
        cr = res.claim_report
        lines.append("-" * 78)
        lines.append("CLAIM_CHECK:")
        lines.append(f"  Claim            : {cr.claim}")
        lines.append(f"  Numbers extracted: {cr.extracted_numbers or ['(none)']}")
        lines.append(f"  Numbers matched  : {cr.matched_numbers or ['(none)']}")
        lines.append(f"  Numbers MISSING  : {cr.missing_numbers or ['(none)']}")
        if cr.abstract_numbers:
            lines.append(f"  Abstract numbers : {cr.abstract_numbers[:20]}")
        if cr.extracted_keywords:
            lines.append(
                f"  Keywords matched : {len(cr.matched_keywords)}/{len(cr.extracted_keywords)} "
                f"({', '.join(cr.matched_keywords) if cr.matched_keywords else 'none'})"
            )
        if cr.fulltext_status != "not_requested":
            lines.append(f"  OA Fulltext      : {cr.fulltext_status}")
        if cr.matched_sentences:
            lines.append("  Matched Abstract Sentences:")
            for s in cr.matched_sentences:
                lines.append(f"    > {s}")
        if cr.fulltext_excerpts:
            lines.append("  Matched OA Full-Text Excerpts:")
            for ex in cr.fulltext_excerpts:
                lines.append(f"    > {_truncate(ex, 450)}")

    # Official abstract
    if show_abstract and res.resolved is not None and res.resolved.abstract:
        lines.append("-" * 78)
        lines.append("OFFICIAL ABSTRACT:")
        for abs_line in res.resolved.abstract.splitlines():
            lines.append(f"  {abs_line}")

    return "\n".join(lines)


def format_summary_text(results: Sequence[VerificationResult], header: str = "SUMMARY") -> str:
    total = len(results)
    passed = sum(1 for r in results if r.status == "PASS")
    warned = sum(1 for r in results if r.status == "WARN")
    failed = sum(1 for r in results if r.status == "FAIL")

    code_counts: Dict[str, int] = {}
    for r in results:
        for d in r.diagnostics:
            key = f"[{d.severity}] {d.code}"
            code_counts[key] = code_counts.get(key, 0) + 1

    lines = [
        "=" * 78,
        f"{header}: total={total} | PASS={passed} | WARN={warned} | FAIL={failed}",
    ]
    if code_counts:
        lines.append("DIAGNOSTIC COUNTS:")
        for k, v in sorted(code_counts.items()):
            lines.append(f"  {k}: {v}")
    lines.append("=" * 78)
    return "\n".join(lines)


def result_to_dict(res: VerificationResult) -> Dict[str, Any]:
    return asdict(res)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    cit = CitationInput(
        doi=args.doi,
        pmid=args.pmid,
        title=args.title,
        author=args.author,
        year=args.year,
        journal=args.journal,
        claim=args.claim,
        fulltext=args.fulltext,
    )
    http = HttpClient(timeout=args.timeout)
    res = verify_citation(cit, http)

    if args.json:
        print(json.dumps(result_to_dict(res), indent=2, ensure_ascii=False))
    else:
        print(format_result_text(res, show_abstract=not args.no_abstract))

    if res.status == "FAIL":
        return 1
    if args.strict and res.status == "WARN":
        return 1
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    try:
        if args.input == "-":
            raw = sys.stdin.read()
        else:
            raw = Path(args.input).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: Could not read batch JSON '{args.input}': {exc}", file=sys.stderr)
        return 2

    if isinstance(payload, dict):
        items = payload.get("references") or payload.get("citations") or payload.get("items")
    else:
        items = payload

    if not isinstance(items, list):
        print(
            "ERROR: Batch input must be a JSON array (or object with 'references'/'citations' array).", file=sys.stderr
        )
        return 2

    http = HttpClient(timeout=args.timeout)
    results: List[VerificationResult] = []
    for idx, entry in enumerate(items, start=1):
        if not isinstance(entry, dict):
            continue
        cit = CitationInput(
            doi=entry.get("doi"),
            pmid=str(entry["pmid"]) if entry.get("pmid") is not None else None,
            title=entry.get("title"),
            author=entry.get("author"),
            year=str(entry["year"]) if entry.get("year") is not None else None,
            journal=entry.get("journal"),
            claim=entry.get("claim"),
            label=entry.get("id") or entry.get("label") or f"ref-{idx}",
            fulltext=bool(entry.get("fulltext") or args.fulltext),
        )
        results.append(verify_citation(cit, http))

    if args.json:
        out = {
            "summary": {
                "total": len(results),
                "pass": sum(1 for r in results if r.status == "PASS"),
                "warn": sum(1 for r in results if r.status == "WARN"),
                "fail": sum(1 for r in results if r.status == "FAIL"),
            },
            "results": [result_to_dict(r) for r in results],
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        for i, res in enumerate(results, start=1):
            print(format_result_text(res, index=i, show_abstract=not args.no_abstract))
        print(format_summary_text(results, header="BATCH SUMMARY"))

    if any(r.status == "FAIL" for r in results):
        return 1
    if args.strict and any(r.status == "WARN" for r in results):
        return 1
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"ERROR: File not found: {path}", file=sys.stderr)
        return 2

    try:
        segments = extract_text_segments(path)
    except (OSError, zipfile.BadZipFile) as exc:
        print(f"ERROR: Could not read '{path}': {exc}", file=sys.stderr)
        return 2

    citations = extract_citations_from_segments(segments)
    unlinked_entries = extract_unlinked_bibliography_entries(segments)
    scan_diag = build_unlinked_bibliography_diagnostic(unlinked_entries)
    scan_diagnostics: List[Diagnostic] = [scan_diag] if scan_diag else []

    results: List[VerificationResult] = []
    if citations:
        http = HttpClient(timeout=args.timeout)
        for cit in citations:
            cit.fulltext = args.fulltext
            results.append(verify_citation(cit, http))

    if args.json:
        out: Dict[str, Any] = {
            "file": str(path),
            "extracted_count": len(citations),
            "unlinked_bibliography_count": len(unlinked_entries),
            "unlinked_bibliography_entries": [asdict(e) for e in unlinked_entries],
            "scan_diagnostics": [asdict(d) for d in scan_diagnostics],
        }
        if citations:
            out["summary"] = {
                "total": len(results),
                "pass": sum(1 for r in results if r.status == "PASS"),
                "warn": sum(1 for r in results if r.status == "WARN"),
                "fail": sum(1 for r in results if r.status == "FAIL"),
            }
        out["results"] = [result_to_dict(r) for r in results]
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        if not citations:
            print(f"SCAN: No DOIs or PMIDs found in '{path}'.")
        else:
            print(f"SCAN FILE: {path}  |  EXTRACTED REFERENCES: {len(citations)}")
            for i, res in enumerate(results, start=1):
                print(format_result_text(res, index=i, show_abstract=not args.no_abstract))
        if scan_diag:
            print(format_unlinked_bibliography_text(scan_diag, unlinked_entries))
        if citations:
            print(format_summary_text(results, header="SCAN SUMMARY"))

    if any(r.status == "FAIL" for r in results):
        return 1
    if args.strict and (any(r.status == "WARN" for r in results) or scan_diagnostics):
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ref-verify",
        description=(
            "Verify scientific references (DOI/PMID), paper metadata, retraction status, "
            "and quantitative claims against Crossref, PubMed, and Europe PMC with pinpointing diagnostics."
        ),
    )
    parser.add_argument("--version", action="version", version=f"ref-verify {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    # check
    p_check = sub.add_parser("check", help="Verify a single citation and optional quantitative claim.")
    p_check.add_argument("--doi", help="DOI (e.g. 10.1056/NEJMoa1911303)")
    p_check.add_argument("--pmid", help="PubMed ID (e.g. 31535829)")
    p_check.add_argument("--title", help="Cited paper title to cross-check (and search if DOI/PMID not found)")
    p_check.add_argument("--author", help="Cited first author surname or name (e.g. 'McMurray' or 'McMurray JJV')")
    p_check.add_argument("--year", help="Cited publication year (e.g. 2019)")
    p_check.add_argument("--journal", help="Cited journal name or abbreviation (e.g. 'NEJM' or 'N Engl J Med')")
    p_check.add_argument("--claim", help="Specific claim to verify against official abstract (and OA full text)")
    p_check.add_argument(
        "--fulltext",
        action="store_true",
        help="Also fetch Open Access JATS full-text XML (Europe PMC with NCBI PMC fallback) if available",
    )
    p_check.add_argument("--no-abstract", action="store_true", help="Omit full official abstract from text output")
    p_check.add_argument("--json", action="store_true", help="Emit machine-readable JSON output")
    p_check.add_argument("--strict", action="store_true", help="Exit non-zero on WARNING diagnostics too")
    p_check.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"HTTP timeout in seconds (default {DEFAULT_TIMEOUT})"
    )
    p_check.set_defaults(func=cmd_check)

    # batch
    p_batch = sub.add_parser("batch", help="Verify a JSON array of citations/claims from a file (or '-' for stdin).")
    p_batch.add_argument("input", help="Path to JSON file containing an array of citation objects (or '-' for stdin)")
    p_batch.add_argument(
        "--fulltext",
        action="store_true",
        help="Fetch Open Access JATS full-text XML (Europe PMC with NCBI PMC fallback) for all claims",
    )
    p_batch.add_argument("--no-abstract", action="store_true", help="Omit full official abstract from text output")
    p_batch.add_argument("--json", action="store_true", help="Emit machine-readable JSON output")
    p_batch.add_argument("--strict", action="store_true", help="Exit non-zero on WARNING diagnostics too")
    p_batch.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"HTTP timeout in seconds (default {DEFAULT_TIMEOUT})"
    )
    p_batch.set_defaults(func=cmd_batch)

    # scan
    p_scan = sub.add_parser("scan", help="Auto-extract and verify all DOIs and PMIDs from .md/.txt/.docx/.pptx files.")
    p_scan.add_argument("file", help="Path to .md, .txt, .docx, or .pptx file")
    p_scan.add_argument(
        "--fulltext",
        action="store_true",
        help="Check Open Access JATS full-text XML availability (Europe PMC with NCBI PMC fallback)",
    )
    p_scan.add_argument("--no-abstract", action="store_true", help="Omit full official abstract from text output")
    p_scan.add_argument("--json", action="store_true", help="Emit machine-readable JSON output")
    p_scan.add_argument("--strict", action="store_true", help="Exit non-zero on WARNING diagnostics too")
    p_scan.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"HTTP timeout in seconds (default {DEFAULT_TIMEOUT})"
    )
    p_scan.set_defaults(func=cmd_scan)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
