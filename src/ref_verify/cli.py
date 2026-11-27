"""Command-line interface: argument parsing and the check, batch, and scan commands."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from ref_verify import __version__
from ref_verify.checklist import build_checklist
from ref_verify.html_report import render_html_report
from ref_verify.http_client import DEFAULT_TIMEOUT, HttpClient
from ref_verify.models import CitationInput, Diagnostic, UnlinkedEntry, VerificationResult
from ref_verify.report import (
    format_result_text,
    format_summary_text,
    format_unlinked_bibliography_text,
    result_to_dict,
    status_counts,
)
from ref_verify.scan import (
    ScanError,
    build_unlinked_bibliography_diagnostic,
    extract_citations_from_segments,
    extract_text_segments,
    extract_unlinked_bibliography_entries,
)
from ref_verify.table import ASCII_SYMBOLS, UNICODE_SYMBOLS, Symbols, format_checklist_table
from ref_verify.verify import DEFAULT_WORKERS, verify_citation, verify_citations_batch


def _emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _exit_code(results: Sequence[VerificationResult], strict: bool, extra_warnings: bool = False) -> int:
    """FAIL always exits 1; under --strict any WARN (or extra scan warning) exits 1 too."""
    if any(r.status == "FAIL" for r in results):
        return 1
    if strict and (extra_warnings or any(r.status == "WARN" for r in results)):
        return 1
    return 0


def _print_results(results: Sequence[VerificationResult], show_abstract: bool) -> None:
    for i, res in enumerate(results, start=1):
        print(format_result_text(res, index=i, show_abstract=show_abstract))


def _terminal_style() -> tuple[Symbols, bool, int]:
    """(symbols, color, width) for stdout: ASCII when the encoding cannot show ticks, color only on a TTY."""
    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "".join(vars(UNICODE_SYMBOLS).values()).encode(encoding)
        symbols = UNICODE_SYMBOLS
    except (LookupError, UnicodeEncodeError):
        symbols = ASCII_SYMBOLS
    color = bool(getattr(sys.stdout, "isatty", lambda: False)()) and "NO_COLOR" not in os.environ
    return symbols, color, shutil.get_terminal_size((120, 24)).columns


def _print_checklist(results: Sequence[VerificationResult], unlinked: Sequence[UnlinkedEntry] = ()) -> None:
    symbols, color, width = _terminal_style()
    print(format_checklist_table(build_checklist(results, unlinked), symbols=symbols, color=color, width=width))


def _write_html_report(
    path: str, results: Sequence[VerificationResult], unlinked: Sequence[UnlinkedEntry], source: str
) -> bool:
    page = render_html_report(
        build_checklist(results, unlinked), source=source, generated=datetime.now().strftime("%Y-%m-%d %H:%M")
    )
    try:
        Path(path).write_text(page, encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: Could not write HTML report '{path}': {exc}", file=sys.stderr)
        return False
    print(f"HTML report written to {path}", file=sys.stderr)
    return True


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
        _emit_json(result_to_dict(res))
    else:
        print(format_result_text(res, show_abstract=not args.no_abstract))
    return _exit_code([res], args.strict)


def _citation_from_entry(entry: dict[str, Any], idx: int, fulltext: bool) -> CitationInput:
    return CitationInput(
        doi=entry.get("doi"),
        pmid=str(entry["pmid"]) if entry.get("pmid") is not None else None,
        title=entry.get("title"),
        author=entry.get("author"),
        year=str(entry["year"]) if entry.get("year") is not None else None,
        journal=entry.get("journal"),
        claim=entry.get("claim"),
        label=entry.get("id") or entry.get("label") or f"ref-{idx}",
        fulltext=bool(entry.get("fulltext") or fulltext),
    )


def cmd_batch(args: argparse.Namespace) -> int:
    try:
        raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
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

    citations = [
        _citation_from_entry(entry, idx, args.fulltext)
        for idx, entry in enumerate(items, start=1)
        if isinstance(entry, dict)
    ]

    with HttpClient(timeout=args.timeout) as http:
        results = verify_citations_batch(citations, http, max_workers=args.workers)

    if args.json:
        _emit_json({"summary": status_counts(results), "results": [result_to_dict(r) for r in results]})
    else:
        _print_results(results, show_abstract=not args.no_abstract)
        _print_checklist(results)
        print(format_summary_text(results, header="BATCH SUMMARY"))
    if args.html and not _write_html_report(args.html, results, (), source=args.input):
        return 2
    return _exit_code(results, args.strict)


def cmd_scan(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"ERROR: File not found: {path}", file=sys.stderr)
        return 2

    try:
        segments = extract_text_segments(path)
    except ScanError as exc:
        print(f"ERROR: Could not read '{path}': {exc}", file=sys.stderr)
        return 2

    citations = extract_citations_from_segments(segments)
    unlinked_entries = extract_unlinked_bibliography_entries(segments)
    scan_diag = build_unlinked_bibliography_diagnostic(unlinked_entries)
    scan_diagnostics: list[Diagnostic] = [scan_diag] if scan_diag else []

    results: list[VerificationResult] = []
    if citations:
        for cit in citations:
            cit.fulltext = args.fulltext
        with HttpClient(timeout=args.timeout) as http:
            results = verify_citations_batch(citations, http, max_workers=args.workers)

    if args.json:
        out: dict[str, Any] = {
            "file": str(path),
            "extracted_count": len(citations),
            "unlinked_bibliography_count": len(unlinked_entries),
            "unlinked_bibliography_entries": [asdict(e) for e in unlinked_entries],
            "scan_diagnostics": [asdict(d) for d in scan_diagnostics],
        }
        if citations:
            out["summary"] = status_counts(results)
        out["results"] = [result_to_dict(r) for r in results]
        _emit_json(out)
    else:
        if not citations:
            print(f"SCAN: No DOIs or PMIDs found in '{path}'.")
        else:
            print(f"SCAN FILE: {path}  |  EXTRACTED REFERENCES: {len(citations)}")
            _print_results(results, show_abstract=not args.no_abstract)
        if scan_diag:
            print(format_unlinked_bibliography_text(scan_diag, unlinked_entries))
        if citations or unlinked_entries:
            _print_checklist(results, unlinked_entries)
        if citations:
            print(format_summary_text(results, header="SCAN SUMMARY"))
    if args.html and not _write_html_report(args.html, results, unlinked_entries, source=str(path)):
        return 2
    return _exit_code(results, args.strict, extra_warnings=bool(scan_diagnostics))


def _output_flags() -> argparse.ArgumentParser:
    """Flags shared by every subcommand."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--no-abstract", action="store_true", help="Omit full official abstract from text output")
    common.add_argument("--json", action="store_true", help="Emit machine-readable JSON output")
    common.add_argument("--strict", action="store_true", help="Exit non-zero on WARNING diagnostics too")
    common.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"HTTP timeout in seconds (default {DEFAULT_TIMEOUT})"
    )
    return common


def _worker_flags() -> argparse.ArgumentParser:
    """Concurrency flag shared by the multi-citation subcommands."""
    pool = argparse.ArgumentParser(add_help=False)
    pool.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Max concurrent verification workers (default {DEFAULT_WORKERS}; use 1 for sequential)",
    )
    return pool


def _report_flags() -> argparse.ArgumentParser:
    """Human-report flag shared by the multi-citation subcommands."""
    report = argparse.ArgumentParser(add_help=False)
    report.add_argument(
        "--html",
        metavar="PATH",
        help="Also write a self-contained HTML checklist report (one row per citation) to PATH",
    )
    return report


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
    common, pool, report = _output_flags(), _worker_flags(), _report_flags()

    p_check = sub.add_parser(
        "check", parents=[common], help="Verify a single citation and optional quantitative claim."
    )
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
    p_check.set_defaults(func=cmd_check)

    p_batch = sub.add_parser(
        "batch",
        parents=[common, pool, report],
        help="Verify a JSON array of citations/claims from a file (or '-' for stdin).",
    )
    p_batch.add_argument("input", help="Path to JSON file containing an array of citation objects (or '-' for stdin)")
    p_batch.add_argument(
        "--fulltext",
        action="store_true",
        help="Fetch Open Access JATS full-text XML (NCBI PMC with Europe PMC fallback) for all claims",
    )
    p_batch.set_defaults(func=cmd_batch)

    p_scan = sub.add_parser(
        "scan",
        parents=[common, pool, report],
        help="Auto-extract and verify all DOIs and PMIDs from .md/.txt/.docx/.pptx files.",
    )
    p_scan.add_argument("file", help="Path to .md, .txt, .docx, or .pptx file")
    p_scan.add_argument(
        "--fulltext",
        action="store_true",
        help="Check Open Access JATS full-text XML availability (NCBI PMC with Europe PMC fallback)",
    )
    p_scan.set_defaults(func=cmd_scan)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
