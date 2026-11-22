"""Text and JSON rendering of verification results."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List, Optional, Sequence

from ref_verify.models import VerificationResult
from ref_verify.text import truncate


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
                lines.append(f"    > {truncate(ex, 450)}")

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
