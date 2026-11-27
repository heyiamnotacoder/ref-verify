"""Human-facing terminal checklist: one row per citation, one tick/cross column per check, then what to fix."""

from __future__ import annotations

import textwrap
from collections.abc import Sequence
from dataclasses import dataclass

from ref_verify.checklist import CHECKS, FAIL, INFO, PASS, SKIP, UNVERIFIED, WARN, ChecklistRow
from ref_verify.text import truncate


@dataclass(frozen=True)
class Symbols:
    passed: str
    info: str
    warn: str
    fail: str
    skip: str

    def for_verdict(self, verdict: str) -> str:
        return {PASS: self.passed, INFO: self.info, WARN: self.warn, FAIL: self.fail, SKIP: self.skip}[verdict]


UNICODE_SYMBOLS = Symbols(passed="✓", info="i", warn="⚠", fail="✗", skip="—")
ASCII_SYMBOLS = Symbols(passed="ok", info="i", warn="!", fail="X", skip="-")

_ANSI = {PASS: "32", INFO: "36", WARN: "33", FAIL: "31", SKIP: "2"}
_STATUS_VERDICT = {"PASS": PASS, "WARN": WARN, "FAIL": FAIL, UNVERIFIED: WARN}
_SEVERITY_VERDICT = {"CRITICAL": FAIL, "ERROR": FAIL, "WARNING": WARN, "INFO": INFO}
_GAP = "  "
_STATUS_WIDTH = len(UNVERIFIED)


def _paint(text: str, verdict: str, color: bool) -> str:
    return f"\x1b[{_ANSI[verdict]}m{text}\x1b[0m" if color else text


def _reference(row: ChecklistRow) -> str:
    return f"{row.paper}, {row.identifier}" if row.paper else row.identifier


def _column_widths(rows: Sequence[ChecklistRow], width: int) -> tuple[int, int, int]:
    """(index, where, reference) widths that fit the check columns into ``width`` where possible."""
    idx_w = max(1, len(str(len(rows))))
    checks_w = sum(len(spec.header) for spec in CHECKS)
    fixed = idx_w + checks_w + _STATUS_WIDTH + len(_GAP) * (len(CHECKS) + 3)
    where_w = min(14, max([len("Where")] + [len(r.location) for r in rows]))
    ref_w = max(len("Reference"), max([0] + [len(_reference(r)) for r in rows]))
    room = width - fixed
    if where_w + ref_w > room:
        ref_w = max(10, room - where_w)
        if where_w + ref_w > room:
            where_w = max(6, room - ref_w)
    return idx_w, where_w, ref_w


def _counts_line(rows: Sequence[ChecklistRow]) -> str:
    statuses = [r.status for r in rows]
    parts = [f"{statuses.count(s)} {s.lower()}" for s in ("PASS", "WARN", "FAIL", UNVERIFIED) if s in statuses]
    noun = "reference" if len(rows) == 1 else "references"
    return f"{len(rows)} {noun}: " + ", ".join(parts)


def _problems_block(rows: Sequence[ChecklistRow], symbols: Symbols, color: bool, width: int) -> list[str]:
    lines: list[str] = []
    body_w = max(40, width - 8)
    for row in rows:
        problems = row.problems()
        if not problems:
            continue
        lines.append(f"  [{row.index}] {truncate(f'{row.location} | {_reference(row)}', width - 6)}")
        if row.reference_text and row.unlinked is None:
            lines.append(f"      Cited as: {truncate(row.reference_text, width - 16)}")
        for spec, diag in problems:
            verdict = _SEVERITY_VERDICT.get(diag.severity, FAIL)
            label = spec.header if spec else "Other"
            mark = _paint(symbols.for_verdict(verdict), verdict, color)
            wrapped = textwrap.wrap(f"{label}: {diag.code}: {diag.message}", body_w) or [""]
            lines.append(f"      {mark} {wrapped[0]}")
            lines.extend(f"        {w}" for w in wrapped[1:])
            if diag.fix:
                for i, w in enumerate(textwrap.wrap(f"Fix: {diag.fix}", body_w)):
                    lines.append(f"        {w}" if i == 0 else f"          {w}")
    return lines


def format_checklist_table(
    rows: Sequence[ChecklistRow],
    *,
    symbols: Symbols = UNICODE_SYMBOLS,
    color: bool = False,
    width: int = 120,
) -> str:
    rule = "=" * min(width, 100)
    if not rows:
        return "\n".join([rule, "CITATION CHECKLIST: no references found.", rule])

    idx_w, where_w, ref_w = _column_widths(rows, width)
    header_cells = ["#".rjust(idx_w), "Where".ljust(where_w), "Reference".ljust(ref_w)]
    header_cells += [spec.header for spec in CHECKS] + ["Result".ljust(_STATUS_WIDTH)]
    header = _GAP.join(header_cells).rstrip()

    out = [rule, f"CITATION CHECKLIST  ({_counts_line(rows)})", rule, header, "-" * len(header)]
    for row in rows:
        cells = [
            str(row.index).rjust(idx_w),
            truncate(row.location, where_w).ljust(where_w),
            truncate(_reference(row), ref_w).ljust(ref_w),
        ]
        for spec in CHECKS:
            verdict = row.cells[spec.key].verdict
            cells.append(_paint(symbols.for_verdict(verdict).center(len(spec.header)), verdict, color))
        status_verdict = _STATUS_VERDICT.get(row.status, FAIL)
        cells.append(_paint(row.status, status_verdict, color))
        out.append(_GAP.join(cells).rstrip())

    legend = (
        f"{symbols.passed} pass   {symbols.warn} warning   {symbols.fail} fail   "
        f"{symbols.info} note   {symbols.skip} not checked (no cited value to compare)"
    )
    out += ["-" * len(header), legend]

    problems = _problems_block(rows, symbols, color, width)
    if problems:
        out += ["", "WHAT TO FIX:"] + problems
    out.append(rule)
    return "\n".join(out)
