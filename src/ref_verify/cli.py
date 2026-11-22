#!/usr/bin/env python3
"""ref-verify: Live scientific reference & claim verifier with pinpointing diagnostics.

Zero external dependencies (pure Python 3 standard library).
Authoritative lookups: Crossref REST API, NCBI PubMed E-utilities, Europe PMC REST API.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import difflib
import gzip
import html
import http.client
import json
import os
import re
import ssl
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from ref_verify import __version__
except ImportError:
    __version__ = "0.1.0"

_MAILTO = os.environ.get("REF_VERIFY_MAILTO", "").strip() or "ref-verify@users.noreply.github.com"
USER_AGENT = (
    f"ref-verify/{__version__} "
    f"(scientific-citation-verifier; https://github.com/heyiamnotacoder/ref-verify; mailto:{_MAILTO})"
)
DEFAULT_TIMEOUT = 15.0
DEFAULT_WORKERS = 8

CROSSREF_SELECT_FIELDS = (
    "DOI,title,author,published-print,published-online,issued,published,"
    "container-title,short-container-title,abstract,update-to,updated-by,"
    "relation,is-referenced-by-count"
)
CROSSREF_BATCH_SIZE = 25
EPMC_BATCH_SIZE = 20
PUBMED_BATCH_SIZE = 100
PMC_FULLTEXT_BATCH_SIZE = 12

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


_JOURNAL_ALIAS_LOOKUP: Dict[str, str] = {
    normalize_journal(v): key for key, variants in JOURNAL_ALIASES.items() for v in variants
}


def journal_alias_group(name: str) -> Optional[str]:
    return _JOURNAL_ALIAS_LOOKUP.get(normalize_journal(name))


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
# HTTP Client with Keep-Alive pooling, gzip, caching, retries & NCBI pacing
# ---------------------------------------------------------------------------


@dataclass
class HttpResponse:
    status: int
    body: str
    url: str
    error: Optional[str] = None


def _decompress_body(raw: bytes, content_encoding: str) -> bytes:
    enc = (content_encoding or "").lower()
    if "gzip" in enc:
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw
    if "deflate" in enc:
        try:
            return zlib.decompress(raw)
        except zlib.error:
            try:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
            except zlib.error:
                return raw
    return raw


def _is_transient_http(resp: Optional[HttpResponse]) -> bool:
    return resp is not None and (resp.status == 0 or resp.status in (429, 500, 502, 503, 504))


class HttpClient:
    FALLBACK_TIMEOUT = 4.0

    def __init__(self, timeout: float = DEFAULT_TIMEOUT, retries: int = 2) -> None:
        self.timeout = timeout
        self.retries = retries
        self.ncbi_api_key = os.environ.get("NCBI_API_KEY", "").strip()
        self._ncbi_lock = threading.Lock()
        self._next_ncbi_slot = 0.0
        self._pool_lock = threading.Lock()
        self._pools: Dict[Tuple[str, str, int], List[http.client.HTTPConnection]] = {}
        self._ssl_ctx: Optional[ssl.SSLContext] = None
        self._cache_lock = threading.Lock()
        self._cache: Dict[Tuple[str, str], HttpResponse] = {}
        self._inflight: Dict[Tuple[str, str], threading.Event] = {}
        self._has_proxies = bool(urllib.request.getproxies())

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        with self._pool_lock:
            for conns in self._pools.values():
                for conn in conns:
                    try:
                        conn.close()
                    except Exception:
                        pass
            self._pools.clear()

    def _get_ssl_context(self) -> ssl.SSLContext:
        with self._pool_lock:
            if self._ssl_ctx is None:
                self._ssl_ctx = ssl.create_default_context()
            return self._ssl_ctx

    def _borrow_conn(
        self, scheme: str, host: str, port: int, timeout: float, force_fresh: bool = False
    ) -> Tuple[http.client.HTTPConnection, bool]:
        key = (scheme, host, port)
        if not force_fresh:
            with self._pool_lock:
                bucket = self._pools.get(key)
                if bucket:
                    conn = bucket.pop()
                    conn.timeout = timeout
                    if conn.sock is not None:
                        conn.sock.settimeout(timeout)
                    return conn, True
        if scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=timeout, context=self._get_ssl_context()), False
        return http.client.HTTPConnection(host, port, timeout=timeout), False

    def _return_conn(self, scheme: str, host: str, port: int, conn: http.client.HTTPConnection) -> None:
        key = (scheme, host, port)
        with self._pool_lock:
            bucket = self._pools.setdefault(key, [])
            if len(bucket) < 16:
                bucket.append(conn)
                return
        try:
            conn.close()
        except Exception:
            pass

    def _pace_ncbi(self) -> None:
        min_gap = 0.11 if self.ncbi_api_key else 0.34
        with self._ncbi_lock:
            now = time.monotonic()
            slot = max(now, self._next_ncbi_slot)
            self._next_ncbi_slot = slot + min_gap
            wait_s = slot - now
        if wait_s > 0:
            time.sleep(wait_s)

    def _single_request_pooled(
        self, url: str, req_headers: Dict[str, str], timeout: float
    ) -> Tuple[int, str, str, Optional[str], Optional[str]]:
        """Perform one GET with Keep-Alive connection reuse & redirect handling.

        Returns (status, body, final_url, reason, retry_after).
        """
        cur_url = url
        for _ in range(6):
            parsed = urllib.parse.urlsplit(cur_url)
            scheme = (parsed.scheme or "https").lower()
            host = parsed.hostname or ""
            port = parsed.port or (443 if scheme == "https" else 80)
            target = parsed.path or "/"
            if parsed.query:
                target = f"{target}?{parsed.query}"

            stale_retried = False
            while True:
                conn, reused = self._borrow_conn(scheme, host, port, timeout, force_fresh=stale_retried)
                try:
                    conn.request("GET", target, headers=req_headers)
                    resp = conn.getresponse()
                    raw = resp.read()
                    status = resp.status
                    reason = resp.reason
                    headers_msg = resp.headers
                    should_close = resp.will_close or (headers_msg.get("Connection") or "").lower() == "close"
                except (
                    http.client.RemoteDisconnected,
                    http.client.BadStatusLine,
                    BrokenPipeError,
                    ConnectionResetError,
                    ssl.SSLError,
                ):
                    try:
                        conn.close()
                    except Exception:
                        pass
                    if reused and not stale_retried:
                        stale_retried = True
                        continue
                    raise
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    raise

                if should_close:
                    try:
                        conn.close()
                    except Exception:
                        pass
                else:
                    self._return_conn(scheme, host, port, conn)
                break

            if status in (301, 302, 303, 307, 308):
                loc = headers_msg.get("Location")
                if loc:
                    cur_url = urllib.parse.urljoin(cur_url, loc)
                    continue

            decompressed = _decompress_body(raw, headers_msg.get("Content-Encoding") or "")
            charset = headers_msg.get_content_charset() or "utf-8"
            body = decompressed.decode(charset, errors="replace")
            return status, body, cur_url, reason, headers_msg.get("Retry-After")

        return 0, "", cur_url, "Too many redirects", None

    def _single_request_urllib(
        self, url: str, req_headers: Dict[str, str], timeout: float
    ) -> Tuple[int, str, str, Optional[str], Optional[str]]:
        req = urllib.request.Request(url, headers=req_headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                decompressed = _decompress_body(raw, resp.headers.get("Content-Encoding") or "")
                charset = resp.headers.get_content_charset() or "utf-8"
                return getattr(resp, "status", 200), decompressed.decode(charset, errors="replace"), url, None, None
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
                decompressed = _decompress_body(raw, exc.headers.get("Content-Encoding") if exc.headers else "")
                body = decompressed.decode("utf-8", errors="replace")
            except Exception:
                body = ""
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            return exc.code, body, url, str(exc.reason), retry_after

    def get_fallback(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        *,
        primary_failed_transient: bool = False,
    ) -> HttpResponse:
        """GET for secondary fallback sources with policy confined inside HttpClient.

        Uses full timeout/retries when the primary source suffered a transient outage (status 0/429/5xx),
        and bounded timeout with zero retries for best-effort enrichment/fallback otherwise.
        Delegates to get() for subclasses/test mocks.
        """
        if type(self) is not HttpClient:
            return self.get(url, headers=headers)
        if primary_failed_transient:
            return self._get_impl(url, headers=headers, timeout=self.timeout, retries=self.retries)
        return self._get_impl(url, headers=headers, timeout=min(self.timeout, self.FALLBACK_TIMEOUT), retries=0)

    def get(self, url: str, headers: Optional[Dict[str, str]] = None) -> HttpResponse:
        return self._get_impl(url, headers=headers, timeout=self.timeout, retries=self.retries)

    def _get_impl(
        self,
        url: str,
        headers: Optional[Dict[str, str]],
        timeout: float,
        retries: int,
    ) -> HttpResponse:
        is_ncbi = "eutils.ncbi.nlm.nih.gov" in url
        if is_ncbi and self.ncbi_api_key and "api_key=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}api_key={urllib.parse.quote(self.ncbi_api_key)}"

        req_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }
        if headers:
            req_headers.update(headers)

        cache_key = (url, req_headers.get("Accept", "*/*"))
        while True:
            with self._cache_lock:
                cached = self._cache.get(cache_key)
                if cached is not None:
                    return cached
                inflight_evt = self._inflight.get(cache_key)
                if inflight_evt is None:
                    inflight_evt = threading.Event()
                    self._inflight[cache_key] = inflight_evt
                    break
            inflight_evt.wait()

        result: Optional[HttpResponse] = None
        try:
            if is_ncbi:
                self._pace_ncbi()

            attempt = 0
            last_err = ""
            while attempt <= retries:
                try:
                    if self._has_proxies:
                        status, body, final_url, reason, retry_after = self._single_request_urllib(
                            url, req_headers, timeout
                        )
                    else:
                        status, body, final_url, reason, retry_after = self._single_request_pooled(
                            url, req_headers, timeout
                        )
                except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
                    last_err = str(exc)
                    if attempt < retries:
                        time.sleep(0.5 * (2**attempt))
                        attempt += 1
                        if is_ncbi:
                            self._pace_ncbi()
                        continue
                    result = HttpResponse(status=0, body="", url=url, error=last_err)
                    break

                if status in (429, 500, 502, 503, 504) and attempt < retries:
                    delay = float(retry_after) if retry_after and retry_after.isdigit() else (0.7 * (2**attempt))
                    time.sleep(min(delay, 5.0))
                    attempt += 1
                    last_err = f"HTTP {status}"
                    if is_ncbi:
                        self._pace_ncbi()
                    continue

                if 200 <= status < 300:
                    result = HttpResponse(status=status, body=body, url=final_url)
                elif status == 0:
                    result = HttpResponse(
                        status=0, body="", url=final_url, error=reason or last_err or "request failed"
                    )
                else:
                    result = HttpResponse(
                        status=status,
                        body=body,
                        url=final_url,
                        error=f"HTTP {status} {reason or ''}".strip(),
                    )
                break

            if result is None:
                result = HttpResponse(status=0, body="", url=url, error=last_err or "request failed")
            return result
        finally:
            with self._cache_lock:
                if result is not None and result.status in (200, 404):
                    self._cache[cache_key] = result
                evt = self._inflight.pop(cache_key, None)
                if evt is not None:
                    evt.set()


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


def fetch_crossref_dois_batch(
    http: HttpClient, dois: Sequence[str], max_workers: int = 4
) -> Tuple[Dict[str, Tuple[PaperRecord, HttpResponse]], bool]:
    """Batch-fetch Crossref works via filter=doi:...&select=... in chunks.

    Returns (doi_lower -> (PaperRecord, HttpResponse), all_chunks_ok).
    DOIs containing commas (Crossref filter delimiter) are skipped for single-DOI fallback.
    """
    unique: List[str] = []
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

    def _fetch_chunk(chunk: List[str]) -> Tuple[Dict[str, Tuple[PaperRecord, HttpResponse]], bool]:
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
        out: Dict[str, Tuple[PaperRecord, HttpResponse]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            rec = parse_crossref_work(item)
            if rec.doi:
                out[rec.doi.lower()] = (rec, resp)
        return out, True

    found: Dict[str, Tuple[PaperRecord, HttpResponse]] = {}
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
    http: HttpClient, title: str, author: Optional[str] = None, rows: int = 8
) -> List[Dict[str, Any]]:
    params = {"query.title": title, "rows": str(rows), "select": CROSSREF_SELECT_FIELDS}
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


def _parse_pubmed_article_node(article_node: ET.Element) -> PaperRecord:
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


def parse_pubmed_xml(xml_text: str) -> Optional[PaperRecord]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    article_node = root.find(".//PubmedArticle")
    if article_node is None:
        return None

    return _parse_pubmed_article_node(article_node)


def _parse_pubmed_xml_many_checked(xml_text: str) -> Tuple[Dict[str, PaperRecord], bool]:
    """Parse all <PubmedArticle> elements, returning (records_by_pmid, xml_valid)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}, False
    out: Dict[str, PaperRecord] = {}
    for article_node in root.findall(".//PubmedArticle"):
        rec = _parse_pubmed_article_node(article_node)
        if rec.pmid:
            out[rec.pmid] = rec
    return out, True


def parse_pubmed_xml_many(xml_text: str) -> Dict[str, PaperRecord]:
    """Parse all <PubmedArticle> elements in a batched efetch XML response, keyed by PMID."""
    parsed, _ = _parse_pubmed_xml_many_checked(xml_text)
    return parsed


def fetch_pubmed_pmid(http: HttpClient, pmid: str) -> Tuple[Optional[PaperRecord], HttpResponse]:
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
) -> Tuple[Dict[str, Tuple[Optional[PaperRecord], HttpResponse]], bool]:
    """Batch-fetch up to PUBMED_BATCH_SIZE PMIDs per single efetch XML request."""
    unique = list(dict.fromkeys(p for p in pmids if p))
    if not unique:
        return {}, True
    if len(unique) == 1:
        rec, resp = fetch_pubmed_pmid(http, unique[0])
        return {unique[0]: (rec, resp)}, resp.status == 200

    out: Dict[str, Tuple[Optional[PaperRecord], HttpResponse]] = {}
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


def pubmed_esearch_by_dois_batch(http: HttpClient, dois: Sequence[str]) -> Tuple[List[str], bool]:
    """Batch-discover PMIDs for DOIs via OR-combined PubMed esearch queries."""
    unique = list(dict.fromkeys(d for d in dois if d))
    if not unique:
        return [], True
    if len(unique) == 1:
        term = f'"{unique[0]}"[DOI]'
        url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urllib.parse.urlencode(
            {"db": "pubmed", "term": term, "retmode": "json", "retmax": "2"}
        )
        resp = http.get(url)
        if resp.status != 200:
            return [], False
        try:
            ids = json.loads(resp.body).get("esearchresult", {}).get("idlist", [])
            return ([str(ids[0])] if ids else []), True
        except json.JSONDecodeError:
            return [], False

    pmids: List[str] = []
    seen: set[str] = set()
    all_ok = True
    for i in range(0, len(unique), 25):
        chunk = unique[i : i + 25]
        retmax = len(chunk) * 2
        term = " OR ".join(f'"{d}"[DOI]' for d in chunk)
        url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?" + urllib.parse.urlencode(
            {"db": "pubmed", "term": term, "retmode": "json", "retmax": str(retmax)}
        )
        resp = http.get(url)
        if resp.status != 200:
            all_ok = False
            continue
        try:
            es_res = json.loads(resp.body).get("esearchresult", {})
            ids = es_res.get("idlist", [])
            total_count = int(es_res.get("count", len(ids)) or 0)
        except (json.JSONDecodeError, TypeError, ValueError):
            all_ok = False
            continue
        if total_count > retmax:
            # Truncated result set: mark incomplete so missed DOIs still fall back individually.
            all_ok = False
        for pid in ids:
            sp = str(pid)
            if sp not in seen:
                seen.add(sp)
                pmids.append(sp)
    return pmids, all_ok


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
    http: HttpClient,
    doi: Optional[str] = None,
    pmid: Optional[str] = None,
    *,
    fallback: bool = False,
    primary_failed_transient: bool = False,
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
) -> Tuple[
    Dict[str, Tuple[Optional[PaperRecord], HttpResponse]],
    Dict[str, Tuple[Optional[PaperRecord], HttpResponse]],
]:
    """Batch-fetch Europe PMC core records for DOIs and PMIDs using OR queries.

    Returns (by_doi_lower, by_pmid). Definitive misses from complete HTTP 200 pages
    are stored as (None, resp) so callers avoid redundant single-item re-queries.
    """
    terms: List[Tuple[str, str, str]] = []
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
        chunk: List[Tuple[str, str, str]],
    ) -> Tuple[
        Dict[str, Tuple[Optional[PaperRecord], HttpResponse]],
        Dict[str, Tuple[Optional[PaperRecord], HttpResponse]],
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

        c_doi: Dict[str, Tuple[Optional[PaperRecord], HttpResponse]] = {}
        c_pmid: Dict[str, Tuple[Optional[PaperRecord], HttpResponse]] = {}
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

    by_doi: Dict[str, Tuple[Optional[PaperRecord], HttpResponse]] = {}
    by_pmid: Dict[str, Tuple[Optional[PaperRecord], HttpResponse]] = {}
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


@dataclass
class FulltextResult:
    paragraphs: List[str] = field(default_factory=list)
    source: Optional[str] = None
    error: Optional[str] = None
    is_transient: bool = False
    missing_body: bool = False


def _extract_paragraphs_from_article_node(node: ET.Element, source_label: str) -> Tuple[List[str], Optional[str], bool]:
    body = node.find(".//body")
    if body is None:
        return [], f"{source_label} XML has no <body> element (likely not licensed for OA full-text XML)", True

    paragraphs: List[str] = []
    for p in body.findall(".//p"):
        txt = re.sub(r"\s+", " ", "".join(p.itertext())).strip()
        if len(txt) >= 30:
            paragraphs.append(txt)
    if not paragraphs:
        return [], f"{source_label} XML <body> contained no text paragraphs", False
    return paragraphs, None, False


def _parse_jats_body_paragraphs(xml_body: str, source_label: str) -> Tuple[List[str], Optional[str], bool]:
    """Extract body paragraphs from JATS full-text XML (Europe PMC or NCBI PMC)."""
    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError as exc:
        return [], f"Failed to parse {source_label} XML: {exc}", False
    return _extract_paragraphs_from_article_node(root, source_label)


def parse_pmc_xml_many(xml_body: str) -> Dict[str, FulltextResult]:
    """Parse multiple <article> elements from a batched NCBI PMC efetch XML response, keyed by PMCID."""
    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError:
        return {}

    articles = root.findall("./article")
    if not articles and root.tag == "article":
        articles = [root]

    out: Dict[str, FulltextResult] = {}
    for art in articles:
        pmcid: Optional[str] = None
        for aid in art.findall(".//front//article-id"):
            ptype = (aid.attrib.get("pub-id-type") or "").lower()
            if ptype in ("pmc", "pmcid") and aid.text:
                raw_id = aid.text.strip().upper()
                pmcid = raw_id if raw_id.startswith("PMC") else f"PMC{raw_id}"
                break
        if not pmcid:
            continue
        paragraphs, err, missing_body = _extract_paragraphs_from_article_node(art, "NCBI PMC efetch")
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
        return FulltextResult(error=_format_http_failure(source_label, resp), is_transient=_is_transient_http(resp))
    paragraphs, err, missing_body = _parse_jats_body_paragraphs(resp.body, source_label)
    if err or not paragraphs:
        return FulltextResult(error=err or f"{source_label} returned empty body", missing_body=missing_body)
    return FulltextResult(paragraphs=paragraphs, source=source_name)


def fetch_epmc_fulltext_paragraphs(http: HttpClient, pmcid: str) -> Tuple[List[str], Optional[str]]:
    url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{urllib.parse.quote(pmcid)}/fullTextXML"
    res = _fetch_jats_fulltext(http, url, "Europe PMC fullTextXML", "Europe PMC")
    return res.paragraphs, res.error


def _resolve_fulltext_with_epmc_fallback(
    http: HttpClient,
    pmcid: str,
    ncbi_res: FulltextResult,
    is_open_access: Optional[bool] = None,
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


def fetch_oa_fulltext_paragraphs(http: HttpClient, pmcid: str, is_open_access: Optional[bool] = None) -> FulltextResult:
    """Fetch OA JATS full-text paragraphs from NCBI PMC first, falling back to Europe PMC."""
    ncbi_url = (
        f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pmc&id={urllib.parse.quote(pmcid)}&retmode=xml"
    )
    ncbi_res = _fetch_jats_fulltext(http, ncbi_url, "NCBI PMC efetch", "NCBI PMC")
    return _resolve_fulltext_with_epmc_fallback(http, pmcid, ncbi_res, is_open_access=is_open_access)


def fetch_oa_fulltext_batch(
    http: HttpClient,
    pmcids: Sequence[str],
    oa_flags: Optional[Dict[str, Optional[bool]]] = None,
    max_workers: int = 4,
) -> Dict[str, FulltextResult]:
    """Batch-fetch OA full-text JATS XML via NCBI PMC efetch, with concurrent Europe PMC fallback."""
    unique = list(dict.fromkeys(p.strip().upper() for p in pmcids if p and p.strip()))
    if not unique:
        return {}
    if len(unique) == 1:
        pid = unique[0]
        return {pid: fetch_oa_fulltext_paragraphs(http, pid, is_open_access=(oa_flags or {}).get(pid))}

    out: Dict[str, FulltextResult] = {}
    need_fallback: List[Tuple[str, FulltextResult]] = []

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
                is_transient=_is_transient_http(resp),
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

        def _fallback_one(item: Tuple[str, FulltextResult]) -> Tuple[str, FulltextResult]:
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


def _normalize_for_quant(text: str) -> Tuple[str, str]:
    """Return (lowercase_normalized, comma_stripped_normalized) for quantitative token matching."""
    norm_text = normalize_text(text)
    norm_no_commas = re.sub(r"(?<=\d),(?=\d{3}\b)", "", norm_text)
    return norm_text.lower(), norm_no_commas


def _quant_token_in_norm_text(
    token: str,
    norm_text_low: str,
    norm_no_commas: str,
) -> bool:
    is_pct = "%" in token and "ci" not in token.lower()
    subvals = _numeric_subvalues(token)
    if not subvals:
        return token.lower() in norm_text_low
    return all(_number_present_in_text(v, norm_no_commas, is_percent=is_pct) for v in subvals)


def quant_token_in_text(token: str, text: str) -> bool:
    """Check if a quantitative token (including all its key numeric values) is present in text."""
    norm_low, norm_no_commas = _normalize_for_quant(text)
    return _quant_token_in_norm_text(token, norm_low, norm_no_commas)


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
    kw_lows = [kw.lower() for kw in keywords]
    for idx, sent in enumerate(text_blocks):
        score = 0
        if quant_tokens:
            norm_low, norm_no_commas = _normalize_for_quant(sent)
            for qt in quant_tokens:
                if _quant_token_in_norm_text(qt, norm_low, norm_no_commas):
                    score += 3
        low = sent.lower()
        for kw_low in kw_lows:
            if kw_low in low:
                score += 1
        if score > 0:
            scored.append((score, -idx, sent))
    scored.sort(reverse=True)
    return [s for _, _, s in scored[:max_items]]


# ---------------------------------------------------------------------------
# Claim Cohort / Subgroup Grounding
# ---------------------------------------------------------------------------

COHORT_SYNONYM_GROUPS: Dict[str, Tuple[str, ...]] = {
    "t2dm": ("type 2 diabetes", "type 2 diabetes mellitus", "t2dm", "t2d"),
    "t1dm": ("type 1 diabetes", "type 1 diabetes mellitus", "t1dm", "t1d"),
    "diabetes": ("diabetes", "diabetic", "diabetics", "diabetes mellitus", "dm"),
    "hfref": (
        "hfref",
        "heart failure with reduced ejection fraction",
        "heart failure and reduced ejection fraction",
        "reduced ejection fraction",
    ),
    "hfpef": (
        "hfpef",
        "heart failure with preserved ejection fraction",
        "heart failure and preserved ejection fraction",
        "preserved ejection fraction",
    ),
    "heart_failure": ("heart failure", "chf", "hf"),
    "ckd": (
        "chronic kidney disease",
        "ckd",
        "renal impairment",
        "kidney disease",
        "renal insufficiency",
        "renal disease",
    ),
    "esrd": ("end stage renal disease", "end stage kidney disease", "esrd", "eskd", "dialysis"),
    "afib": ("atrial fibrillation", "afib", "af"),
    "hypertension": ("hypertension", "hypertensive", "htn", "high blood pressure"),
    "cad": ("coronary artery disease", "cad"),
    "mi": ("myocardial infarction", "mi"),
    "copd": ("chronic obstructive pulmonary disease", "copd"),
    "atopic_dermatitis": ("atopic dermatitis", "eczema"),
    "ra": ("rheumatoid arthritis",),
    "ibd": ("inflammatory bowel disease", "ibd"),
    "nsclc": ("non small cell lung cancer", "nsclc"),
    "hcc": ("hepatocellular carcinoma", "hcc"),
    "aki": ("acute kidney injury", "aki"),
    "vte": ("venous thromboembolism", "vte"),
    "women": ("women", "woman", "female", "females"),
    "men": ("men", "man", "male", "males"),
    "pediatric": (
        "pediatric",
        "paediatric",
        "children",
        "child",
        "childhood",
        "infants",
        "infant",
        "neonates",
        "neonate",
    ),
    "adolescents": ("adolescents", "adolescent"),
    "adults": ("adults", "adult"),
    "elderly": ("elderly", "geriatric", "older adults", "older patients", "older"),
    "younger": ("younger", "younger adults", "younger patients"),
    "obesity": ("obese", "obesity"),
    "smoking": ("smokers", "smoker", "smoking"),
    "cirrhosis": ("cirrhosis", "cirrhotic"),
    "metastatic": ("metastatic", "metastasis", "metastases"),
    "pregnancy": ("pregnant", "pregnancy"),
    "inpatient": ("hospitalized", "hospitalised", "inpatient", "inpatients"),
    "outpatient": ("outpatient", "outpatients", "ambulatory"),
    "asymptomatic": ("asymptomatic",),
    "symptomatic": ("symptomatic",),
}

_COHORT_PHRASE_TO_GROUP: Dict[str, str] = {}
_COHORT_SINGLE_TO_GROUP: Dict[str, str] = {}
for _grp, _aliases in COHORT_SYNONYM_GROUPS.items():
    for _al in _aliases:
        _norm_al = re.sub(r"[^a-z0-9\s]", " ", _al.lower())
        _norm_al = re.sub(r"\s+", " ", _norm_al).strip()
        if " " in _norm_al:
            _COHORT_PHRASE_TO_GROUP[_norm_al] = _grp
        else:
            _COHORT_SINGLE_TO_GROUP[_norm_al] = _grp

# Ordered longest-first so "type 2 diabetes" matches before "diabetes"
_COHORT_MULTI_PHRASES: Tuple[Tuple[str, str, re.Pattern[str]], ...] = tuple(
    (
        phrase,
        grp,
        re.compile(rf"\b{re.escape(phrase)}\b", re.IGNORECASE),
    )
    for phrase, grp in sorted(_COHORT_PHRASE_TO_GROUP.items(), key=lambda kv: -len(kv[0]))
)

COHORT_OPPOSITE_PAIRS: Tuple[Tuple[str, str], ...] = (
    ("women", "men"),
    ("hfref", "hfpef"),
    ("t1dm", "t2dm"),
    ("pediatric", "adults"),
    ("pediatric", "elderly"),
    ("younger", "elderly"),
    ("inpatient", "outpatient"),
    ("asymptomatic", "symptomatic"),
)
_COHORT_OPPOSITES_MAP: Dict[str, set[str]] = {}
for _a, _b in COHORT_OPPOSITE_PAIRS:
    _COHORT_OPPOSITES_MAP.setdefault(_a, set()).add(_b)
    _COHORT_OPPOSITES_MAP.setdefault(_b, set()).add(_a)

# Negated cohort adjectives -> positive canonical group or stem
NEGATED_COHORT_ADJECTIVES: Dict[str, str] = {
    "nondiabetic": "diabetes",
    "non-diabetic": "diabetes",
    "normotensive": "hypertension",
    "nonhypertensive": "hypertension",
    "non-hypertensive": "hypertension",
    "nonobese": "obesity",
    "non-obese": "obesity",
    "nonsmoker": "smoking",
    "nonsmokers": "smoking",
    "non-smoker": "smoking",
    "non-smokers": "smoking",
    "nonmetastatic": "metastatic",
    "non-metastatic": "metastatic",
    "noncirrhotic": "cirrhosis",
    "non-cirrhotic": "cirrhosis",
    "nonpregnant": "pregnancy",
    "non-pregnant": "pregnancy",
    "nonhospitalized": "inpatient",
    "non-hospitalized": "inpatient",
    "nonhospitalised": "inpatient",
    "non-hospitalised": "inpatient",
    "uninfected": "infection",
    "unvaccinated": "vaccination",
}

TREATMENT_ARM_WORDS = {
    "placebo",
    "control",
    "controls",
    "controlled",
    "active",
    "treatment",
    "treated",
    "intervention",
    "experimental",
    "standard",
    "usual",
    "care",
    "vehicle",
    "sham",
    "comparator",
    "reference",
    "arm",
    "arms",
    "group",
    "groups",
    "dose",
    "doses",
    "daily",
    "weekly",
    "monthly",
    "once",
    "twice",
    "oral",
    "topical",
    "intravenous",
    "subcutaneous",
    "therapy",
    "regimen",
    "monotherapy",
    "combination",
    "assigned",
    "randomized",
    "randomised",
    "enrolled",
    "included",
    "analyzed",
    "analysed",
    "evaluated",
    "eligible",
    "receiving",
    "received",
    "administered",
}

COHORT_GENERIC_WORDS = {
    "patient",
    "patients",
    "participant",
    "participants",
    "subject",
    "subjects",
    "individual",
    "individuals",
    "person",
    "persons",
    "people",
    "those",
    "case",
    "cases",
    "recipient",
    "recipients",
    "cohort",
    "population",
    "subgroup",
    "subgroups",
    "subcohort",
    "subpopulation",
    "group",
    "groups",
    "status",
    "history",
    "diagnosis",
    "disease",
    "disorder",
    "syndrome",
    "condition",
    "chronic",
    "acute",
    "primary",
    "secondary",
    "baseline",
    "prespecified",
    "pre",
    "specified",
    "post",
    "hoc",
    "exploratory",
    "other",
    "with",
    "without",
    "having",
    "lacking",
    "and",
    "or",
    "of",
    "the",
    "a",
    "an",
    "in",
    "on",
    "at",
    "to",
    "for",
    "by",
    "who",
    "were",
    "was",
    "had",
    "have",
    "all",
    "any",
    "no",
    "not",
    "same",
    "this",
    "that",
    "each",
    "overall",
    "level",
    "levels",
    "expression",
    "score",
    "scores",
    "stage",
    "class",
    "grade",
    "type",
    "years",
    "year",
    "yrs",
    "aged",
    "age",
}

NON_COHORT_WITH_WORDS = {
    "median",
    "mean",
    "follow",
    "followup",
    "event",
    "events",
    "outcome",
    "outcomes",
    "endpoint",
    "endpoints",
    "data",
    "result",
    "results",
    "dose",
    "doses",
    "response",
    "survival",
    "mortality",
    "death",
    "hazard",
    "odds",
    "ratio",
    "risk",
    "rate",
    "rates",
    "change",
    "reduction",
    "increase",
    "benefit",
    "effect",
    "available",
    "complete",
    "missing",
    "valid",
    "evaluable",
    "months",
    "weeks",
    "days",
    "adverse",
    "serious",
    "side",
    "reaction",
    "reactions",
    "complication",
    "complications",
    "toxicity",
    "toxicities",
}

_COHORT_GROUP_PARENTS: Dict[str, str] = {
    "t2dm": "diabetes",
    "t1dm": "diabetes",
    "hfref": "heart_failure",
    "hfpef": "heart_failure",
    "esrd": "ckd",
}

_POSITIVE_COHORT_ADJECTIVES: Tuple[str, ...] = (
    "diabetic",
    "hypertensive",
    "obese",
    "pregnant",
    "postmenopausal",
    "premenopausal",
    "older",
    "younger",
    "geriatric",
    "pediatric",
    "paediatric",
    "elderly",
    "hospitalized",
    "hospitalised",
    "ambulatory",
    "immunocompromised",
    "asymptomatic",
    "symptomatic",
    "untreated",
    "pretreated",
    "refractory",
    "relapsed",
    "metastatic",
    "cirrhotic",
    "asian",
    "black",
    "white",
    "hispanic",
    "latino",
    "latina",
    "african",
    "european",
    "caucasian",
)

_NON_COHORT_MODIFIER_WORDS = {
    "overall",
    "entire",
    "full",
    "whole",
    "total",
    "general",
    "combined",
    "pooled",
    "unselected",
    "broad",
    "study",
    "trial",
    "such",
    "these",
    "those",
    "both",
    "many",
    "most",
    "some",
    "few",
    "more",
    "less",
    "similar",
    "additional",
    "remaining",
    "subsequent",
    "consecutive",
    "human",
}

_OVERALL_ENDPOINT_FOLLOW = (
    r"(?!\s+(?:survival|mortality|death|response|remission|rate|rates|risk|incidence|"
    r"prevalence|score|scores|change|improvement|benefit|efficacy|safety|adverse|"
    r"toxicity|burden|mean|median|effect|hazard|odds|relative))"
)
OVERALL_SCOPE_RE = re.compile(
    rf"\b(?:"
    rf"overall{_OVERALL_ENDPOINT_FOLLOW}|"
    rf"(?:entire|full|whole|total|general|combined|pooled|primary|unselected|broad)\s+"
    rf"(?:cohort|population|trial|study|sample|patients|participants)|"
    rf"(?:across\s+all|all)\s+(?:randomized\s+|randomised\s+|enrolled\s+|study\s+|trial\s+)?"
    rf"(?:patients|participants|subjects|individuals)|"
    rf"intent(?:ion)?[- ]to[- ]treat|ITT\s+(?:cohort|population|analysis)"
    rf")\b",
    re.IGNORECASE,
)

SUBGROUP_CUE_RE = re.compile(
    r"\b(?:sub-?groups?|sub-?cohorts?|sub-?populations?|strat(?:um|a|ified)|post[- ]?hoc)\b",
    re.IGNORECASE,
)

SUBGROUP_INTRO_RE = re.compile(
    r"^(?:in|for)\s+(?:the\s+)?(?:prespecified\s+|pre-specified\s+|post[- ]?hoc\s+|exploratory\s+)?"
    r"sub-?group\s+analys(?:is|es)\b",
    re.IGNORECASE,
)

SECTION_HEADER_RE = re.compile(
    r"^(?:BACKGROUND|OBJECTIVES?|PURPOSE|METHODS?|PATIENTS?|DESIGN|SETTING|PARTICIPANTS?|"
    r"RESULTS?|FINDINGS?|CONCLUSIONS?|INTERPRETATION|TRIAL\s+REGISTRATION)\s*:\s*",
    re.IGNORECASE,
)

ENROLLMENT_SECTION_RE = re.compile(
    r"^(?:BACKGROUND|OBJECTIVES?|PURPOSE|METHODS?|PATIENTS?|DESIGN|SETTING|PARTICIPANTS?)\s*:",
    re.IGNORECASE,
)

RESULTS_SECTION_RE = re.compile(
    r"^(?:RESULTS?|FINDINGS?|CONCLUSIONS?|INTERPRETATION)\s*:",
    re.IGNORECASE,
)

ENROLLMENT_CUE_RE = re.compile(
    r"\b(?:randomly\s+assigned|random(?:ized|ised)\s+\d|we\s+(?:enrolled|recruited|assigned|included|studied|evaluated)|"
    r"eligible\s+patients|phase\s+[1-4]\b|double[- ]blind|placebo[- ]controlled)\b",
    re.IGNORECASE,
)

_POP_NOUN_RE = (
    r"(?:patients?|participants?|subjects?|individuals?|persons?|people|"
    r"adults?|children|infants?|neonates?|adolescents?|women|men|females?|males?|those)"
)

_COHORT_WITH_RE = re.compile(
    rf"\b(?:(?P<prep>in|among|for|within|of)\s+(?:the\s+)?)?"
    rf"(?:(?:prespecified|pre-specified|post[- ]?hoc|exploratory)\s+)?"
    rf"(?P<sg>sub-?group\s+of\s+)?"
    rf"{_POP_NOUN_RE}\s+(?P<pol>with|without|having|lacking)\s+"
    rf"(?P<tail>[^,;.()\[\]]+)",
    re.IGNORECASE,
)

_COHORT_TAIL_CUT_RE = re.compile(
    r"\s+(?:who\b|were\b|was\b|had\b|have\b|has\b|achieved\b|showed\b|experienced\b|"
    r"received\b|receiving\b|treated\b|assigned\b|randomized\b|randomised\b|enrolled\b|"
    r"to\s+receive\b|versus\b|vs\.?\b|compared\b|"
    r"the\s+(?:primary|secondary|hazard|odds|relative|risk|rate|mortality|incidence|median|mean|HR|OR|RR)\b)",
    re.IGNORECASE,
)

_COHORT_SUBGROUP_MOD_RE = re.compile(
    r"\b(?:(?:in|among|for|within)\s+(?:the\s+)?)?"
    r"([A-Za-z0-9≥≤<>%+-]+(?:\s+[A-Za-z0-9≥≤<>%+-]+){0,3})\s+"
    r"(?:sub-?group|sub-?cohort|sub-?population)\b",
    re.IGNORECASE,
)

_COHORT_STANDALONE_DEMO_RE = re.compile(
    r"\b(?:in|among|for|within)\s+(?:the\s+)?"
    r"(women|females?|men|males?|children|infants?|neonates?|adolescents?|adults?|"
    r"elderly|pediatric|paediatric|outpatients?|inpatients?|smokers?|non-?smokers?)"
    r"(?:\s+(?:patients?|participants?|subjects?|individuals?|cohort|population|group|sub-?group))?\b",
    re.IGNORECASE,
)

_ALL_COHORT_ADJECTIVES = sorted(
    set(NEGATED_COHORT_ADJECTIVES.keys()) | set(_POSITIVE_COHORT_ADJECTIVES),
    key=len,
    reverse=True,
)
_COHORT_ADJ_POP_RE = re.compile(
    r"\b(?:(?:in|among|for|within)\s+(?:the\s+)?)?("
    + "|".join(re.escape(a) for a in _ALL_COHORT_ADJECTIVES)
    + r")\s+(patients?|participants?|subjects?|individuals?|adults?|children|women|men|cohort|population|sub-?group|group)\b",
    re.IGNORECASE,
)

_COHORT_PREP_MOD_POP_RE = re.compile(
    r"\b(?:in|among|for|within)\s+(?:the\s+)?([A-Za-z][A-Za-z-]{2,})\s+"
    r"(patients?|participants?|subjects?|individuals?|adults?|children|women|men)\b",
    re.IGNORECASE,
)

_COHORT_STAGE_CLASS_RE = re.compile(
    r"\b(?:in|among|for|within)\s+(?:the\s+)?(?:(?:patients?|participants?|subjects?|those)\s+(?:with|in)\s+)?"
    r"((?:nyha\s+class|child[- ]pugh\s+class|class|stage|grade)\s+"
    r"[ivx1-4a-c]+(?:\s*(?:/|or|to|-)\s*[ivx1-4a-c]+)*)\b",
    re.IGNORECASE,
)

_STAGE_CLASS_TOKEN_RE = re.compile(
    r"\b(?:(nyha)\s+)?(class|stage|grade)\s+([ivx1-4a-c]+(?:\s*(?:/|or|to|-)\s*[ivx1-4a-c]+)*)\b",
    re.IGNORECASE,
)

_STANDALONE_CONDITION_PHRASES = sorted(
    {
        p
        for grp in (
            "t2dm",
            "t1dm",
            "diabetes",
            "hfref",
            "hfpef",
            "heart_failure",
            "ckd",
            "esrd",
            "afib",
            "hypertension",
            "cad",
            "copd",
            "atopic_dermatitis",
            "ra",
            "ibd",
            "nsclc",
            "hcc",
            "aki",
            "vte",
            "cirrhosis",
        )
        for p in COHORT_SYNONYM_GROUPS[grp]
        if len(p) >= 3
    },
    key=len,
    reverse=True,
)

_COHORT_KNOWN_COND_RE = re.compile(
    r"\b(?:in|among|for|within)\s+(?:the\s+)?("
    + "|".join(re.escape(p) for p in _STANDALONE_CONDITION_PHRASES)
    + r")\b",
    re.IGNORECASE,
)

_AGE_LEFT_BOUND = r"(?<![A-Za-z0-9.])"
_AGE_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    (
        "range",
        re.compile(r"\baged?\s+(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)\s*(?:years?|yrs?)?\b", re.IGNORECASE),
    ),
    (
        ">=",
        re.compile(
            rf"{_AGE_LEFT_BOUND}(?:aged?\s+)?(?:>=|≥)\s*(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\b"
            r"|\b(?:aged?\s+)?at\s+least\s+(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\b"
            r"|\baged?\s*(?:>=|≥)\s*(\d+(?:\.\d+)?)\b"
            r"|\b(?:aged?\s+)?(\d+(?:\.\d+)?)\s*(?:years?|yrs?)?\s*(?:or\s+older|and\s+older)\b",
            re.IGNORECASE,
        ),
    ),
    (
        ">",
        re.compile(
            rf"{_AGE_LEFT_BOUND}(?:aged?\s+)?(?<![<>])>\s*(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\b"
            r"|\b(?:aged?\s+)?(?:older\s+than|over)\s+(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\b"
            r"|\baged?\s*>\s*(\d+(?:\.\d+)?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "<=",
        re.compile(
            rf"{_AGE_LEFT_BOUND}(?:aged?\s+)?(?:<=|≤)\s*(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\b"
            r"|\baged?\s*(?:<=|≤)\s*(\d+(?:\.\d+)?)\b"
            r"|\b(?:aged?\s+)?(\d+(?:\.\d+)?)\s*(?:years?|yrs?)?\s*(?:or\s+younger|and\s+younger)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "<",
        re.compile(
            rf"{_AGE_LEFT_BOUND}(?:aged?\s+)?(?<![<>])<\s*(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\b"
            r"|\b(?:aged?\s+)?(?:younger\s+than|under|below|less\s+than)\s+(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\b"
            r"|\baged?\s*<\s*(\d+(?:\.\d+)?)\b",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True)
class AgeBound:
    op: str  # ">=" | ">" | "<=" | "<" | "range"
    lo: float
    hi: Optional[float] = None

    def to_interval(self) -> Tuple[float, bool, float, bool]:
        if self.op == "range" and self.hi is not None:
            return (self.lo, True, self.hi, True)
        if self.op == ">=":
            return (self.lo, True, float("inf"), False)
        if self.op == ">":
            return (self.lo, False, float("inf"), False)
        if self.op == "<=":
            return (float("-inf"), False, self.lo, True)
        return (float("-inf"), False, self.lo, False)

    def conflicts_with(self, other: "AgeBound") -> bool:
        a_lo, a_lo_inc, a_hi, a_hi_inc = self.to_interval()
        b_lo, b_lo_inc, b_hi, b_hi_inc = other.to_interval()
        if a_hi < b_lo or (a_hi == b_lo and not (a_hi_inc and b_lo_inc)):
            return True
        if b_hi < a_lo or (b_hi == a_lo and not (b_hi_inc and a_lo_inc)):
            return True
        return False

    def matches_demographic_groups(self, groups: set[str]) -> bool:
        lo, _, hi, _ = self.to_interval()
        if lo >= 60 and hi == float("inf") and "elderly" in groups:
            return True
        if hi <= 18 and ("pediatric" in groups or "adolescents" in groups):
            return True
        return False


@dataclass(frozen=True)
class CohortTerms:
    groups: Tuple[str, ...]
    core_terms: Tuple[str, ...]

    def __bool__(self) -> bool:
        return bool(self.groups or self.core_terms)


@dataclass(frozen=True)
class CohortQualifier:
    raw: str
    polarity: str  # "with" | "without"
    terms: CohortTerms
    sub_conditions: Tuple[CohortTerms, ...] = ()
    age_bounds: Optional[AgeBound] = None

    @property
    def groups(self) -> Tuple[str, ...]:
        return self.terms.groups

    @property
    def core_terms(self) -> Tuple[str, ...]:
        return self.terms.core_terms


@dataclass(frozen=True)
class CohortContext:
    text: str
    qualifiers: Tuple[CohortQualifier, ...]


@dataclass(frozen=True)
class RawClauseEntry:
    text: str
    is_enrollment: bool
    has_explicit_sg: bool
    from_fulltext: bool


@dataclass
class SourceClause:
    text: str
    norm_low: str
    norm_no_commas: str
    is_enrollment: bool
    is_subgroup: bool
    qualifiers: Tuple[CohortQualifier, ...]
    from_fulltext: bool = False


@dataclass(frozen=True)
class SubgroupIssue:
    reason: str
    token: str
    source_cohorts: Tuple[str, ...]
    source_clauses: Tuple[str, ...]


def _is_drug_word(word: str) -> bool:
    low = word.lower()
    return len(low) >= 6 and any(low.endswith(stem) for stem in PHARMA_STEMS)


def _singularize_cohort_word(word: str) -> str:
    low = word.lower()
    if low in ("women", "men", "diabetes", "cirrhosis", "metastases", "metastasis"):
        return low
    if len(low) > 4 and low.endswith("s") and not low.endswith("ss"):
        return low[:-1]
    return low


def _groups_overlap_for_polarity(groups_a: Sequence[str], groups_b: Sequence[str]) -> bool:
    for ga in groups_a:
        pa = _COHORT_GROUP_PARENTS.get(ga)
        for gb in groups_b:
            pb = _COHORT_GROUP_PARENTS.get(gb)
            if ga == gb or pa == gb or pb == ga:
                return True
    return False


def _extract_stage_class_tokens(norm_text: str) -> List[str]:
    tokens: List[str] = []
    for m in _STAGE_CLASS_TOKEN_RE.finditer(norm_text):
        prefix = m.group(1).lower() + "_" if m.group(1) else ""
        kind = m.group(2).lower()
        levels = re.findall(r"[ivx1-4a-c]+", m.group(3).lower())
        for lvl in levels:
            tok = f"{prefix}{kind}_{lvl}"
            if tok not in tokens:
                tokens.append(tok)
    return tokens


def _extract_groups_and_core_terms(phrase: str) -> CohortTerms:
    """Map a cohort phrase into canonical synonym groups and remaining core tokens."""
    norm = re.sub(r"[^a-z0-9≥≤<>=%.+/\-]", " ", normalize_text(phrase).lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    groups: List[str] = []
    consumed_spans: List[Tuple[int, int]] = []

    for _, grp, pat in _COHORT_MULTI_PHRASES:
        for m in pat.finditer(norm):
            if any(s <= m.start() and m.end() <= e for s, e in consumed_spans):
                continue
            consumed_spans.append((m.start(), m.end()))
            if grp not in groups:
                groups.append(grp)

    # Extract structured stage/class/grade tokens (e.g. "NYHA class III/IV" -> "nyha_class_iii", "nyha_class_iv")
    core_terms: List[str] = _extract_stage_class_tokens(norm)
    for m in _STAGE_CLASS_TOKEN_RE.finditer(norm):
        consumed_spans.append((m.start(), m.end()))

    # Mask consumed spans before single-word matching
    chars = list(norm)
    for s, e in consumed_spans:
        for idx in range(s, e):
            chars[idx] = " "
    rem = "".join(chars)

    for raw_tok in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", rem):
        low = raw_tok.lower()
        if low in NEGATED_COHORT_ADJECTIVES:
            target = NEGATED_COHORT_ADJECTIVES[low]
            if target in COHORT_SYNONYM_GROUPS:
                if target not in groups:
                    groups.append(target)
            elif target not in core_terms:
                core_terms.append(target)
            continue
        sing = _singularize_cohort_word(low)
        grp_hit = _COHORT_SINGLE_TO_GROUP.get(low) or _COHORT_SINGLE_TO_GROUP.get(sing)
        if grp_hit:
            if grp_hit not in groups:
                groups.append(grp_hit)
            continue
        if (
            low in COHORT_GENERIC_WORDS
            or sing in COHORT_GENERIC_WORDS
            or low in TREATMENT_ARM_WORDS
            or low in NON_COHORT_WITH_WORDS
            or _is_drug_word(low)
        ):
            continue
        if len(low) <= 1 and not low.isdigit():
            continue
        if sing not in core_terms:
            core_terms.append(sing)

    # Include numeric threshold qualifiers like ">=50%" or "<1%" if present (e.g. PD-L1 >=50%)
    for th in re.findall(r"(?:>=|<=|≥|≤|<|>)\s*\d+(?:\.\d+)?%?", norm):
        compact = th.replace(" ", "").replace("≥", ">=").replace("≤", "<=")
        if compact not in core_terms:
            core_terms.append(compact)

    return CohortTerms(tuple(groups), tuple(core_terms))


def _split_sub_conditions(phrase: str) -> List[str]:
    """Split compound 'X and Y' phrases unless the full phrase is a single known multi-word cohort."""
    norm = re.sub(r"[^a-z0-9\s]", " ", normalize_text(phrase).lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    if norm in _COHORT_PHRASE_TO_GROUP:
        return [phrase]
    # Protect known multi-word phrases containing 'and' (e.g. 'heart failure and reduced ejection fraction')
    for mphrase, _, _ in _COHORT_MULTI_PHRASES:
        if " and " in mphrase and mphrase in norm:
            return [phrase]
    parts = [p.strip() for p in re.split(r"\band\b", phrase, flags=re.IGNORECASE) if p.strip()]
    return parts if len(parts) > 1 else [phrase]


def _extract_age_bounds(text: str) -> List[Tuple[str, AgeBound]]:
    out: List[Tuple[str, AgeBound]] = []
    norm = normalize_text(text)
    for op, pat in _AGE_PATTERNS:
        for m in pat.finditer(norm):
            if op == "range":
                lo, hi = float(m.group(1)), float(m.group(2))
                out.append((m.group(0).strip(), AgeBound("range", min(lo, hi), max(lo, hi))))
            else:
                val_str = next((g for g in m.groups() if g is not None), None)
                if val_str is not None:
                    out.append((m.group(0).strip(), AgeBound(op, float(val_str))))
    return out


def _make_cohort_qualifier(
    raw: str,
    polarity: str,
    phrase: str,
    age_bounds: Optional[AgeBound] = None,
) -> Optional[CohortQualifier]:
    terms = _extract_groups_and_core_terms(phrase)
    if not terms and age_bounds is None:
        return None
    sub_conds: List[CohortTerms] = []
    for sub in _split_sub_conditions(phrase):
        st = _extract_groups_and_core_terms(sub)
        if st:
            sub_conds.append(st)
    return CohortQualifier(
        raw=re.sub(r"\s+", " ", raw).strip(" ,.;:"),
        polarity=polarity,
        terms=terms,
        sub_conditions=tuple(sub_conds),
        age_bounds=age_bounds,
    )


def extract_cohort_qualifiers(text: str) -> List[CohortQualifier]:
    """Extract structured patient cohort/subgroup qualifiers from a claim or source clause."""
    norm = SECTION_HEADER_RE.sub("", normalize_text(text)).strip()
    if not norm:
        return []
    qualifiers: List[CohortQualifier] = []
    seen_keys: set[Tuple[str, CohortTerms, Optional[AgeBound]]] = set()

    def _add(q: Optional[CohortQualifier]) -> None:
        if q is None:
            return
        key = (q.polarity, q.terms, q.age_bounds)
        if key not in seen_keys:
            seen_keys.add(key)
            qualifiers.append(q)

    # 1. "patients with/without <condition>" (only when cohort-scoped, not bare outcome counts like "42 patients with ...")
    for m in _COHORT_WITH_RE.finditer(norm):
        prep = (m.group("prep") or "").lower()
        has_sg = bool(m.group("sg"))
        before = norm[: m.start()].rstrip()
        if not prep and not has_sg:
            # Reject outcome-count patterns ("42 patients with serious adverse events") and mid-clause bare noun phrases
            if re.search(r"\d$", before):
                continue
            if before and not re.search(r"(?:^|[,;]\s*|\b(?:whereas|while|and|however))$", before, re.IGNORECASE):
                continue
        elif prep == "of" and not has_sg:
            continue

        pol_word = m.group("pol").lower()
        polarity = "without" if pol_word in ("without", "lacking") else "with"
        tail = _COHORT_TAIL_CUT_RE.split(m.group("tail"), maxsplit=1)[0].strip()
        if not tail:
            continue
        tail_words = [w.lower() for w in re.findall(r"[A-Za-z]+", tail)]
        if tail_words and all(
            w in COHORT_GENERIC_WORDS or w in NON_COHORT_WITH_WORDS or w in TREATMENT_ARM_WORDS or _is_drug_word(w)
            for w in tail_words
        ):
            continue
        raw = f"{m.group(0).split(m.group('tail'), 1)[0]}{tail}".strip()
        raw = re.sub(r"^(?:in|among|for|within|of)\s+(?:the\s+)?", "", raw, flags=re.IGNORECASE)
        _add(_make_cohort_qualifier(raw, polarity, tail))

    # 2. "<modifier> subgroup/subcohort/subpopulation"
    for m in _COHORT_SUBGROUP_MOD_RE.finditer(norm):
        mod = m.group(1).strip()
        mod_tokens = mod.split()
        while mod_tokens and mod_tokens[0].lower() in (
            COHORT_GENERIC_WORDS | TREATMENT_ARM_WORDS | {"in", "among", "for", "within", "the", "a", "an", "of"}
        ):
            mod_tokens.pop(0)
        cleaned_mod = " ".join(mod_tokens).strip()
        if not cleaned_mod:
            continue
        pol = "without" if any(t.lower() in NEGATED_COHORT_ADJECTIVES for t in mod_tokens) else "with"
        _add(_make_cohort_qualifier(f"{cleaned_mod} subgroup", pol, cleaned_mod))

    # 3. "<adj> patients/adults/women/..." (known clinical/demographic adjectives)
    for m in _COHORT_ADJ_POP_RE.finditer(norm):
        adj = m.group(1).lower()
        noun = m.group(2)
        pol = "without" if adj in NEGATED_COHORT_ADJECTIVES else "with"
        phrase = adj if pol == "without" else f"{adj} {noun}"
        _add(_make_cohort_qualifier(f"{adj} {noun}", pol, phrase))

    # 4. General "in/among <modifier> patients/adults/women/..." for unlisted cohorts (e.g. "In Asian patients")
    for m in _COHORT_PREP_MOD_POP_RE.finditer(norm):
        mod = m.group(1)
        mod_low = mod.lower()
        noun = m.group(2)
        if (
            mod_low in COHORT_GENERIC_WORDS
            or mod_low in TREATMENT_ARM_WORDS
            or mod_low in NON_COHORT_WITH_WORDS
            or mod_low in _NON_COHORT_MODIFIER_WORDS
            or mod_low.endswith("-treated")
            or _is_drug_word(mod_low)
        ):
            continue
        pol = "without" if mod_low in NEGATED_COHORT_ADJECTIVES else "with"
        phrase = mod_low if pol == "without" else f"{mod} {noun}"
        _add(_make_cohort_qualifier(f"{mod} {noun}", pol, phrase))

    # 5. Standalone demographic ("in women", "among children", "in elderly")
    for m in _COHORT_STANDALONE_DEMO_RE.finditer(norm):
        demo = m.group(1).lower()
        pol = "without" if demo in NEGATED_COHORT_ADJECTIVES else "with"
        raw = re.sub(r"^(?:in|among|for|within)\s+(?:the\s+)?", "", m.group(0), flags=re.IGNORECASE).strip()
        _add(_make_cohort_qualifier(raw, pol, demo))

    # 6. Clinical class/stage/grade ("In NYHA class III/IV", "In stage IV patients")
    for m in _COHORT_STAGE_CLASS_RE.finditer(norm):
        stage_phrase = m.group(1).strip()
        _add(_make_cohort_qualifier(stage_phrase, "with", stage_phrase))

    # 7. Shorthand "in <known condition>" (e.g. "in HFpEF", "in CKD", "in type 2 diabetes")
    for m in _COHORT_KNOWN_COND_RE.finditer(norm):
        cond = m.group(1).strip()
        q = _make_cohort_qualifier(cond, "with", cond)
        if q is not None and not any(existing.terms == q.terms for existing in qualifiers):
            _add(q)

    # 8. Age thresholds ("patients >=65 years", "patients <65 years", "aged 12 to 17 years")
    for raw_age, bounds in _extract_age_bounds(norm):
        _add(_make_cohort_qualifier(raw_age, "with", "", age_bounds=bounds))

    return qualifiers


def _split_top_level(text: str, sep_re: re.Pattern[str]) -> List[str]:
    """Split text at regex matches occurring outside parentheses/brackets."""
    parts: List[str] = []
    depth = 0
    cut_positions: List[Tuple[int, int]] = []
    matches = list(sep_re.finditer(text))
    if not matches:
        return [text]

    match_idx = 0
    for i, ch in enumerate(text):
        while match_idx < len(matches) and matches[match_idx].start() < i:
            match_idx += 1
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        elif depth == 0 and match_idx < len(matches) and matches[match_idx].start() == i:
            cut_positions.append((matches[match_idx].start(), matches[match_idx].end()))
            match_idx += 1

    if not cut_positions:
        return [text]

    prev = 0
    for start, end in cut_positions:
        seg = text[prev:start].strip()
        if seg:
            parts.append(seg)
        # Keep leading contrastive word (e.g. "whereas") attached to the right clause if matched
        kept = text[start:end].lstrip(" ,;")
        prev = end - len(kept)
    tail = text[prev:].strip()
    if tail:
        parts.append(tail)
    return parts


_CLAUSE_SEP_RE = re.compile(
    r";\s+"
    r"|,\s*(?=whereas\b|while\b)"
    r"|,\s*and\s+(?=(?:in|among|for|within)\s+(?:the\s+)?(?:sub-?group|patients?|participants?|subjects?|those|women|men|adults?|children|adolescents?|elderly|pediatric|paediatric))"
    r"|,\s*compared\s+with\s+(?=(?:in|among|for)\b|\d)"
    r"|,?\s+(?:vs\.?|versus)\s+(?=(?:in|among|for)\s+(?:the\s+)?(?:sub-?group|patients?|participants?|subjects?|those|women|men|adults?|children|adolescents?|elderly|pediatric|paediatric))",
    re.IGNORECASE,
)

_COMPOUND_COHORT_SEPS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:,\s*and\s+|\s+and\s+)", re.IGNORECASE),
    re.compile(r",?\s+(?:vs\.?|versus)\s+", re.IGNORECASE),
    re.compile(r",\s+"),
)


def _split_compound_cohort_segment(seg: str) -> List[str]:
    """Split a multi-cohort segment on top-level conjunctions when every part carries both cohort & number."""
    parts = [seg]
    for sep_re in _COMPOUND_COHORT_SEPS:
        next_parts: List[str] = []
        for piece in parts:
            if len(extract_cohort_qualifiers(piece)) < 2:
                next_parts.append(piece)
                continue
            cand = _split_top_level(piece, sep_re)
            if len(cand) >= 2 and all(extract_cohort_qualifiers(p) and extract_claim_numbers(p) for p in cand):
                next_parts.extend(cand)
            else:
                next_parts.append(piece)
        parts = next_parts
    return parts


def _split_clauses(sentence: str) -> List[str]:
    """Split a sentence into top-level clauses without breaking parenthetical statistics."""
    initial = _split_top_level(sentence, _CLAUSE_SEP_RE)
    out: List[str] = []
    for seg in initial:
        out.extend(_split_compound_cohort_segment(seg))
    return out


def _context_has_negated_concept(text: str, terms: CohortTerms) -> bool:
    norm_low = normalize_text(text).lower()
    for adj, target in NEGATED_COHORT_ADJECTIVES.items():
        if (_groups_overlap_for_polarity((target,), terms.groups) or target in terms.core_terms) and re.search(
            rf"\b{re.escape(adj)}\b", norm_low
        ):
            return True
    for g in terms.groups:
        related_groups = {g}
        parent = _COHORT_GROUP_PARENTS.get(g)
        if parent:
            related_groups.add(parent)
        for child, par in _COHORT_GROUP_PARENTS.items():
            if par == g:
                related_groups.add(child)
        for rg in related_groups:
            for alias in COHORT_SYNONYM_GROUPS.get(rg, ()):
                if re.search(rf"\b(?:without|no|lacking)\s+{re.escape(alias)}\b", norm_low):
                    return True
    for t in terms.core_terms:
        if re.search(rf"\b(?:without|no|lacking)\s+{re.escape(t)}\b", norm_low):
            return True
    return False


def _context_contains_unit(
    ctx_groups: set[str],
    ctx_words: set[str],
    ctx_norm_low: str,
    unit: CohortTerms,
) -> bool:
    """Check whether a single cohort sub-condition's groups & core terms appear in a context."""
    for g in unit.groups:
        if g in ctx_groups:
            continue
        # Allow broader parent concept (e.g. 'diabetes') to match narrower child ('t2dm'/'t1dm')
        if any(_COHORT_GROUP_PARENTS.get(cg) == g for cg in ctx_groups):
            continue
        return False
    for t in unit.core_terms:
        if t.startswith((">=", "<=", ">", "<")):
            compact_ctx = re.sub(r"\s+", "", ctx_norm_low.replace("≥", ">=").replace("≤", "<="))
            if t not in compact_ctx:
                return False
        elif t not in ctx_words and _singularize_cohort_word(t) not in ctx_words:
            return False
    return bool(unit)


def _qualifiers_conflict(claim_q: CohortQualifier, source_qs: Sequence[CohortQualifier]) -> bool:
    """Return True if claim_q directly contradicts source_qs (polarity, opposite cohort, or age)."""
    for sq in source_qs:
        # 1. Age threshold conflict
        if claim_q.age_bounds is not None and sq.age_bounds is not None:
            if claim_q.age_bounds.conflicts_with(sq.age_bounds):
                return True

        # 2. Same or parent/child concept, opposite polarity ("without diabetes" vs "with type 2 diabetes")
        same_concept = _groups_overlap_for_polarity(claim_q.groups, sq.groups) or bool(
            set(claim_q.core_terms) & set(sq.core_terms)
        )
        if same_concept and claim_q.polarity != sq.polarity:
            has_same_pol = any(
                (
                    _groups_overlap_for_polarity(claim_q.groups, other.groups)
                    or bool(set(claim_q.core_terms) & set(other.core_terms))
                )
                and other.polarity == claim_q.polarity
                for other in source_qs
            )
            if not has_same_pol:
                return True

        # 3. Mutually exclusive cohort pair (e.g. women vs men, HFpEF vs HFrEF, T1DM vs T2DM)
        if claim_q.polarity == "with" and sq.polarity == "with":
            for cg in claim_q.groups:
                opposites = _COHORT_OPPOSITES_MAP.get(cg, set())
                if opposites & set(sq.groups):
                    has_claim_grp = any(cg in other.groups and other.polarity == "with" for other in source_qs)
                    if not has_claim_grp:
                        return True
    return False


def _qualifier_supported_by_context(q: CohortQualifier, ctx: CohortContext) -> bool:
    """Return True if cohort qualifier q is supported by ctx without conflict."""
    if _qualifiers_conflict(q, ctx.qualifiers):
        return False

    ctx_terms = _extract_groups_and_core_terms(ctx.text)
    ctx_groups = set(ctx_terms.groups)
    ctx_norm_low = normalize_text(ctx.text).lower()
    ctx_words = {_singularize_cohort_word(w) for w in re.findall(r"[a-z0-9]+", ctx_norm_low)}
    ctx_words.update(re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)+", ctx_norm_low))
    ctx_words.update(ctx_terms.core_terms)

    # Check opposite-group conflict against raw context text too (e.g. Title has "Reduced Ejection Fraction")
    if q.polarity == "with":
        for cg in q.groups:
            opposites = _COHORT_OPPOSITES_MAP.get(cg, set())
            if (opposites & ctx_groups) and cg not in ctx_groups:
                return False

    # Age-bound check
    if q.age_bounds is not None:
        ctx_ages = [b for _, b in _extract_age_bounds(ctx.text)]
        if ctx_ages:
            return not all(q.age_bounds.conflicts_with(cb) for cb in ctx_ages)
        return q.age_bounds.matches_demographic_groups(ctx_groups)

    # Negated qualifier ("without diabetes" / "nondiabetic") must find negation in context
    if q.polarity == "without":
        has_neg_q = any(
            sq.polarity == "without"
            and (_groups_overlap_for_polarity(q.groups, sq.groups) or bool(set(q.core_terms) & set(sq.core_terms)))
            for sq in ctx.qualifiers
        )
        return has_neg_q or _context_has_negated_concept(ctx.text, q.terms)

    # Positive qualifier: every sub-condition must be present in context
    units = q.sub_conditions or (q.terms,)
    for unit in units:
        if not _context_contains_unit(ctx_groups, ctx_words, ctx_norm_low, unit):
            return False
    return True


def _collect_raw_clauses(
    blocks: Sequence[str],
    *,
    from_fulltext: bool,
    track_sections: bool,
) -> List[RawClauseEntry]:
    """Split text blocks into raw clauses while preserving structured section context and per-clause subgroup scope."""
    entries: List[RawClauseEntry] = []
    in_enrollment_section = False

    for block in blocks:
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if track_sections:
                if ENROLLMENT_SECTION_RE.match(stripped):
                    in_enrollment_section = True
                elif RESULTS_SECTION_RE.match(stripped):
                    in_enrollment_section = False

            for sent in split_sentences(stripped):
                if track_sections:
                    if ENROLLMENT_SECTION_RE.match(sent):
                        in_enrollment_section = True
                    elif RESULTS_SECTION_RE.match(sent):
                        in_enrollment_section = False
                sent_is_enroll = in_enrollment_section or bool(ENROLLMENT_CUE_RE.search(sent))
                sub_clauses = _split_clauses(sent)
                # Only inherit subgroup cue across sibling clauses when the sentence starts with "In subgroup analyses..."
                first_clean = SECTION_HEADER_RE.sub("", sub_clauses[0]).strip() if sub_clauses else ""
                inherit_sg = bool(SUBGROUP_INTRO_RE.match(first_clean))
                for clause in sub_clauses:
                    c_has_sg = bool(SUBGROUP_CUE_RE.search(clause)) or (
                        inherit_sg and not OVERALL_SCOPE_RE.search(clause)
                    )
                    entries.append(
                        RawClauseEntry(
                            text=clause,
                            is_enrollment=sent_is_enroll,
                            has_explicit_sg=c_has_sg,
                            from_fulltext=from_fulltext,
                        )
                    )
    return entries


def _build_source_clauses(
    rec: PaperRecord,
    fulltext_paragraphs: Sequence[str] = (),
) -> Tuple[List[SourceClause], CohortContext]:
    """Parse abstract (+ optional OA fulltext) into clauses and detect subgroup scoping."""
    raw_entries: List[RawClauseEntry] = []
    if rec.abstract:
        raw_entries.extend(_collect_raw_clauses([rec.abstract], from_fulltext=False, track_sections=True))
    if fulltext_paragraphs:
        raw_entries.extend(_collect_raw_clauses(fulltext_paragraphs, from_fulltext=True, track_sections=False))

    # Build baseline overall-population context (title + enrollment clauses)
    overall_parts: List[str] = []
    if rec.title:
        overall_parts.append(rec.title)
    for entry in raw_entries:
        if not entry.from_fulltext and entry.is_enrollment and not entry.has_explicit_sg:
            overall_parts.append(entry.text)
    baseline_text = " ".join(overall_parts)
    baseline_ctx = CohortContext(
        text=baseline_text,
        qualifiers=tuple(extract_cohort_qualifiers(baseline_text)) if baseline_text else (),
    )

    total_abstract_clauses = sum(1 for e in raw_entries if not e.from_fulltext)
    clauses: List[SourceClause] = []

    for entry in raw_entries:
        c_quals = tuple(extract_cohort_qualifiers(entry.text))
        is_subgroup = False
        if entry.has_explicit_sg:
            is_subgroup = True
        elif (
            not entry.is_enrollment
            and c_quals
            and (bool(overall_parts) or total_abstract_clauses > 1 or entry.from_fulltext)
        ):
            if not baseline_text:
                other_clauses = [e.text for e in raw_entries if not e.from_fulltext and e.text != entry.text]
                if any(
                    not all(
                        _qualifier_supported_by_context(cq, CohortContext(oc, tuple(extract_cohort_qualifiers(oc))))
                        for cq in c_quals
                    )
                    for oc in other_clauses
                ):
                    is_subgroup = True
            else:
                if any(not _qualifier_supported_by_context(cq, baseline_ctx) for cq in c_quals):
                    is_subgroup = True

        norm_low, norm_no_commas = _normalize_for_quant(entry.text)
        clauses.append(
            SourceClause(
                text=entry.text,
                norm_low=norm_low,
                norm_no_commas=norm_no_commas,
                is_enrollment=entry.is_enrollment,
                is_subgroup=is_subgroup,
                qualifiers=c_quals,
                from_fulltext=entry.from_fulltext,
            )
        )

    # Expand overall context with non-subgroup abstract clauses for general context checks
    main_parts = list(overall_parts)
    for c in clauses:
        if not c.from_fulltext and not c.is_subgroup and c.text not in main_parts:
            main_parts.append(c.text)
    overall_text = " ".join(main_parts)
    overall_ctx = CohortContext(
        text=overall_text,
        qualifiers=tuple(extract_cohort_qualifiers(overall_text)) if overall_text else (),
    )
    return clauses, overall_ctx


def _clause_context(clause: SourceClause, overall_ctx: CohortContext) -> CohortContext:
    return CohortContext(
        text=f"{clause.text} {overall_ctx.text}".strip(),
        qualifiers=clause.qualifiers + overall_ctx.qualifiers,
    )


def check_claim_cohort(
    claim: str,
    rec: PaperRecord,
    matched_numbers: Sequence[str],
    fulltext_paragraphs: Sequence[str] = (),
) -> Optional[Diagnostic]:
    """Verify that matched quantitative tokens belong to the cohort/subgroup stated in the claim."""
    if not matched_numbers or (not rec.abstract and not fulltext_paragraphs):
        return None

    clauses, overall_ctx = _build_source_clauses(rec, fulltext_paragraphs=fulltext_paragraphs)
    if not clauses:
        return None

    norm_claim = normalize_text(claim)
    claim_qualifiers = tuple(extract_cohort_qualifiers(claim))
    claim_ctx = CohortContext(text=claim, qualifiers=claim_qualifiers)
    claim_asserts_overall = bool(OVERALL_SCOPE_RE.search(norm_claim))
    claim_mentions_subgroup = bool(SUBGROUP_CUE_RE.search(norm_claim))

    # Locate source clauses matching each matched quantitative token
    token_clauses: Dict[str, List[SourceClause]] = {}
    for raw_tok in matched_numbers:
        clean_tok = re.sub(r"\s+\[fulltext\]$", "", raw_tok)
        hits = [c for c in clauses if _quant_token_in_norm_text(clean_tok, c.norm_low, c.norm_no_commas)]
        if hits:
            token_clauses[clean_tok] = hits

    if not token_clauses:
        return None

    # Check 1: Tokens that ONLY appear in subgroup-scoped clauses
    subgroup_only_issues: List[SubgroupIssue] = []
    for tok, hits in token_clauses.items():
        if not all(c.is_subgroup for c in hits):
            continue

        any_clause_aligned = False
        has_explicit_conflict = False
        for c in hits:
            if claim_qualifiers and any(_qualifiers_conflict(cq, c.qualifiers) for cq in claim_qualifiers):
                has_explicit_conflict = True
                continue
            if claim_asserts_overall and not claim_mentions_subgroup:
                continue

            if c.qualifiers:
                claim_matches_sg = any(_qualifier_supported_by_context(sq, claim_ctx) for sq in c.qualifiers)
                if claim_qualifiers:
                    # Claim named explicit cohort(s): must match this subgroup's qualifier(s) directly
                    # (never allow generic "subgroup" wording to bypass a wrong subgroup like "HFrEF subgroup")
                    c_ctx = _clause_context(c, overall_ctx)
                    all_claim_q_ok = all(
                        _qualifier_supported_by_context(cq, c_ctx) and not _qualifiers_conflict(cq, c.qualifiers)
                        for cq in claim_qualifiers
                    )
                    if all_claim_q_ok and claim_matches_sg:
                        any_clause_aligned = True
                        break
                elif claim_matches_sg or claim_mentions_subgroup:
                    any_clause_aligned = True
                    break
            elif claim_mentions_subgroup and not claim_asserts_overall:
                any_clause_aligned = True
                break

        if not any_clause_aligned:
            sg_labels = tuple(dict.fromkeys(sq.raw for c in hits for sq in c.qualifiers)) or ("subgroup",)
            sg_texts = tuple(dict.fromkeys(c.text for c in hits))
            if has_explicit_conflict or claim_qualifiers:
                reason = "cohort_conflict"
            elif claim_asserts_overall and not claim_mentions_subgroup:
                reason = "subgroup_attributed_to_overall"
            else:
                reason = "subgroup_qualifier_omitted"
            subgroup_only_issues.append(SubgroupIssue(reason, tok, sg_labels, sg_texts))

    if subgroup_only_issues:
        reason_priority = {
            "cohort_conflict": 0,
            "subgroup_attributed_to_overall": 1,
            "subgroup_qualifier_omitted": 2,
        }
        subgroup_only_issues.sort(key=lambda issue: reason_priority.get(issue.reason, 9))
        top_reason = subgroup_only_issues[0].reason
        bad_tokens = list(dict.fromkeys(issue.token for issue in subgroup_only_issues))
        source_cohorts = list(dict.fromkeys(lbl for issue in subgroup_only_issues for lbl in issue.source_cohorts))
        source_clauses = list(dict.fromkeys(txt for issue in subgroup_only_issues for txt in issue.source_clauses))
        claim_cohort_labels = [q.raw for q in claim_qualifiers] or (
            ["overall"] if claim_asserts_overall else ["unqualified/overall"]
        )

        if top_reason == "cohort_conflict":
            msg = (
                f"Matched number(s) [{', '.join(bad_tokens)}] appear in a different cohort/subgroup "
                f"in the source text ({', '.join(source_cohorts)}) than claimed ({', '.join(claim_cohort_labels)})."
            )
        elif top_reason == "subgroup_attributed_to_overall":
            msg = (
                f"Matched number(s) [{', '.join(bad_tokens)}] only appear in a subgroup "
                f"({', '.join(source_cohorts)}) in the source text, not the claimed overall cohort."
            )
        else:
            msg = (
                f"Matched number(s) [{', '.join(bad_tokens)}] only appear in a subgroup "
                f"({', '.join(source_cohorts)}) in the source text, but the claim omits the subgroup qualifier."
            )

        return Diagnostic(
            code="CLAIM_COHORT_MISMATCH",
            severity="WARNING",
            message=msg,
            expected=f"claim_cohort={claim_cohort_labels}",
            actual=f'source_cohort={source_cohorts}; clause="{_truncate(source_clauses[0], 260)}"',
            details={
                "reason": top_reason,
                "matched_numbers": bad_tokens,
                "claim_cohorts": claim_cohort_labels,
                "source_cohorts": source_cohorts,
                "matched_clauses": source_clauses[:3],
            },
            hint=(
                "A plain numeric match does not prove the claim applies to the stated population — "
                "the matched value(s) are reported for a specific subgroup/cohort in the paper."
            ),
            fix=(
                "Compare the claim's cohort/population against the printed OFFICIAL ABSTRACT below "
                "(or OA full text) and qualify or correct the cohort/numbers before marking SUPPORTED."
            ),
        )

    # Check 2: Claim explicitly names cohort(s) that are unsupported by the source context of the matched numbers.
    # Verify per non-enrollment (results/outcome) token so mixing a subgroup sample size with an overall HR is caught.
    if claim_qualifiers:
        results_tokens = {tok: hits for tok, hits in token_clauses.items() if any(not c.is_enrollment for c in hits)}
        tokens_to_check = results_tokens if results_tokens else token_clauses

        unsupported_cohorts: List[str] = []
        mismatched_tokens: List[str] = []
        mismatched_clauses: List[str] = []

        for tok, hits in tokens_to_check.items():
            tok_unsupported: List[str] = []
            for cq in claim_qualifiers:
                supported = any(
                    not _qualifiers_conflict(cq, c.qualifiers)
                    and _qualifier_supported_by_context(cq, _clause_context(c, overall_ctx))
                    for c in hits
                )
                if not supported:
                    tok_unsupported.append(cq.raw)
            if tok_unsupported:
                mismatched_tokens.append(tok)
                for u in tok_unsupported:
                    if u not in unsupported_cohorts:
                        unsupported_cohorts.append(u)
                for c in hits:
                    if c.text not in mismatched_clauses:
                        mismatched_clauses.append(c.text)

        if unsupported_cohorts:
            sample_clause = mismatched_clauses[0]
            return Diagnostic(
                code="CLAIM_COHORT_MISMATCH",
                severity="WARNING",
                message=(
                    f"Claim attributes matched number(s) [{', '.join(mismatched_tokens)}] to cohort(s) "
                    f"[{', '.join(unsupported_cohorts)}] not supported by the source context for those values."
                ),
                expected=f"claim_cohort={[q.raw for q in claim_qualifiers]}",
                actual=f'unsupported_cohorts={unsupported_cohorts}; clause="{_truncate(sample_clause, 260)}"',
                details={
                    "reason": "cohort_not_in_source_context",
                    "matched_numbers": mismatched_tokens,
                    "claim_cohorts": [q.raw for q in claim_qualifiers],
                    "unsupported_cohorts": unsupported_cohorts,
                    "matched_clauses": mismatched_clauses[:3],
                },
                hint=(
                    "A plain numeric match does not prove the claim applies to the stated population — "
                    "the claimed cohort does not appear in the source context for those numbers."
                ),
                fix=(
                    "Compare the claim's cohort/population against the printed OFFICIAL ABSTRACT below "
                    "(or OA full text) and qualify or correct the cohort/numbers before marking SUPPORTED."
                ),
            )

    return None


def evaluate_claim(
    claim: str,
    rec: PaperRecord,
    http: Optional[HttpClient] = None,
    fetch_fulltext: bool = False,
    prefetched_fulltext: Optional[FulltextResult] = None,
) -> Tuple[ClaimReport, List[Diagnostic]]:
    report = ClaimReport(
        claim=claim,
        extracted_numbers=extract_claim_numbers(claim),
        extracted_keywords=extract_claim_keywords(claim),
        abstract_available=bool(rec.abstract),
    )
    diags: List[Diagnostic] = []
    ft_paragraphs: List[str] = []

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
        abs_low, abs_no_commas = _normalize_for_quant(rec.abstract)
        for tok in report.extracted_numbers:
            if _quant_token_in_norm_text(tok, abs_low, abs_no_commas):
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
                    full_low, full_no_commas = _normalize_for_quant("\n".join(ft_res.paragraphs))
                    still_missing: List[str] = []
                    for tok in report.missing_numbers:
                        if _quant_token_in_norm_text(tok, full_low, full_no_commas):
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


# ---------------------------------------------------------------------------
# Core single-citation verification orchestration
# ---------------------------------------------------------------------------


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
            cr_transient = _is_transient_http(cr_resp)
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
                primary_failed_transient=_is_transient_http(pm_resp),
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
    with HttpClient(timeout=args.timeout) as http:
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

    citations: List[CitationInput] = []
    for idx, entry in enumerate(items, start=1):
        if not isinstance(entry, dict):
            continue
        citations.append(
            CitationInput(
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
        )

    with HttpClient(timeout=args.timeout) as http:
        results = verify_citations_batch(citations, http, max_workers=args.workers)

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
        for cit in citations:
            cit.fulltext = args.fulltext
        with HttpClient(timeout=args.timeout) as http:
            results = verify_citations_batch(citations, http, max_workers=args.workers)

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
        help="Also fetch Open Access JATS full-text XML (NCBI PMC with Europe PMC fallback) if available",
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
        help="Fetch Open Access JATS full-text XML (NCBI PMC with Europe PMC fallback) for all claims",
    )
    p_batch.add_argument("--no-abstract", action="store_true", help="Omit full official abstract from text output")
    p_batch.add_argument("--json", action="store_true", help="Emit machine-readable JSON output")
    p_batch.add_argument("--strict", action="store_true", help="Exit non-zero on WARNING diagnostics too")
    p_batch.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"HTTP timeout in seconds (default {DEFAULT_TIMEOUT})"
    )
    p_batch.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Max concurrent verification workers (default {DEFAULT_WORKERS}; use 1 for sequential)",
    )
    p_batch.set_defaults(func=cmd_batch)

    # scan
    p_scan = sub.add_parser("scan", help="Auto-extract and verify all DOIs and PMIDs from .md/.txt/.docx/.pptx files.")
    p_scan.add_argument("file", help="Path to .md, .txt, .docx, or .pptx file")
    p_scan.add_argument(
        "--fulltext",
        action="store_true",
        help="Check Open Access JATS full-text XML availability (NCBI PMC with Europe PMC fallback)",
    )
    p_scan.add_argument("--no-abstract", action="store_true", help="Omit full official abstract from text output")
    p_scan.add_argument("--json", action="store_true", help="Emit machine-readable JSON output")
    p_scan.add_argument("--strict", action="store_true", help="Exit non-zero on WARNING diagnostics too")
    p_scan.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"HTTP timeout in seconds (default {DEFAULT_TIMEOUT})"
    )
    p_scan.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Max concurrent verification workers (default {DEFAULT_WORKERS}; use 1 for sequential)",
    )
    p_scan.set_defaults(func=cmd_scan)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
