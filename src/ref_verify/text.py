"""Pure text helpers: tags, accents, tokens, journal names, years, title similarity, truncation."""

from __future__ import annotations

import difflib
import html
import re
import unicodedata
from typing import Any

from ref_verify.lexicons import JOURNAL_ALIASES


def strip_tags(text: str | None) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"<[^>]+>", " ", text)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def strip_accents(text: str) -> str:
    if text.isascii():  # NFKD leaves ASCII unchanged; skip the per-character scan
        return text
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


def tokenize_words(text: str) -> list[str]:
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


_JOURNAL_ALIAS_LOOKUP: dict[str, str] = {
    normalize_journal(v): key for key, variants in JOURNAL_ALIASES.items() for v in variants
}


def journal_alias_group(name: str) -> str | None:
    return _JOURNAL_ALIAS_LOOKUP.get(normalize_journal(name))


def parse_year_int(val: Any) -> int | None:
    if val is None:
        return None
    m = re.search(r"\b(1[5-9]\d{2}|20\d{2}|21\d{2})\b", str(val))
    return int(m.group(1)) if m else None


def title_similarity(cited: str, official: str) -> float:
    a = " ".join(tokenize_words(cited))
    b = " ".join(tokenize_words(official))
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."
