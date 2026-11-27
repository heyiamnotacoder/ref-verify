"""Self-contained HTML checklist report (inline CSS/JS, no external assets) built from checklist rows."""

from __future__ import annotations

import re
from collections.abc import Sequence
from html import escape
from urllib.parse import quote

from ref_verify.checklist import (
    CHECKS,
    VERDICT_LABELS,
    ChecklistRow,
    severity_verdict,
    status_counts,
    status_verdict,
)
from ref_verify.models import Diagnostic, PaperRecord
from ref_verify.table import UNICODE_SYMBOLS, legend

_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")

_CSS = """
:root{--bg:#f7f6f3;--panel:#fff;--ink:#1d1d1f;--muted:#6b6b70;--line:#e4e2dc;
--pass:#1f7a44;--pass-bg:#e6f4ea;--warn:#8a5a00;--warn-bg:#fdf1d8;--fail:#b3261e;--fail-bg:#fbe7e5;
--info:#1f5f8b;--info-bg:#e5f0f8;--skip:#a3a3a8}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--panel:#1c1c1f;--ink:#ececef;--muted:#9a9aa2;--line:#2c2c31;
--pass:#6fd39a;--pass-bg:#16301f;--warn:#f2c36b;--warn-bg:#352a12;--fail:#ff8a80;--fail-bg:#3a1a18;
--info:#8cc4ee;--info-bg:#15283a;--skip:#5c5c63}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:1180px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}
.meta{color:var(--muted);margin:0 0 20px;overflow-wrap:anywhere}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px}
.chip{border-radius:999px;padding:4px 12px;font-weight:600;font-size:14px;background:var(--panel);border:1px solid var(--line)}
.chip.pass{color:var(--pass);background:var(--pass-bg);border-color:transparent}
.chip.warn{color:var(--warn);background:var(--warn-bg);border-color:transparent}
.chip.fail{color:var(--fail);background:var(--fail-bg);border-color:transparent}
.toolbar{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;align-items:center}
.toolbar button{font:inherit;border:1px solid var(--line);background:var(--panel);color:var(--ink);border-radius:8px;padding:6px 12px;cursor:pointer}
.toolbar button[aria-pressed=true]{border-color:var(--ink)}
.legend{color:var(--muted);font-size:13px;margin-left:auto}
.wrap{overflow-x:auto;background:var(--panel);border:1px solid var(--line);border-radius:12px}
table{border-collapse:collapse;width:100%;min-width:900px}
th,td{padding:10px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{position:sticky;top:0;background:var(--panel);font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
th.c,td.c{text-align:center;width:72px}
tr.row{cursor:pointer}tr.row:hover td{background:color-mix(in srgb,var(--line) 35%,transparent)}
tr.row:focus-visible{outline:2px solid var(--info);outline-offset:-2px}
.v{display:inline-block;min-width:26px;padding:2px 6px;border-radius:6px;font-weight:700}
.v.pass{color:var(--pass);background:var(--pass-bg)}.v.warn{color:var(--warn);background:var(--warn-bg)}
.v.fail{color:var(--fail);background:var(--fail-bg)}.v.info{color:var(--info);background:var(--info-bg)}
.v.skip{color:var(--skip)}
.status{font-weight:700;font-size:13px;padding:3px 8px;border-radius:6px;white-space:nowrap}
.status.pass{color:var(--pass);background:var(--pass-bg)}.status.warn{color:var(--warn);background:var(--warn-bg)}
.status.fail{color:var(--fail);background:var(--fail-bg)}
.paper{font-weight:600}.ident{color:var(--muted);font-size:13px;overflow-wrap:anywhere}
a{color:var(--info)}
tr.detail td{background:var(--bg);padding:16px 20px}
dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;margin:0 0 12px}
dt{color:var(--muted)}dd{margin:0;word-break:break-word}
.problem{border-left:3px solid var(--line);padding:6px 12px;margin:8px 0;background:var(--panel);border-radius:0 8px 8px 0}
.problem.fail{border-color:var(--fail)}.problem.warn{border-color:var(--warn)}.problem.info{border-color:var(--info)}
.problem code{font-size:12px;color:var(--muted)}
.problem .fix{margin-top:4px}
.ok{color:var(--pass);margin:0}
@media print{.toolbar{display:none}tr.detail{display:table-row!important}th{position:static}}
"""

_JS = """
document.querySelectorAll('tr.row').forEach(function(r){
  function toggle(){var d=r.nextElementSibling;var open=d.hidden;d.hidden=!open;r.setAttribute('aria-expanded',open);}
  r.addEventListener('click',function(e){if(e.target.closest('a'))return;toggle();});
  r.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();toggle();}});
});
document.querySelectorAll('.toolbar button').forEach(function(b){
  b.addEventListener('click',function(){
    var only=b.dataset.filter==='problems';
    document.querySelectorAll('.toolbar button').forEach(function(x){x.setAttribute('aria-pressed',x===b);});
    document.querySelectorAll('tr.row').forEach(function(r){
      var hide=only&&r.dataset.ok==='1';r.hidden=hide;if(hide)r.nextElementSibling.hidden=true;});
  });
});
"""


def _doi_link(doi: str | None) -> str:
    if not doi or not _DOI_RE.match(doi.strip()):
        return escape(doi or "")
    doi = doi.strip()
    return f'<a href="https://doi.org/{escape(quote(doi, safe="/:;()._-"))}">{escape(doi)}</a>'


def _pmid_link(pmid: str | None) -> str:
    if not pmid or not pmid.strip().isdigit():
        return escape(pmid or "")
    pmid = pmid.strip()
    return f'<a href="https://pubmed.ncbi.nlm.nih.gov/{pmid}/">PMID {pmid}</a>'


def _identifier_html(row: ChecklistRow) -> str:
    parts = [p for p in (_doi_link(row.doi) if row.doi else "", _pmid_link(row.pmid) if row.pmid else "") if p]
    return " · ".join(parts) or escape(row.identifier)


def _official_block(rec: PaperRecord) -> str:
    authors = ", ".join(a.display for a in rec.authors[:6]) + (
        f", … ({len(rec.authors)})" if len(rec.authors) > 6 else ""
    )
    journal = rec.journal_full or rec.journal_abbrev or ""
    ids = " · ".join(p for p in (_doi_link(rec.doi) if rec.doi else "", _pmid_link(rec.pmid) if rec.pmid else "") if p)
    fields = [
        ("Official title", escape(rec.title or "N/A")),
        ("Authors", escape(authors or "N/A")),
        ("Journal", escape(journal or "N/A")),
        ("Year", escape(str(rec.best_year() or "N/A"))),
        ("Identifiers", ids or "N/A"),
        ("Sources", escape(", ".join(rec.sources))),
    ]
    return "<dl>" + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in fields) + "</dl>"


def _problem_html(label: str, d: Diagnostic) -> str:
    cls = severity_verdict(d.severity)
    parts = [f"<div><strong>{escape(label)}</strong> — {escape(d.message)} <code>{escape(d.code)}</code></div>"]
    if d.expected:
        parts.append(f"<div>Expected: {escape(d.expected)}</div>")
    if d.actual:
        parts.append(f"<div>Actual: {escape(d.actual)}</div>")
    if d.fix:
        parts.append(f'<div class="fix"><strong>Fix:</strong> {escape(d.fix)}</div>')
    return f'<div class="problem {cls}">' + "".join(parts) + "</div>"


def _detail_html(row: ChecklistRow) -> str:
    parts: list[str] = []
    if row.reference_text:
        parts.append(f"<dl><dt>Cited as</dt><dd>{escape(row.reference_text)}</dd></dl>")
    if row.result is not None and row.result.resolved is not None:
        parts.append(_official_block(row.result.resolved))
    problems = row.problems()
    if problems:
        parts.extend(_problem_html(spec.header if spec else "Other", d) for spec, d in problems)
    else:
        parts.append('<p class="ok">Every check that could run passed.</p>')
    if row.result is not None and row.result.did_you_mean:
        items = "".join(
            f"<li>{escape(str(s.get('title') or ''))} — {_doi_link(s.get('doi'))} "
            f"({escape(str(s.get('first_author') or '?'))}, {escape(str(s.get('year') or '?'))})</li>"
            for s in row.result.did_you_mean
        )
        parts.append(f"<div><strong>Did you mean:</strong><ul>{items}</ul></div>")
    return "".join(parts)


def _row_html(row: ChecklistRow) -> str:
    ok = row.status == "PASS" and not row.problems()
    cells = [
        f"<td>{row.index}</td>",
        f"<td>{escape(row.location)}</td>",
        f'<td><div class="paper">{escape(row.paper)}</div><div class="ident">{_identifier_html(row)}</div></td>',
    ]
    for spec in CHECKS:
        cell = row.cells[spec.key]
        tip = "; ".join(d.message for d in cell.diagnostics) or VERDICT_LABELS[cell.verdict]
        cells.append(
            f'<td class="c"><span class="v {cell.verdict}" title="{escape(spec.header)}: {escape(tip)}" '
            f'aria-label="{escape(spec.header)}: {VERDICT_LABELS[cell.verdict]}">{UNICODE_SYMBOLS.for_verdict(cell.verdict)}</span></td>'
        )
    status_cls = status_verdict(row.status)
    cells.append(f'<td><span class="status {status_cls}">{escape(row.status)}</span></td>')
    span = len(CHECKS) + 4
    return (
        f'<tr class="row" tabindex="0" aria-expanded="false" data-ok="{1 if ok else 0}">{"".join(cells)}</tr>'
        f'<tr class="detail" hidden><td colspan="{span}">{_detail_html(row)}</td></tr>'
    )


def render_html_report(rows: Sequence[ChecklistRow], *, source: str | None = None, generated: str = "") -> str:
    chips = [f'<span class="chip">{len(rows)} references</span>']
    chips += [
        f'<span class="chip {status_verdict(status)}">{n} {status.lower()}</span>' for status, n in status_counts(rows)
    ]
    meta = " · ".join(escape(p) for p in (source or "", f"Generated {generated}" if generated else "") if p)
    headers = "".join(f'<th class="c" title="{escape(spec.description)}">{escape(spec.header)}</th>' for spec in CHECKS)
    body = "".join(_row_html(r) for r in rows) or f'<tr><td colspan="{len(CHECKS) + 4}">No references found.</td></tr>'
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Citation check</title><style>{_CSS}</style></head><body><main>"
        f'<h1>Citation check</h1><p class="meta">{meta}</p>'
        f'<div class="chips">{"".join(chips)}</div>'
        '<div class="toolbar"><button data-filter="all" aria-pressed="true">All</button>'
        '<button data-filter="problems" aria-pressed="false">Needs attention</button>'
        f'<span class="legend">{escape(legend(UNICODE_SYMBOLS, " · "))}. Click a row for details.</span></div>'
        '<div class="wrap"><table><thead><tr><th>#</th><th>Where</th><th>Reference</th>'
        f"{headers}<th>Result</th></tr></thead><tbody>{body}</tbody></table></div>"
        f"<script>{_JS}</script></main></body></html>"
    )
