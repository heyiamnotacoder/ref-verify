"""Document scanning: extracts DOIs, PMIDs, and unlinked bibliography entries from .md/.txt/.docx/.pptx."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ref_verify.identifiers import DOI_CORE_RE, strip_unbalanced_closing
from ref_verify.models import CitationInput, Diagnostic, UnlinkedEntry
from ref_verify.refparse import parse_reference
from ref_verify.text import truncate

SCAN_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s<>\"'{}|\\^\[\]`]+)")


SCAN_PMID_RE = re.compile(
    r"(?:\bPMID\s*[:#]?\s*(\d{5,9})\b|pubmed\.ncbi\.nlm\.nih\.gov/(\d{5,9})(?:/|\b))",
    flags=re.IGNORECASE,
)


# "3", "1,2", "4-6", "1, 3\u20135": the number lists used by Vancouver citation markers.
CITATION_NUMBER_LIST = r"\d{1,3}(?:\s*[,\u2013\-]\s*\d{1,3})*"
# A superscript run is a citation marker ("26%.¹,²") only if it is a short number list and does not
# follow a unit, a number, or a one/two-letter symbol ("kg/m²", "10³", "I²", "R²", "χ²").
SUPERSCRIPT_CITATION_RE = re.compile(rf"\s*{CITATION_NUMBER_LIST}\s*")
_SUPERSCRIPT_UNIT_RE = re.compile(
    r"(?:\b(?:[kcmµμ]?m|[mkµμ]?g|d?[lL]|mol|s)|/[A-Za-z]+|(?<![\d.])10|\^|(?<![^\W\d_])[^\W\d_]{1,2})$"
)


def superscript_is_citation(sup: str, before: str) -> bool:
    """Whether superscript ``sup`` written right after ``before`` is a citation marker, not an exponent."""
    return bool(SUPERSCRIPT_CITATION_RE.fullmatch(sup)) and not _SUPERSCRIPT_UNIT_RE.search(before.rstrip())


def _paragraph_text(p_elem: ET.Element, is_superscript_run: Callable[[ET.Element], bool]) -> str:
    """Join a paragraph's text runs, rendering superscript citation numbers as "[n]" markers."""
    superscript_texts: set[int] = set()
    for run in p_elem.iter():
        if run.tag.endswith("}r") and is_superscript_run(run):
            superscript_texts.update(id(t) for t in run if t.tag.endswith("}t"))

    pieces: list[str] = []
    pending_sup: list[str] = []

    def flush_sup() -> None:
        if not pending_sup:
            return
        sup = "".join(pending_sup)
        pending_sup.clear()
        before = "".join(pieces).rstrip()
        if superscript_is_citation(sup, before):
            pieces.append(f"[{sup.strip()}]")
        else:
            pieces.append(sup)

    for t in p_elem.iter():
        if not ((t.tag.endswith("}t") or t.tag == "t") and t.text):
            continue
        if id(t) in superscript_texts:
            pending_sup.append(t.text)
        else:
            flush_sup()
            pieces.append(t.text)
    flush_sup()
    return "".join(pieces).strip()


def _docx_superscript(run: ET.Element) -> bool:
    return any(el.tag.endswith("}vertAlign") and "superscript" in el.attrib.values() for rpr in run for el in rpr)


def _pptx_superscript(run: ET.Element) -> bool:
    for rpr in run:
        if rpr.tag.endswith("}rPr"):
            try:
                return int(rpr.attrib.get("baseline", "0")) > 0
            except ValueError:
                return False
    return False


def _extract_segments_from_docx(path: Path) -> list[tuple[str, str]]:
    import zipfile

    segments: list[tuple[str, str]] = []
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
                    joined = _paragraph_text(elem, _docx_superscript)
                    if joined:
                        p_idx += 1
                        segments.append((f"{part_label}:p{p_idx}", joined))
    return segments


def _extract_segments_from_pptx(path: Path) -> list[tuple[str, str]]:
    import zipfile

    segments: list[tuple[str, str]] = []
    with zipfile.ZipFile(path, "r") as zf:
        slide_files: list[tuple[int, str, str]] = []
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
            paras: list[str] = []
            for elem in root.iter():
                if elem.tag.endswith("}p") or elem.tag == "p":
                    joined = _paragraph_text(elem, _pptx_superscript)
                    if joined:
                        paras.append(joined)
            if paras:
                segments.append((label, "\n".join(paras)))
    return segments


class ScanError(Exception):
    """A document could not be read (missing, unreadable, or a corrupt .docx/.pptx archive)."""


def extract_text_segments(path: Path) -> list[tuple[str, str]]:
    """Return (location, text) segments; raises ScanError when the file cannot be read."""
    import zipfile  # only .docx/.pptx need it; keep it off the import path of every run

    suffix = path.suffix.lower()
    try:
        if suffix == ".docx":
            return _extract_segments_from_docx(path)
        if suffix == ".pptx":
            return _extract_segments_from_pptx(path)
        raw = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ScanError(str(exc)) from exc
    return [(f"line {i}", line) for i, line in enumerate(raw.splitlines(), start=1) if line.strip()]


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


def _segment_part_key(loc: str) -> str:
    if ":" in loc:
        return loc.split(":", 1)[0]
    if loc.endswith(" notes"):
        return "notes"
    return "body"


@dataclass(frozen=True)
class DocLine:
    """One non-blank line of a segment: its span in the segment text and whether it is a References entry line."""

    segment: int
    location: str
    start: int
    end: int
    text: str  # stripped
    is_bibliography_entry: bool  # under a References/Bibliography heading (the heading itself excluded)
    reference_list: int = -1  # which References section (0, 1, ... in document order); -1 outside one


@dataclass(frozen=True)
class BibliographyEntry:
    """One reference under a References heading; a numbered entry absorbs its unnumbered wrapped lines."""

    location: str  # location of the entry's first line
    text: str  # entry lines joined with single spaces
    lines: tuple[DocLine, ...]


def document_lines(segments: Sequence[tuple[str, str]]) -> list[DocLine]:
    """Every non-blank line in document order, tracking whether it sits under a References/Bibliography heading."""
    lines: list[DocLine] = []
    in_bib = False
    list_index = -1  # each References heading starts a new list (e.g. one per slide)
    prev_part: str | None = None
    for seg_idx, (loc, text) in enumerate(segments):
        part = _segment_part_key(loc)
        if prev_part is not None and part != prev_part:
            in_bib = False
        prev_part = part
        for m in re.finditer(r"[^\r\n]+", text):
            stripped = m.group(0).strip()
            if not stripped:
                continue
            entry = False
            if BIBLIOGRAPHY_HEADING_RE.match(stripped):
                in_bib = True
                list_index += 1
            elif in_bib and _is_bibliography_section_end(stripped):
                in_bib = False
            else:
                entry = in_bib
            lines.append(DocLine(seg_idx, loc, m.start(), m.end(), stripped, entry, list_index if entry else -1))
    return lines


def bibliography_entries(lines: Sequence[DocLine]) -> list[BibliographyEntry]:
    """Group consecutive References lines into entries: an unnumbered line continues a numbered entry."""
    entries: list[BibliographyEntry] = []
    current: list[DocLine] = []
    numbered = False

    def flush() -> None:
        if current:
            entries.append(BibliographyEntry(current[0].location, " ".join(ln.text for ln in current), tuple(current)))
            current.clear()

    for line in lines:
        if not line.is_bibliography_entry:
            flush()
            continue
        starts_entry = bool(NUMBERED_ENTRY_RE.match(line.text))
        if current and numbered and not starts_entry:
            current.append(line)
            continue
        flush()
        current.append(line)
        numbered = starts_entry
    flush()
    return entries


@dataclass(frozen=True)
class _CitationUnit:
    """One DOI and/or PMID cited together, with the reference text (if any) that cites it."""

    order: tuple[int, int]  # (segment, position) of its first identifier, for document order
    doi: str | None
    pmid: str | None
    location: str
    reference: BibliographyEntry | None


def _identifier_mentions(text: str) -> list[tuple[int, str, str]]:
    """(position, "doi" | "pmid", value) for every identifier in ``text``, in reading order."""
    found: list[tuple[int, str, str]] = []
    for m in SCAN_DOI_RE.finditer(text):
        cleaned, _ = strip_unbalanced_closing(m.group(1))
        if cleaned and DOI_CORE_RE.match(cleaned):
            found.append((m.start(), "doi", cleaned))
    for m in SCAN_PMID_RE.finditer(text):
        pid = m.group(1) or m.group(2)
        if pid:
            found.append((m.start(), "pmid", pid))
    return sorted(found)


def _entry_units(entry: BibliographyEntry, order: tuple[int, int]) -> list[_CitationUnit]:
    """A reference entry cites one paper: its first DOI and first PMID pair regardless of distance."""
    mentions = _identifier_mentions(entry.text)
    dois = list(dict.fromkeys(v for _, kind, v in mentions if kind == "doi"))
    pmids = list(dict.fromkeys(v for _, kind, v in mentions if kind == "pmid"))
    if not dois and not pmids:
        return []
    pairs: list[tuple[str | None, str | None]] = [(dois[0] if dois else None, pmids[0] if pmids else None)]
    pairs += [(d, None) for d in dois[1:]] + [(None, p) for p in pmids[1:]]
    return [_CitationUnit(order, d, p, entry.location, entry) for d, p in pairs]


def _body_units(seg_idx: int, loc: str, mentions: Sequence[tuple[int, str, str]]) -> list[_CitationUnit]:
    """Body text: pair a DOI with a PMID appearing within 160 characters of it."""
    dois = [(pos, v) for pos, kind, v in mentions if kind == "doi"]
    pmids = [(pos, v) for pos, kind, v in mentions if kind == "pmid"]
    units: list[_CitationUnit] = []
    used: set[int] = set()
    for d_pos, doi in dois:
        paired: str | None = None
        for idx, (p_pos, pmid) in enumerate(pmids):
            if idx not in used and abs(p_pos - d_pos) <= 160:
                paired = pmid
                used.add(idx)
                break
        units.append(_CitationUnit((seg_idx, d_pos), doi, paired, loc, None))
    units += [
        _CitationUnit((seg_idx, p_pos), None, pmid, loc, None) for i, (p_pos, pmid) in enumerate(pmids) if i not in used
    ]
    return units


def _citation_units(segments: Sequence[tuple[str, str]]) -> list[_CitationUnit]:
    lines = document_lines(segments)
    entry_of = {line: entry for entry in bibliography_entries(lines) for line in entry.lines}
    lines_by_segment: dict[int, list[DocLine]] = {}
    for line in lines:
        lines_by_segment.setdefault(line.segment, []).append(line)

    units: list[_CitationUnit] = []
    seen_entries: set[int] = set()
    for seg_idx, (loc, text) in enumerate(segments):
        body: list[tuple[int, str, str]] = []
        for mention in _identifier_mentions(text):
            pos = mention[0]
            line = next((ln for ln in lines_by_segment.get(seg_idx, []) if ln.start <= pos < ln.end), None)
            entry = entry_of.get(line) if line is not None else None
            if entry is None and line is not None and NUMBERED_ENTRY_RE.match(line.text):
                entry = BibliographyEntry(line.location, line.text, (line,))  # numbered list outside References
                entry_of[line] = entry
            if entry is None:
                body.append(mention)
            elif id(entry) not in seen_entries:
                seen_entries.add(id(entry))
                units += _entry_units(entry, (seg_idx, pos))
        units += _body_units(seg_idx, loc, body)
    return sorted(units, key=lambda u: u.order)


def _citation_label(loc: str, doi: str | None, pmid: str | None) -> str:
    if doi:
        return f"{loc} ({doi}" + (f", PMID:{pmid})" if pmid else ")")
    return f"{loc} (PMID:{pmid})"


def _attach_reference(cit: CitationInput, entry: BibliographyEntry | None) -> None:
    """Fill cited title/author/year/journal from the first reference entry citing this paper.

    Body-text mentions stay identifier-only; when the reference entry comes later, the citation is
    re-pointed at that entry so "Where" shows the reference the author actually wrote.
    """
    if cit.reference_text is not None or entry is None:
        return
    ref = parse_reference(entry.text)
    cit.title, cit.author, cit.year, cit.journal = ref.title, ref.author, ref.year, ref.journal
    cit.reference_text = entry.text
    cit.location = entry.location


def _record_reference_entry(
    cit: CitationInput, entry: BibliographyEntry | None, first_entry: dict[int, BibliographyEntry]
) -> None:
    """Remember a citation's first References entry; another entry for it *in the same list* is a duplicate.

    Repeating a reference in a different list (each slide's own References) is not a duplicate.
    """
    if entry is None or not entry.lines[0].is_bibliography_entry:  # body text / numbered lists don't count
        return
    seen = first_entry.setdefault(id(cit), entry)
    if seen is not entry and seen.lines[0].reference_list == entry.lines[0].reference_list:
        cit.duplicate_locations.append(entry.location)


def extract_citations_from_segments(segments: Sequence[tuple[str, str]]) -> list[CitationInput]:
    """One citation per cited paper: DOIs/PMIDs merged across mentions, cited fields from its reference entry.

    A later mention merges into an earlier citation of the same DOI or PMID unless it pairs that identifier
    with a *different* partner ID (kept separate so PMID_DOI_CONFLICT stays visible). A second References
    entry for the same paper is recorded in ``duplicate_locations``.
    """
    extracted: list[CitationInput] = []
    by_doi: dict[str, CitationInput] = {}
    by_pmid: dict[str, CitationInput] = {}
    first_entry: dict[int, BibliographyEntry] = {}  # id(citation) -> its first References entry

    for unit in _citation_units(segments):
        doi_key = unit.doi.lower() if unit.doi else None
        cit = (by_doi.get(doi_key) if doi_key else None) or (by_pmid.get(unit.pmid) if unit.pmid else None)
        conflicting = cit is not None and (
            (unit.pmid is not None and cit.pmid is not None and cit.pmid != unit.pmid)
            or (doi_key is not None and cit.doi is not None and cit.doi.lower() != doi_key)
        )
        if cit is None or conflicting:
            cit = CitationInput(doi=unit.doi, pmid=unit.pmid, location=unit.location)
            extracted.append(cit)
        cit.doi = cit.doi or unit.doi
        cit.pmid = cit.pmid or unit.pmid
        if doi_key:
            by_doi.setdefault(doi_key, cit)
        if unit.pmid:
            by_pmid.setdefault(unit.pmid, cit)
        _record_reference_entry(cit, unit.reference, first_entry)
        _attach_reference(cit, unit.reference)
        cit.label = _citation_label(cit.location or unit.location, cit.doi, cit.pmid)

    return extracted


def unlinked_entry_diagnostic(entry: UnlinkedEntry) -> Diagnostic:
    """Per-entry form of UNLINKED_BIBLIOGRAPHY_ENTRIES, used for one checklist row."""
    return Diagnostic(
        code="UNLINKED_BIBLIOGRAPHY_ENTRIES",
        severity="WARNING",
        message="Bibliography entry has no DOI or PMID and no title that could be searched, so it was not verified.",
        expected="A DOI or PMID in this reference entry, or a Vancouver/APA entry with a parseable title",
        actual=f"{entry.location}: {truncate(entry.text, 160)}",
        details={"location": entry.location},
        hint="Only entries whose author/title/journal split is unambiguous are searched by title.",
        fix="Add the DOI or PMID to this entry, or verify it with `ref-verify batch` using `title`/`author`/`year`.",
    )


def extract_unlinked_bibliography_entries(segments: Sequence[tuple[str, str]]) -> list[UnlinkedEntry]:
    """Find bibliography entries under a References/Bibliography heading that contain no DOI or PMID."""
    unlinked: list[UnlinkedEntry] = []
    for entry in bibliography_entries(document_lines(segments)):
        text = entry.text
        if SCAN_DOI_RE.search(text) or SCAN_PMID_RE.search(text):
            continue
        looks_like_entry = (NUMBERED_ENTRY_RE.match(text) and len(text) >= 15) or (
            YEAR_OR_URL_RE.search(text) and len(text) >= 20
        )
        if looks_like_entry:
            unlinked.append(UnlinkedEntry(location=entry.location, text=text))
    return unlinked


def split_searchable_entries(entries: Sequence[UnlinkedEntry]) -> tuple[list[CitationInput], list[UnlinkedEntry]]:
    """(citations to find by search, entries left unverified): an entry is searchable once its title parses."""
    searchable: list[CitationInput] = []
    unsearchable: list[UnlinkedEntry] = []
    for entry in entries:
        ref = parse_reference(entry.text)
        if not ref.title:
            unsearchable.append(entry)
            continue
        searchable.append(
            CitationInput(
                title=ref.title,
                author=ref.author,
                year=ref.year,
                journal=ref.journal,
                label=entry.location,
                location=entry.location,
                reference_text=entry.text,
            )
        )
    return searchable, unsearchable


def merge_in_document_order(
    segments: Sequence[tuple[str, str]], cited: Sequence[CitationInput], searched: Sequence[CitationInput]
) -> list[CitationInput]:
    """Slot each searched reference into ``cited`` at its place in the reference list.

    ``cited`` keeps its own order; a searched entry goes before the first cited reference entry that follows it.
    Citations not tied to a reference entry (identifiers in body text) are never compared against.
    """
    position = {
        (entry.location, entry.text): (entry.lines[0].segment, entry.lines[0].start)
        for entry in bibliography_entries(document_lines(segments))
    }

    def pos(cit: CitationInput) -> tuple[int, int] | None:
        return position.get((cit.location or "", cit.reference_text or ""))

    pending = sorted(searched, key=lambda c: pos(c) or (len(segments), 0))
    merged: list[CitationInput] = []
    for cit in cited:
        here = pos(cit)
        while pending and here is not None and (pos(pending[0]) or here) < here:
            merged.append(pending.pop(0))
        merged.append(cit)
    return merged + pending


def build_unlinked_bibliography_diagnostic(entries: Sequence[UnlinkedEntry]) -> Diagnostic | None:
    if not entries:
        return None
    count = len(entries)
    previews = [f"{e.location}: {truncate(e.text, 110)}" for e in entries[:5]]
    return Diagnostic(
        code="UNLINKED_BIBLIOGRAPHY_ENTRIES",
        severity="WARNING",
        message=(
            f"Found {count} bibliography entr{'y' if count == 1 else 'ies'} without a DOI, PMID, or searchable "
            f"title; `ref-verify scan` could not verify {'it' if count == 1 else 'them'} automatically."
        ),
        expected="Every journal reference includes a DOI or PMID (or is verified via `ref-verify batch`)",
        actual=f"{count} unlinked bibliography entries (first {len(previews)}: {previews})",
        details={"count": count, "locations": [e.location for e in entries]},
        hint="Entries without identifiers are searched by title only when their author/title split is unambiguous.",
        fix="Add DOIs/PMIDs to bibliography entries or export them to JSON and run `ref-verify batch <refs.json>`.",
    )
