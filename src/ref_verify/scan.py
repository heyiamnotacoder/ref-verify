"""Document scanning: extracts DOIs, PMIDs, and unlinked bibliography entries from .md/.txt/.docx/.pptx."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path

from ref_verify.identifiers import DOI_CORE_RE, strip_unbalanced_closing
from ref_verify.models import CitationInput, Diagnostic, UnlinkedEntry
from ref_verify.text import truncate

SCAN_DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s<>\"'{}|\\^\[\]`]+)")


SCAN_PMID_RE = re.compile(
    r"(?:\bPMID\s*[:#]?\s*(\d{5,9})\b|pubmed\.ncbi\.nlm\.nih\.gov/(\d{5,9})(?:/|\b))",
    flags=re.IGNORECASE,
)


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
                    texts = [t.text for t in elem.iter() if (t.tag.endswith("}t") or t.tag == "t") and t.text]
                    joined = "".join(texts).strip()
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
                    texts = [t.text for t in elem.iter() if (t.tag.endswith("}t") or t.tag == "t") and t.text]
                    joined = "".join(texts).strip()
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


def extract_citations_from_segments(segments: Sequence[tuple[str, str]]) -> list[CitationInput]:
    """Extract deduplicated DOIs and PMIDs from text segments, pairing co-located DOI+PMID pairs."""
    extracted: list[CitationInput] = []
    seen_dois: set[str] = set()
    seen_pmids: set[str] = set()

    for loc, text in segments:
        dois_found: list[tuple[int, str]] = []
        for m in SCAN_DOI_RE.finditer(text):
            cleaned, _ = strip_unbalanced_closing(m.group(1))
            if cleaned and DOI_CORE_RE.match(cleaned):
                dois_found.append((m.start(), cleaned))

        pmids_found: list[tuple[int, str]] = []
        for m in SCAN_PMID_RE.finditer(text):
            pid = m.group(1) or m.group(2)
            if pid:
                pmids_found.append((m.start(), pid))

        # Pair a DOI and PMID if they appear near each other (within 160 chars) in the same segment
        used_pmid_idxs: set[int] = set()
        for d_pos, doi in dois_found:
            d_key = doi.lower()
            paired_pmid: str | None = None
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


def _segment_part_key(loc: str) -> str:
    if ":" in loc:
        return loc.split(":", 1)[0]
    if loc.endswith(" notes"):
        return "notes"
    return "body"


def extract_unlinked_bibliography_entries(segments: Sequence[tuple[str, str]]) -> list[UnlinkedEntry]:
    """Find bibliography entries under a References/Bibliography heading that contain no DOI or PMID."""
    unlinked: list[UnlinkedEntry] = []
    in_bib = False
    prev_part: str | None = None

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


def build_unlinked_bibliography_diagnostic(entries: Sequence[UnlinkedEntry]) -> Diagnostic | None:
    if not entries:
        return None
    count = len(entries)
    previews = [f"{e.location}: {truncate(e.text, 110)}" for e in entries[:5]]
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
