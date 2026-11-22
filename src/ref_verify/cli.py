"""Command-line interface: argument parsing and the check, batch, and scan commands."""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ref_verify import __version__
from ref_verify.http_client import DEFAULT_TIMEOUT, HttpClient
from ref_verify.models import CitationInput, Diagnostic, VerificationResult
from ref_verify.report import format_result_text, format_summary_text, result_to_dict
from ref_verify.scan import (
    build_unlinked_bibliography_diagnostic,
    extract_citations_from_segments,
    extract_text_segments,
    extract_unlinked_bibliography_entries,
    format_unlinked_bibliography_text,
)
from ref_verify.verify import DEFAULT_WORKERS, verify_citation, verify_citations_batch


def cmd_check(args: argparse.Namespace) -> int:
    cit = CitationInput(
        doi=args.doi,
        pmid=args.pmid,
        title=args.title,
        author=args.author,
        year=args.year,
        journal=args.journal,
        claim=args.claim,
        fulltext=args.fulltext,
    )
    with HttpClient(timeout=args.timeout) as http:
        res = verify_citation(cit, http)

    if args.json:
        print(json.dumps(result_to_dict(res), indent=2, ensure_ascii=False))
    else:
        print(format_result_text(res, show_abstract=not args.no_abstract))

    if res.status == "FAIL":
        return 1
    if args.strict and res.status == "WARN":
        return 1
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    try:
        if args.input == "-":
            raw = sys.stdin.read()
        else:
            raw = Path(args.input).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: Could not read batch JSON '{args.input}': {exc}", file=sys.stderr)
        return 2

    if isinstance(payload, dict):
        items = payload.get("references") or payload.get("citations") or payload.get("items")
    else:
        items = payload

    if not isinstance(items, list):
        print(
            "ERROR: Batch input must be a JSON array (or object with 'references'/'citations' array).", file=sys.stderr
        )
        return 2

    citations: List[CitationInput] = []
    for idx, entry in enumerate(items, start=1):
        if not isinstance(entry, dict):
            continue
        citations.append(
            CitationInput(
                doi=entry.get("doi"),
                pmid=str(entry["pmid"]) if entry.get("pmid") is not None else None,
                title=entry.get("title"),
                author=entry.get("author"),
                year=str(entry["year"]) if entry.get("year") is not None else None,
                journal=entry.get("journal"),
                claim=entry.get("claim"),
                label=entry.get("id") or entry.get("label") or f"ref-{idx}",
                fulltext=bool(entry.get("fulltext") or args.fulltext),
            )
        )

    with HttpClient(timeout=args.timeout) as http:
        results = verify_citations_batch(citations, http, max_workers=args.workers)

    if args.json:
        out = {
            "summary": {
                "total": len(results),
                "pass": sum(1 for r in results if r.status == "PASS"),
                "warn": sum(1 for r in results if r.status == "WARN"),
                "fail": sum(1 for r in results if r.status == "FAIL"),
            },
            "results": [result_to_dict(r) for r in results],
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        for i, res in enumerate(results, start=1):
            print(format_result_text(res, index=i, show_abstract=not args.no_abstract))
        print(format_summary_text(results, header="BATCH SUMMARY"))

    if any(r.status == "FAIL" for r in results):
        return 1
    if args.strict and any(r.status == "WARN" for r in results):
        return 1
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"ERROR: File not found: {path}", file=sys.stderr)
        return 2

    try:
        segments = extract_text_segments(path)
    except (OSError, zipfile.BadZipFile) as exc:
        print(f"ERROR: Could not read '{path}': {exc}", file=sys.stderr)
        return 2

    citations = extract_citations_from_segments(segments)
    unlinked_entries = extract_unlinked_bibliography_entries(segments)
    scan_diag = build_unlinked_bibliography_diagnostic(unlinked_entries)
    scan_diagnostics: List[Diagnostic] = [scan_diag] if scan_diag else []

    results: List[VerificationResult] = []
    if citations:
        for cit in citations:
            cit.fulltext = args.fulltext
        with HttpClient(timeout=args.timeout) as http:
            results = verify_citations_batch(citations, http, max_workers=args.workers)

    if args.json:
        out: Dict[str, Any] = {
            "file": str(path),
            "extracted_count": len(citations),
            "unlinked_bibliography_count": len(unlinked_entries),
            "unlinked_bibliography_entries": [asdict(e) for e in unlinked_entries],
            "scan_diagnostics": [asdict(d) for d in scan_diagnostics],
        }
        if citations:
            out["summary"] = {
                "total": len(results),
                "pass": sum(1 for r in results if r.status == "PASS"),
                "warn": sum(1 for r in results if r.status == "WARN"),
                "fail": sum(1 for r in results if r.status == "FAIL"),
            }
        out["results"] = [result_to_dict(r) for r in results]
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        if not citations:
            print(f"SCAN: No DOIs or PMIDs found in '{path}'.")
        else:
            print(f"SCAN FILE: {path}  |  EXTRACTED REFERENCES: {len(citations)}")
            for i, res in enumerate(results, start=1):
                print(format_result_text(res, index=i, show_abstract=not args.no_abstract))
        if scan_diag:
            print(format_unlinked_bibliography_text(scan_diag, unlinked_entries))
        if citations:
            print(format_summary_text(results, header="SCAN SUMMARY"))

    if any(r.status == "FAIL" for r in results):
        return 1
    if args.strict and (any(r.status == "WARN" for r in results) or scan_diagnostics):
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ref-verify",
        description=(
            "Verify scientific references (DOI/PMID), paper metadata, retraction status, "
            "and quantitative claims against Crossref, PubMed, and Europe PMC with pinpointing diagnostics."
        ),
    )
    parser.add_argument("--version", action="version", version=f"ref-verify {__version__}")

    sub = parser.add_subparsers(dest="command", required=True)

    # check
    p_check = sub.add_parser("check", help="Verify a single citation and optional quantitative claim.")
    p_check.add_argument("--doi", help="DOI (e.g. 10.1056/NEJMoa1911303)")
    p_check.add_argument("--pmid", help="PubMed ID (e.g. 31535829)")
    p_check.add_argument("--title", help="Cited paper title to cross-check (and search if DOI/PMID not found)")
    p_check.add_argument("--author", help="Cited first author surname or name (e.g. 'McMurray' or 'McMurray JJV')")
    p_check.add_argument("--year", help="Cited publication year (e.g. 2019)")
    p_check.add_argument("--journal", help="Cited journal name or abbreviation (e.g. 'NEJM' or 'N Engl J Med')")
    p_check.add_argument("--claim", help="Specific claim to verify against official abstract (and OA full text)")
    p_check.add_argument(
        "--fulltext",
        action="store_true",
        help="Also fetch Open Access JATS full-text XML (NCBI PMC with Europe PMC fallback) if available",
    )
    p_check.add_argument("--no-abstract", action="store_true", help="Omit full official abstract from text output")
    p_check.add_argument("--json", action="store_true", help="Emit machine-readable JSON output")
    p_check.add_argument("--strict", action="store_true", help="Exit non-zero on WARNING diagnostics too")
    p_check.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"HTTP timeout in seconds (default {DEFAULT_TIMEOUT})"
    )
    p_check.set_defaults(func=cmd_check)

    # batch
    p_batch = sub.add_parser("batch", help="Verify a JSON array of citations/claims from a file (or '-' for stdin).")
    p_batch.add_argument("input", help="Path to JSON file containing an array of citation objects (or '-' for stdin)")
    p_batch.add_argument(
        "--fulltext",
        action="store_true",
        help="Fetch Open Access JATS full-text XML (NCBI PMC with Europe PMC fallback) for all claims",
    )
    p_batch.add_argument("--no-abstract", action="store_true", help="Omit full official abstract from text output")
    p_batch.add_argument("--json", action="store_true", help="Emit machine-readable JSON output")
    p_batch.add_argument("--strict", action="store_true", help="Exit non-zero on WARNING diagnostics too")
    p_batch.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"HTTP timeout in seconds (default {DEFAULT_TIMEOUT})"
    )
    p_batch.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Max concurrent verification workers (default {DEFAULT_WORKERS}; use 1 for sequential)",
    )
    p_batch.set_defaults(func=cmd_batch)

    # scan
    p_scan = sub.add_parser("scan", help="Auto-extract and verify all DOIs and PMIDs from .md/.txt/.docx/.pptx files.")
    p_scan.add_argument("file", help="Path to .md, .txt, .docx, or .pptx file")
    p_scan.add_argument(
        "--fulltext",
        action="store_true",
        help="Check Open Access JATS full-text XML availability (NCBI PMC with Europe PMC fallback)",
    )
    p_scan.add_argument("--no-abstract", action="store_true", help="Omit full official abstract from text output")
    p_scan.add_argument("--json", action="store_true", help="Emit machine-readable JSON output")
    p_scan.add_argument("--strict", action="store_true", help="Exit non-zero on WARNING diagnostics too")
    p_scan.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"HTTP timeout in seconds (default {DEFAULT_TIMEOUT})"
    )
    p_scan.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Max concurrent verification workers (default {DEFAULT_WORKERS}; use 1 for sequential)",
    )
    p_scan.set_defaults(func=cmd_scan)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
