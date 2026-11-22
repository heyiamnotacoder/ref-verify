"""DOI and PMID syntax validation with pinpointing diagnostics."""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from ref_verify.models import Diagnostic

DOI_CORE_RE = re.compile(r"^10\.\d{4,9}/\S+$")


TRAILING_PUNCT_CHARS = ".,;:'\"<>?!"


def strip_unbalanced_closing(s: str) -> Tuple[str, str]:
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
    stripped, trailing = strip_unbalanced_closing(candidate)
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
