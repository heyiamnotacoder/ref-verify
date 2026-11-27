"""Quantitative claim matching: number extraction, unit-aware token search, sentence ranking."""

from __future__ import annotations

import re
from collections.abc import Sequence

from ref_verify.lexicons import PHARMA_STEMS, STOPWORDS
from ref_verify.text import ends_with_abbreviation, normalize_text

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


def extract_claim_numbers(claim: str) -> list[str]:
    """Extract quantitative tokens (numbers, %, HRs, CIs, p-values, sample sizes) cleanly."""
    norm = normalize_text(claim)
    tokens: list[str] = []
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
    covered_spans: list[tuple[int, int]] = []
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


def _numeric_subvalues(token: str) -> list[str]:
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


def normalize_for_quant(text: str) -> tuple[str, str]:
    """Return (lowercase_normalized, comma_stripped_normalized) for quantitative token matching."""
    norm_text = normalize_text(text)
    norm_no_commas = re.sub(r"(?<=\d),(?=\d{3}\b)", "", norm_text)
    return norm_text.lower(), norm_no_commas


def quant_token_in_norm_text(
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
    norm_low, norm_no_commas = normalize_for_quant(text)
    return quant_token_in_norm_text(token, norm_low, norm_no_commas)


def extract_abstract_numbers(text: str) -> list[str]:
    """Extract all notable quantitative expressions from the official abstract for side-by-side comparison."""
    return extract_claim_numbers(text)[:30]


def extract_claim_keywords(claim: str) -> list[str]:
    """Extract drug names, trial acronyms, and high-signal domain terms."""
    norm = normalize_text(claim)
    keywords: list[str] = []
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


_PAREN_LOOKAHEAD = 200
_SENTENCE_GAP_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(])")


def split_sentences(text: str) -> list[str]:
    # Split on newlines or sentence-ending punctuation followed by whitespace and uppercase/digit
    parts: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        start = 0
        for m in _SENTENCE_GAP_RE.finditer(line):
            head = line[start : m.start()]
            # Never end a sentence at "vs." / "et al." or inside a parenthesis that closes soon after
            # ("(3.7%, vs. 5.9%; ...)"); an unclosed "(" must not swallow the rest of the line.
            if ends_with_abbreviation(head) or (
                head.count("(") > head.count(")") and ")" in line[m.end() : m.end() + _PAREN_LOOKAHEAD]
            ):
                continue
            if head.strip():
                parts.append(head.strip())
            start = m.end()
        if line[start:].strip():
            parts.append(line[start:].strip())
    return parts


def find_top_matching_sentences(
    text_blocks: Sequence[str], quant_tokens: Sequence[str], keywords: Sequence[str], max_items: int = 4
) -> list[str]:
    scored: list[tuple[int, int, str]] = []
    kw_lows = [kw.lower() for kw in keywords]
    for idx, sent in enumerate(text_blocks):
        score = 0
        if quant_tokens:
            norm_low, norm_no_commas = normalize_for_quant(sent)
            for qt in quant_tokens:
                if quant_token_in_norm_text(qt, norm_low, norm_no_commas):
                    score += 3
        low = sent.lower()
        for kw_low in kw_lows:
            if kw_low in low:
                score += 1
        if score > 0:
            scored.append((score, -idx, sent))
    scored.sort(reverse=True)
    return [s for _, _, s in scored[:max_items]]
