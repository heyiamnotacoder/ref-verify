"""Record merging and metadata checks: title, author, year, journal, retraction notices."""

from __future__ import annotations

import difflib
import re
from collections.abc import Sequence

from ref_verify.lexicons import SURNAME_PARTICLES
from ref_verify.models import Author, CitationInput, Diagnostic, PaperRecord
from ref_verify.text import (
    journal_alias_group,
    normalize_journal,
    normalize_text,
    parse_year_int,
    title_similarity,
    tokenize_words,
)


def merge_records(records: Sequence[PaperRecord | None]) -> PaperRecord | None:
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


def check_title(cited_title: str, official_title: str) -> Diagnostic | None:
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


def _extract_cited_surname_candidates(cited_author: str) -> list[str]:
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

    cands: list[str] = []
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


def _author_matches(cited_cands: list[str], cited_full_norm: str, author: Author) -> bool:
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


def check_author(cited_author: str, authors: list[Author]) -> Diagnostic | None:
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
    matched_positions: list[tuple[int, str]] = []
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


def check_year(cited_year_raw: str, rec: PaperRecord) -> Diagnostic | None:
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


def check_journal(cited_journal: str, rec: PaperRecord) -> Diagnostic | None:
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


def check_retraction_and_notices(rec: PaperRecord) -> list[Diagnostic]:
    diags: list[Diagnostic] = []
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


def checked_metadata_fields(cit: CitationInput, resolved: PaperRecord) -> frozenset[str]:
    """Cited fields that can actually be compared against ``resolved`` (the rest stay unchecked, never passed)."""
    fields: set[str] = set()
    if cit.title and resolved.title:
        fields.add("title")
    if cit.author:
        fields.add("author")  # an empty official author list still yields a fail-closed WARNING
    cited_year = str(cit.year).strip() if cit.year is not None else ""
    if cited_year and (resolved.all_years() or parse_year_int(cited_year) is None):
        fields.add("year")
    if cit.journal and resolved.journal_variants():
        fields.add("journal")
    return frozenset(fields)


def check_metadata(cit: CitationInput, resolved: PaperRecord) -> list[Diagnostic]:
    """All record-level checks for a resolved paper: notices first, then cited title/author/year/journal."""
    fields = checked_metadata_fields(cit, resolved)
    checks = [
        check_title(cit.title, resolved.title) if "title" in fields and cit.title and resolved.title else None,
        check_author(cit.author, resolved.authors) if "author" in fields and cit.author else None,
        check_year(str(cit.year), resolved) if "year" in fields else None,
        check_journal(cit.journal, resolved) if "journal" in fields and cit.journal else None,
    ]
    return check_retraction_and_notices(resolved) + [d for d in checks if d is not None]
