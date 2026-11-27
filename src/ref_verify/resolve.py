"""Find the DOI/PMID of a citation that cites neither, by searching Crossref and PubMed.

A candidate is accepted only when the evidence names one paper: a near-identical title (not a reply or notice
about it), no contradiction from the cited first author or year, and at least one of them positively confirmed. Anything weaker stays a
"did you mean" suggestion, because verifying the wrong paper would surface as false mismatches.
"""

from __future__ import annotations

import concurrent.futures
import re
from collections.abc import Sequence
from typing import Any

from ref_verify.http_client import HttpClient
from ref_verify.metadata import check_author, check_journal
from ref_verify.models import Author, CitationInput, PaperRecord, SearchResolution
from ref_verify.sources.crossref import search_crossref_by_title
from ref_verify.sources.pubmed import search_pubmed_by_title
from ref_verify.text import normalize_text, parse_year_int, title_similarity

Candidate = dict[str, Any]

MIN_TITLE_SIMILARITY = 0.90
MAX_YEAR_GAP = 1  # print vs online year
SAME_PAPER_TITLE_SIMILARITY = 0.97  # a DOI-less PubMed hit and a Crossref hit are one paper above this
DID_YOU_MEAN_LIMIT = 3
# A letter, reply, or notice *about* a paper often repeats its title (and authors): "<title>. Reply",
# "Correction to: <title>". Crossref may list such a record under the bare title, so every source's title counts.
_NOTICE_TITLE_RE = re.compile(
    r"(?:^|[.:]\s*)(?:in\s+|authors?['’]?\s+)?(?:reply|response|comment(?:ary)?|correction|erratum|corrigendum|"
    r"retraction(?:\s+note)?|retracted|expression\s+of\s+concern|withdrawn)\b",
    flags=re.IGNORECASE,
)


def is_notice_title(title: str) -> bool:
    """True for titles of replies, comments, corrections, retractions, and similar notices about another paper."""
    return bool(_NOTICE_TITLE_RE.search(title.strip()))


def needs_resolution(cit: CitationInput) -> bool:
    """True when a citation supplies no identifier at all (a malformed one is reported, never replaced)."""
    return cit.resolution is None and bool(cit.title) and not (cit.doi or "").strip() and not (cit.pmid or "").strip()


def rank_candidates(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Merge Crossref/PubMed hits for the same DOI, then rank by title similarity, author, citations, PMID."""
    merged_by_key: dict[str, Candidate] = {}
    for c in candidates:
        key = (c.get("doi") or "").lower() or normalize_text(c.get("title") or "").lower()
        existing = merged_by_key.get(key)
        if existing is None:
            existing = merged_by_key[key] = dict(c)
        else:
            _fill_missing(existing, c)
        existing["notice"] = bool(existing.get("notice")) or is_notice_title(str(c.get("title") or ""))

    def _rank_key(x: Candidate) -> tuple[float, int, int, int]:
        sim_bucket = round(float(x.get("title_similarity", 0)), 2)
        has_author = 1 if x.get("first_author") else 0
        cited_by = int(x.get("cited_by_count", 0) or 0)
        has_pmid = 1 if x.get("pmid") else 0
        return (sim_bucket, has_author, cited_by, has_pmid)

    return sorted(merged_by_key.values(), key=_rank_key, reverse=True)


def _fill_missing(into: Candidate, other: Candidate) -> None:
    for field in ("pmid", "doi", "first_author", "year", "journal"):
        into[field] = into.get(field) or other.get(field)
    into["title_similarity"] = max(into.get("title_similarity", 0), other.get("title_similarity", 0))
    into["cited_by_count"] = max(into.get("cited_by_count", 0) or 0, other.get("cited_by_count", 0) or 0)
    into["notice"] = bool(into.get("notice") or other.get("notice"))


def search_candidates(http: HttpClient, title: str, author: str | None = None) -> list[Candidate]:
    """Every Crossref and PubMed title-search hit, merged and ranked (best first)."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_cr = pool.submit(search_crossref_by_title, http, title, author, 8)
        f_pm = pool.submit(search_pubmed_by_title, http, title, 5)
        return rank_candidates(f_cr.result() + f_pm.result())


def _candidate_author(name: str) -> Author:
    """ "McMurray JJV" (Crossref display / PubMed esummary) -> Author(family="McMurray", initials="JJV")."""
    parts = name.split()
    if len(parts) > 1 and parts[-1].isalpha() and parts[-1].isupper() and len(parts[-1]) <= 4:
        return Author(family=" ".join(parts[:-1]), initials=parts[-1])
    return Author(family=name)


def _title_evidence(cited: str, official: str) -> str | None:
    """ "full" for a near-identical title, "main" when the cited title is the official one minus its subtitle."""
    if title_similarity(cited, official) >= MIN_TITLE_SIMILARITY:
        return "full"
    main = official.split(":", 1)[0]
    if main != official and len(cited.split()) >= 3 and title_similarity(cited, main) >= MIN_TITLE_SIMILARITY:
        return "main"
    return None


def _confirmations(cit: CitationInput, cand: Candidate) -> int | None:
    """Score how well the cited author, year, and journal confirm ``cand``; None if anything contradicts it.

    An exact year scores above a one-year (print vs online) offset, so a paper beats next year's letter about it.
    """
    evidence = _title_evidence(cit.title or "", str(cand.get("title") or ""))
    if evidence is None:
        return None
    if cand.get("notice") and not is_notice_title(cit.title or ""):
        return None
    score = 0
    author_ok = year_ok = False
    if cit.author:
        name = cand.get("first_author")
        if not name or check_author(cit.author, [_candidate_author(str(name))]) is not None:
            return None  # a cited author that the candidate cannot confirm is not the same paper
        author_ok = True
        score += 1
    cited_year = parse_year_int(cit.year)
    cand_year = parse_year_int(cand.get("year"))
    if cited_year is not None and cand_year is not None:
        gap = abs(cited_year - cand_year)
        if gap > MAX_YEAR_GAP:
            return None
        year_ok = True
        score += 2 if gap == 0 else 1
    # Title alone is not enough (generic titles recur); a main title without its subtitle needs both fields.
    confirmed = (author_ok and year_ok) if evidence == "main" else (author_ok or year_ok)
    if not confirmed:
        return None
    if cit.journal and cand.get("journal"):
        if check_journal(cit.journal, PaperRecord(journal_full=str(cand["journal"]))) is None:
            score += 1  # journals only break ties: abbreviations vary too much to reject on
    return score


def _same_paper(a: Candidate, b: Candidate) -> bool:
    if a.get("doi") and b.get("doi"):
        return str(a["doi"]).lower() == str(b["doi"]).lower()
    if a.get("pmid") and b.get("pmid"):
        return str(a["pmid"]) == str(b["pmid"])
    return title_similarity(
        str(a.get("title") or ""), str(b.get("title") or "")
    ) >= SAME_PAPER_TITLE_SIMILARITY and parse_year_int(a.get("year")) == parse_year_int(b.get("year"))


def pick_confident_match(cit: CitationInput, candidates: Sequence[Candidate]) -> Candidate | None:
    """The one paper the cited fields identify, or None when no candidate (or more than one) fits."""
    scored = [(score, c) for c in candidates if (score := _confirmations(cit, c)) is not None]
    if not scored:
        return None
    best = max(score for score, _ in scored)
    top = [c for score, c in scored if score == best]
    match = dict(top[0])
    for other in top[1:]:
        if not _same_paper(match, other):
            return None  # two different papers fit equally well
        _fill_missing(match, other)
    return match


def resolve_citation(cit: CitationInput, http: HttpClient) -> None:
    """Search for a citation without identifiers; a confident match fills its DOI/PMID in place."""
    if not needs_resolution(cit):
        return
    assert cit.title is not None
    candidates = search_candidates(http, cit.title, cit.author)
    match = pick_confident_match(cit, candidates)
    cit.resolution = SearchResolution(candidates=candidates[:DID_YOU_MEAN_LIMIT], match=match)
    if match is not None:
        cit.doi = match.get("doi")
        cit.pmid = match.get("pmid")


def resolve_citations(citations: Sequence[CitationInput], http: HttpClient, max_workers: int) -> None:
    """Resolve every citation that needs it, concurrently."""
    pending = [c for c in citations if needs_resolution(c)]
    if not pending:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(pending)))) as pool:
        list(pool.map(lambda c: resolve_citation(c, http), pending))
