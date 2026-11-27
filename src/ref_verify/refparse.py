"""Conservative parsing of one bibliography entry (Vancouver/NLM or APA) into cited title/author/year/journal.

Every field is optional: a field is returned only when the entry's shape makes it unambiguous, because a
mis-parsed field would surface as a false TITLE/AUTHOR/YEAR/JOURNAL mismatch. When in doubt, return None and
let the checklist show the check as "not checked".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ref_verify.text import normalize_text


@dataclass(frozen=True)
class ParsedReference:
    author: str | None = None  # first author as "Surname INITIALS" (or a group name)
    year: str | None = None
    title: str | None = None
    journal: str | None = None


_NUMBERING_RE = re.compile(r"^\s*(?:\[\d+\]|\(\d+\)|\d+[.)])\s*")
# Everything from the first identifier/URL onward is a locator, not bibliographic text.
_LOCATOR_RE = re.compile(
    r"(?:\bdoi\s*:|\bhttps?://|\bwww\.|\bPMID\b|\bPMCID\b|\bPMC\d|\b10\.\d{4,9}/)", flags=re.IGNORECASE
)
_YEAR = r"(?:19|20)\d{2}"
# Lowercase surname particles: "van Geel MJ", "de Bruin, A. B.", "von Kobyletzki L".
_PARTICLES = r"(?:(?:van|von|de|der|den|di|da|du|del|della|le|la|dos|das|ten|ter)\s+)*"
_CAP = "[A-ZÀ-ÖØ-Þ]"  # an uppercase letter, including accented and "Ø"
_SURNAME = rf"{_PARTICLES}{_CAP}[\w'’\-]+(?:\s{_PARTICLES}{_CAP}[\w'’\-]+){{0,2}}"
# Real month names only ("Jun", "June", "Sept.", seasons), optionally a range and a day or day range.
_MONTH_NAME = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sept?(?:ember)?|"
    r"Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?|Spring|Summer|Autumn|Fall|Winter)\.?"
)
_DATE_TAIL = rf"(?:\s+{_MONTH_NAME}(?:-{_MONTH_NAME})?(?:\s+\d{{1,2}}(?:-\d{{1,2}})?)?)?"
# A one-word place qualifier on a journal abbreviation: "Dermatol Ther (Heidelb)", "Br J Hosp Med (Lond)".
_JOURNAL_QUALIFIER = r"(?:\s\([A-Z][a-z]{1,12}\))?"

# Vancouver/NLM: "Authors. Title. Journal. 2019 Nov 21;381(21):1995-2008."  (Lancet style drops the period
# after the journal.) Neither the author block nor the title may contain a period, so a split is never guessed.
_VANCOUVER_RE = re.compile(
    rf"^(?P<authors>[^.]+?)\.\s+(?P<title>[^.?!]+[.?!])\s+"
    rf"(?P<journal>[A-Z][^.;:()]*?{_JOURNAL_QUALIFIER})\.?\s+"
    # "2019", "2019 Nov", "2020 June 9", "2017 Jun-Jul", "2018 Sep 1-15"
    rf"(?P<year>{_YEAR}){_DATE_TAIL}\s*(?:[;:]|$)"
)
# APA: "Surname, A. A., & Surname, B. (2019). Title. Journal, 381(21), 1995-2008."
_APA_HEAD_RE = re.compile(
    rf"^(?P<authors>{_PARTICLES}[A-Z][^()]*?)\s*\((?P<year>{_YEAR})[a-z]?(?:,[^)]*)?\)\.?\s*(?P<rest>.*)$"
)
_APA_REST_RE = re.compile(r"^(?P<title>[^.?!]+[.?!])\s+(?P<journal>[A-Z][^,.]*?),\s*\d")

_MD_EMPHASIS_RE = re.compile(r"(\*\*|\*|__)(?=\S)(.+?)(?<=\S)\1")
# Vancouver medium/language tags ("[Internet]", "[Article in Chinese]") and bracketed translated titles.
_BRACKET_TAG_RE = re.compile(r"\s*\[[^\]]*\]")
_ET_AL_RE = re.compile(r"\s*,?\s*\bet\s+al\b\.?$", flags=re.IGNORECASE)
# Vancouver first authors always carry initials ("McMurray JJV"); a short title sentence never does.
_VANCOUVER_NAME_RE = re.compile(rf"^{_SURNAME}\s{_CAP}{{1,4}}$")
_APA_SURNAME_RE = re.compile(rf"^{_SURNAME}$")
_MIN_TITLE_WORDS = 3


def _bibliographic_text(text: str) -> str:
    body = _MD_EMPHASIS_RE.sub(r"\2", _NUMBERING_RE.sub("", normalize_text(text), count=1))
    m = _LOCATOR_RE.search(body)
    if m:
        body = body[: m.start()]
    return body.strip(" .,;")


def _clean_title(raw: str) -> str | None:
    title = _BRACKET_TAG_RE.sub("", raw).strip().rstrip(".").strip()
    return title if len(title.split()) >= _MIN_TITLE_WORDS else None


def _clean_journal(raw: str) -> str | None:
    journal = _BRACKET_TAG_RE.sub("", raw).strip().rstrip(".").strip()
    if not (2 <= len(journal) <= 80) or journal.isdigit():
        return None
    return journal


def _vancouver_first_author(authors: str) -> str | None:
    first = _ET_AL_RE.sub("", authors.split(",")[0]).strip()
    return first if _VANCOUVER_NAME_RE.match(first) else None


def _apa_first_author(authors: str) -> str | None:
    parts = [p.strip() for p in authors.split(",")]
    surname = _ET_AL_RE.sub("", parts[0]).strip()
    if not _APA_SURNAME_RE.match(surname):
        return None
    initials = ""
    if len(parts) > 1 and re.fullmatch(r"(?:[A-Z]\.\s*-?\s*)+", parts[1]):
        initials = "".join(re.findall(r"[A-Z]", parts[1]))
    return f"{surname} {initials}".strip()


def parse_reference(text: str) -> ParsedReference:
    body = _bibliographic_text(text)
    if not body:
        return ParsedReference()

    apa = _APA_HEAD_RE.match(body)
    if apa:
        rest = _APA_REST_RE.match(apa.group("rest"))
        return ParsedReference(
            author=_apa_first_author(apa.group("authors")),
            year=apa.group("year"),
            title=_clean_title(rest.group("title")) if rest else None,
            journal=_clean_journal(rest.group("journal")) if rest else None,
        )

    van = _VANCOUVER_RE.match(body)
    if van:
        author = _vancouver_first_author(van.group("authors"))
        return ParsedReference(
            author=author,
            year=van.group("year"),
            # Without a recognisable author block (consensus reports, group authors) the first period may sit
            # inside the title, so "authors. title." cannot be told apart: keep only journal and year.
            title=_clean_title(van.group("title")) if author else None,
            journal=_clean_journal(van.group("journal")),
        )
    return ParsedReference()
