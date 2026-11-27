"""Find the sentences in a scanned document that cite each reference, and the claim text each makes.

Pure. Recognised citation styles:
- numeric markers "[3]", "[1,4-6]" (superscript runs in .docx/.pptx arrive as "[n]" from ``scan``),
  resolved against explicit reference numbers, or entry order when the list has none (Word auto-numbering);
- author-year "(McMurray et al., 2019; Zinman et al., 2015)" and "McMurray et al. (2019)", matched on the
  first author and year parsed from each reference entry;
- an inline DOI, PMID, or doi.org / PubMed link inside the sentence.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from ref_verify.identifiers import strip_unbalanced_closing
from ref_verify.models import CitationInput, CitingSentence
from ref_verify.scan import (
    BIBLIOGRAPHY_HEADING_RE,
    CITATION_NUMBER_LIST,
    SCAN_DOI_RE,
    SCAN_PMID_RE,
    BibliographyEntry,
    bibliography_entries,
    document_lines,
    superscript_is_citation,
)
from ref_verify.text import ends_with_abbreviation, strip_accents

NUMERIC_MARKER_RE = re.compile(rf"\s*\[({CITATION_NUMBER_LIST})\]")
# "(3)" only right before punctuation or the end, so enumerations like "(1) screening" are not citations.
PAREN_MARKER_RE = re.compile(rf"\s*\(({CITATION_NUMBER_LIST})\)(?=\s*(?:[.,;:]|$))")
_TYPED_SUPERSCRIPT_RE = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+(?:[,⁻–][⁰¹²³⁴⁵⁶⁷⁸⁹]+)*")
_SUPERSCRIPT_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")
_ENTRY_NUMBER_RE = re.compile(r"^\s*\[?(\d{1,4})[\].)]")
_NAME = r"[A-Z][\w'’\-]+"
# "McMurray", "McMurray et al.", "McMurray and Solomon": the first author is captured as ``name``.
_AUTHORS = rf"(?P<name>{_NAME})(?:\s+et\s+al\.?|\s+(?:and|&)\s+{_NAME})?"
_AUTHOR_YEAR_PART_RE = re.compile(
    rf"^\s*(?:see\s+|e\.g\.,?\s+|cf\.\s+)?{_AUTHORS},?\s+(?P<year>(?:19|20)\d{{2}})[a-z]?\s*$"
)
_PARENTHETICAL_RE = re.compile(r"\s*\(([^()]{4,200})\)")
_NARRATIVE_RE = re.compile(rf"{_AUTHORS}\s+(?P<paren>\((?P<year>(?:19|20)\d{{2}})[a-z]?\))")
_LOCATOR_RE = re.compile(
    r"\s*(?:https?://\S+|\bdoi\s*:\s*\S+|\b10\.\d{4,9}/[^\s<>\"'{}|\\^\[\]`]+|\bPMID\s*[:#]?\s*\d{5,9}\b)",
    flags=re.IGNORECASE,
)
_SENTENCE_END_RE = re.compile(r"[.!?](?:\s*\[[^\]]{1,40}\])?(?=\s+[A-Z0-9(\[\"“]|\s*$)")
_MAX_RANGE = 50


def split_citing_sentences(text: str) -> list[str]:
    """Split running text into sentences, keeping a trailing "[n]" marker with its sentence."""
    sentences: list[str] = []
    start = 0
    for m in _SENTENCE_END_RE.finditer(text):
        if ends_with_abbreviation(text[: m.start() + 1]):
            continue
        sentence = text[start : m.end()].strip()
        if sentence:
            sentences.append(sentence)
        start = m.end()
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


def _expand_numbers(spec: str) -> list[int]:
    out: list[int] = []
    for part in re.split(r"\s*,\s*", spec):
        bounds = [int(x) for x in re.split(r"\s*[–\-]\s*", part) if x]
        if len(bounds) == 2 and 0 < bounds[1] - bounds[0] <= _MAX_RANGE:
            out.extend(range(bounds[0], bounds[1] + 1))
        else:
            out.extend(bounds)
    return out


def _surname_key(name: str) -> str:
    return strip_accents(name).lower()


class _ReferenceIndex:
    """Maps citation markers in body text to citations (index into the citation list) or unlinked entries."""

    def __init__(self, entries: Sequence[BibliographyEntry], citations: Sequence[CitationInput]) -> None:
        self._by_doi: dict[str, int] = {}
        self._by_pmid: dict[str, int] = {}
        self._by_author_year: dict[tuple[str, str], list[int]] = {}
        self._by_entry: dict[tuple[str, str], int] = {}  # (location, text) of the entry a citation came from
        for i, c in enumerate(citations):
            if not (c.doi or c.pmid):
                continue  # no identity to ground claims against (an unresolved reference)
            if c.location and c.reference_text:
                self._by_entry.setdefault((c.location, c.reference_text), i)
            if c.doi:
                self._by_doi.setdefault(c.doi.lower(), i)
            if c.pmid:
                self._by_pmid.setdefault(c.pmid, i)
            if c.author and c.year:
                key = (_surname_key(c.author.split()[0]), str(c.year))
                self._by_author_year.setdefault(key, []).append(i)

        explicit = [_ENTRY_NUMBER_RE.match(e.text) for e in entries]
        use_explicit = any(explicit)
        self._by_number: dict[int, list[tuple[str, int | None]]] = {}
        for ordinal, (entry, m) in enumerate(zip(entries, explicit), start=1):
            number = int(m.group(1)) if use_explicit and m else (None if use_explicit else ordinal)
            if number is not None:
                self._by_number.setdefault(number, []).append((entry.location, self._entry_citation(entry)))

    def _entry_citation(self, entry: BibliographyEntry) -> int | None:
        """The citation an entry's DOI/PMID (or found identifier) belongs to; a duplicate maps to the same one."""
        for m in SCAN_DOI_RE.finditer(entry.text):
            doi, _ = strip_unbalanced_closing(m.group(1))
            if (idx := self.doi(doi)) is not None:
                return idx
        for m in SCAN_PMID_RE.finditer(entry.text):
            if (idx := self.pmid(m.group(1) or m.group(2))) is not None:
                return idx
        return self._by_entry.get((entry.location, entry.text))  # an identifier found by search

    def number(self, n: int, location: str) -> tuple[bool, int | None]:
        """(is a known reference, its citation index or None when the entry has no DOI/PMID)."""
        candidates = self._by_number.get(n, [])
        if len({idx for _, idx in candidates}) == 1:  # a duplicated entry for the same paper
            return True, candidates[0][1]
        if len(candidates) > 1:  # e.g. every slide restarting at 1: prefer the entry on the same slide
            candidates = [c for c in candidates if c[0] == location]
        if len(candidates) != 1:
            return False, None
        return True, candidates[0][1]

    def author_year(self, name: str, year: str) -> list[int]:
        return self._by_author_year.get((_surname_key(name), year), [])

    def doi(self, doi: str) -> int | None:
        return self._by_doi.get(doi.lower())

    def pmid(self, pmid: str) -> int | None:
        return self._by_pmid.get(pmid)


def _tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([.,;:!?)])", r"\1", text)
    text = re.sub(r"\(\s*\)", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _cited_in(sentence: str, location: str, index: _ReferenceIndex) -> tuple[list[int], int, str]:
    """(citation indices cited, count of cited references without DOI/PMID, claim text without markers)."""
    cited: list[int] = []
    uncheckable = 0
    claim = sentence

    for marker_re in (NUMERIC_MARKER_RE, PAREN_MARKER_RE):
        for m in marker_re.finditer(claim):
            for n in _expand_numbers(m.group(1)):
                known, idx = index.number(n, location)
                if known and idx is None:
                    uncheckable += 1
                elif idx is not None:
                    cited.append(idx)
        claim = marker_re.sub("", claim)

    def _parenthetical(m: re.Match[str]) -> str:
        parts = [_AUTHOR_YEAR_PART_RE.match(p) for p in m.group(1).split(";")]
        if not all(parts):
            return m.group(0)  # "(HR 0.74, 95% CI ...)" and other non-citation brackets stay in the claim
        for p in parts:
            assert p is not None
            cited.extend(index.author_year(p.group("name"), p.group("year")))
        return ""

    claim = _PARENTHETICAL_RE.sub(_parenthetical, claim)
    for m in _NARRATIVE_RE.finditer(claim):
        cited.extend(index.author_year(m.group("name"), m.group("year")))
    claim = _NARRATIVE_RE.sub(lambda m: m.group(0)[: m.start("paren") - m.start()].rstrip(), claim)

    for m in SCAN_DOI_RE.finditer(sentence):
        doi, _ = strip_unbalanced_closing(m.group(1))
        if (idx := index.doi(doi)) is not None:
            cited.append(idx)
    for m in SCAN_PMID_RE.finditer(sentence):
        if (idx := index.pmid(m.group(1) or m.group(2))) is not None:
            cited.append(idx)
    claim = _LOCATOR_RE.sub("", claim)

    return list(dict.fromkeys(cited)), uncheckable, _tidy(claim)


def _typed_superscripts_to_markers(text: str) -> str:
    """ "fraction.¹⁻³" -> "fraction.[1-3]"; exponents such as "kg/m²", "10³", "I²" stay as written."""

    def repl(m: re.Match[str]) -> str:
        digits = m.group(0).translate(_SUPERSCRIPT_DIGITS)
        return f"[{digits}]" if superscript_is_citation(digits, text[: m.start()]) else m.group(0)

    return _TYPED_SUPERSCRIPT_RE.sub(repl, text)


def attach_citing_sentences(segments: Sequence[tuple[str, str]], citations: Sequence[CitationInput]) -> None:
    """Fill ``citation.citing_sentences`` for every citation cited by a body sentence of the document."""
    lines = document_lines(segments)
    index = _ReferenceIndex(bibliography_entries(lines), citations)
    for line in lines:
        if line.is_bibliography_entry or BIBLIOGRAPHY_HEADING_RE.match(line.text):
            continue
        for sentence in split_citing_sentences(_typed_superscripts_to_markers(line.text)):
            cited, uncheckable, claim = _cited_in(sentence, line.location, index)
            if not cited:
                continue
            keys = [citations[i].key for i in cited]
            for i in cited:
                citations[i].citing_sentences.append(
                    CitingSentence(line.location, sentence, claim, keys, uncheckable_refs=uncheckable)
                )
