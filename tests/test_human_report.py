#!/usr/bin/env python3
"""Offline tests for the human-facing report: reference parsing, checklist rows, terminal table, HTML page."""

from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_ref_verify import DAPA_HF_CROSSREF, FakeHttpClient

from ref_verify import checklist, html_report, models, refparse, scan, table
from ref_verify import cli as rv

VANCOUVER_DAPA = (
    "1. McMurray JJV, Solomon SD, Inzucchi SE, et al. Dapagliflozin in Patients with Heart Failure and "
    "Reduced Ejection Fraction. N Engl J Med. 2019;381(21):1995-2008. doi:10.1056/NEJMoa1911303"
)
APA_DAPA = (
    "McMurray, J. J. V., Solomon, S. D., & Inzucchi, S. E. (2019). Dapagliflozin in patients with heart "
    "failure and reduced ejection fraction. New England Journal of Medicine, 381(21), 1995-2008. "
    "https://doi.org/10.1056/NEJMoa1911303"
)


def _dapa_record(**overrides: object) -> models.PaperRecord:
    rec = models.PaperRecord(
        doi="10.1056/NEJMoa1911303",
        pmid="31535829",
        title="Dapagliflozin in Patients with Heart Failure and Reduced Ejection Fraction",
        authors=[models.Author(family="McMurray", initials="JJV")],
        published_print=2019,
        journal_full="New England Journal of Medicine",
        journal_abbrev="N Engl J Med",
        sources=["crossref"],
    )
    for key, value in overrides.items():
        setattr(rec, key, value)
    return rec


def _diag(code: str, severity: str, fix: str = "") -> models.Diagnostic:
    return models.Diagnostic(code=code, severity=severity, message=f"{code} message", fix=fix)


def _cells(row: checklist.ChecklistRow) -> dict[str, str]:
    return {key: cell.verdict for key, cell in row.cells.items()}


class TestReferenceParsing(unittest.TestCase):
    def test_vancouver_entry_yields_all_four_fields(self) -> None:
        ref = refparse.parse_reference(VANCOUVER_DAPA)
        self.assertEqual(ref.author, "McMurray JJV")
        self.assertEqual(ref.year, "2019")
        self.assertEqual(ref.title, "Dapagliflozin in Patients with Heart Failure and Reduced Ejection Fraction")
        self.assertEqual(ref.journal, "N Engl J Med")

    def test_apa_entry_yields_all_four_fields(self) -> None:
        ref = refparse.parse_reference(APA_DAPA)
        self.assertEqual(ref.author, "McMurray JJV")
        self.assertEqual(ref.year, "2019")
        self.assertEqual(ref.title, "Dapagliflozin in patients with heart failure and reduced ejection fraction")
        self.assertEqual(ref.journal, "New England Journal of Medicine")

    def test_partial_entry_keeps_only_confident_fields(self) -> None:
        ref = refparse.parse_reference("2. McMurray et al. (2019). doi:10.1056/NEJMoa1911303. PMID: 31535829.")
        self.assertEqual((ref.author, ref.year, ref.title, ref.journal), ("McMurray", "2019", None, None))
        # One-word "title" (an acronym) is not trusted as a title, but journal and year still are.
        short = refparse.parse_reference(
            "3. McMurray JJV et al. DAPA-HF. N Engl J Med. 2019. doi:10.1056/NEJMoa1911303"
        )
        self.assertEqual(
            (short.author, short.year, short.title, short.journal), ("McMurray JJV", "2019", None, "N Engl J Med")
        )

    def test_surname_particles_are_names(self) -> None:
        ref = refparse.parse_reference(
            "20.    van Geel MJ, Maatkamp M, Oostveen AM, et al. Comparison of the Dermatology Life Quality Index and "
            "the Children's Dermatology Life Quality Index. Br J Dermatol. 2016;174(3):578-84. doi:10.1111/bjd.14163"
        )
        self.assertEqual(ref.author, "van Geel MJ")
        self.assertTrue(ref.title and ref.title.startswith("Comparison of the Dermatology"))
        apa = refparse.parse_reference(
            "de Bruin, A. B., & Smith, J. (2020). A useful trial of things. Lancet, 1(2), 3-4."
        )
        self.assertEqual(apa.author, "de Bruin AB")

    def test_entry_without_author_list_keeps_no_title(self) -> None:
        # Consensus reports have no authors; "Title part 1. Title part 2. Journal. Year" must not be split
        # into authors="part 1" and title="part 2".
        ref = refparse.parse_reference(
            "17.    Severity scoring of atopic dermatitis: the SCORAD index. Consensus Report of the European Task "
            "Force on Atopic Dermatitis. Dermatology. 1993;186(1):23-31. doi:10.1159/000247298"
        )
        self.assertEqual((ref.author, ref.title), (None, None))
        self.assertEqual((ref.year, ref.journal), ("1993", "Dermatology"))

    def test_unrecognized_text_yields_nothing(self) -> None:
        for text in (
            "As shown in the landmark trial (doi:10.1056/NEJMoa1911303), dapagliflozin reduced events.",
            "Smith J. A study of A vs. B in adults. Lancet. 2020;1:1-2.",  # period inside title -> ambiguous split
        ):
            ref = refparse.parse_reference(text)
            self.assertIsNone(ref.title, text)
            self.assertIsNone(ref.journal, text)
        self.assertEqual(refparse.parse_reference("See doi:10.1056/NEJMoa1911303"), refparse.ParsedReference())

    def test_markdown_emphasis_and_vancouver_bracket_tags_are_ignored(self) -> None:
        ref = refparse.parse_reference(
            "1. McMurray JJV, et al. Dapagliflozin in Patients with Heart Failure [Internet]. "
            "*N Engl J Med*. 2019;381(21):1995-2008."
        )
        self.assertEqual(ref.title, "Dapagliflozin in Patients with Heart Failure")
        self.assertEqual(ref.journal, "N Engl J Med")
        translated = refparse.parse_reference("2. Li X. [Effect of drug X in rats]. Zhonghua Yi Xue Za Zhi. 2019;1:2.")
        self.assertIsNone(translated.title)


class TestScanFillsCitedMetadata(unittest.TestCase):
    def test_wrapped_reference_is_one_entry_not_phantom_rows(self) -> None:
        segments = [
            ("line 1", "## References"),
            ("line 2", "1. McMurray JJV, Solomon SD, Inzucchi SE, et al. Dapagliflozin in Patients with Heart"),
            ("line 3", "Failure and Reduced Ejection Fraction. N Engl J Med. 2019;381(21):1995-2008."),
            ("line 4", "doi:10.1056/NEJMoa1911303"),
            ("line 5", "2. Ali F, Vyas J, Finlay AY. Counting the Burden. Acta Derm Venereol. 2020;100(12):adv00161."),
        ]
        (cit,) = scan.extract_citations_from_segments(segments)
        self.assertEqual(cit.location, "line 2")
        self.assertEqual(cit.journal, "N Engl J Med")
        self.assertEqual(cit.title, "Dapagliflozin in Patients with Heart Failure and Reduced Ejection Fraction")
        unlinked = scan.extract_unlinked_bibliography_entries(segments)
        self.assertEqual([u.location for u in unlinked], ["line 5"])

    def test_one_paper_is_one_row_however_its_ids_are_spread(self) -> None:
        far_apart = VANCOUVER_DAPA.replace("doi:", "Some trailing commentary " * 8 + "doi:") + " PMID: 31535829"
        body_then_bib = [
            ("line 1", "SGLT2 inhibitors reduce events (doi:10.1056/NEJMoa1911303)."),
            ("line 2", "## References"),
            ("line 3", VANCOUVER_DAPA + " PMID: 31535829"),
        ]
        for segments in ([("line 1", "## References"), ("line 2", far_apart)], body_then_bib):
            cits = scan.extract_citations_from_segments(segments)
            self.assertEqual([(c.doi, c.pmid) for c in cits], [("10.1056/NEJMoa1911303", "31535829")])
            self.assertEqual(cits[0].journal, "N Engl J Med")

    def test_unlinked_entry_diagnostic_pinpoints_the_entry(self) -> None:
        entry = models.UnlinkedEntry(location="line 9", text="1. Ali F. Counting the Burden. Acta Derm Venereol. 2020.")
        diag = scan.unlinked_entry_diagnostic(entry)
        self.assertEqual((diag.code, diag.severity), ("UNLINKED_BIBLIOGRAPHY_ENTRIES", "WARNING"))
        self.assertTrue(diag.expected and diag.actual and diag.fix)
        self.assertIn("Ali F", diag.actual)

    def test_bibliography_entries_are_parsed_but_body_mentions_are_not(self) -> None:
        segments = [
            ("line 1", "Dapagliflozin helps (doi:10.1016/S0140-6736(20)31180-6)."),
            ("line 2", "## References"),
            ("line 3", VANCOUVER_DAPA),
        ]
        body, bib = scan.extract_citations_from_segments(segments)
        self.assertIsNone(body.title)
        self.assertIsNone(body.author)
        self.assertIsNone(body.reference_text)
        self.assertEqual(bib.author, "McMurray JJV")
        self.assertEqual(bib.year, "2019")
        self.assertEqual(bib.journal, "N Engl J Med")
        self.assertTrue(bib.title and bib.title.startswith("Dapagliflozin"))
        self.assertEqual(bib.reference_text, VANCOUVER_DAPA)

    def test_bibliography_entry_upgrades_an_earlier_body_mention_of_the_same_doi(self) -> None:
        segments = [
            ("line 1", "SGLT2 inhibitors reduce events (doi:10.1056/NEJMoa1911303)."),
            ("line 2", "## References"),
            ("line 3", VANCOUVER_DAPA),
        ]
        (cit,) = scan.extract_citations_from_segments(segments)
        self.assertEqual(cit.location, "line 3")
        self.assertEqual(cit.reference_text, VANCOUVER_DAPA)
        self.assertEqual(cit.journal, "N Engl J Med")

    def test_multi_line_slide_uses_the_line_holding_the_doi(self) -> None:
        slide = (
            "References\n1. Ali F. Counting the Burden. Acta Derm Venereol. 2020;100(12):adv00161.\n" + VANCOUVER_DAPA
        )
        (cit,) = scan.extract_citations_from_segments([("Slide 9", slide)])
        self.assertEqual(cit.reference_text, VANCOUVER_DAPA)
        self.assertEqual(cit.journal, "N Engl J Med")


class TestChecklistRows(unittest.TestCase):
    def test_resolved_citation_marks_unchecked_fields_as_skipped(self) -> None:
        cit = models.CitationInput(doi="10.1056/NEJMoa1911303", author="McMurray", year="2020", location="line 4")
        res = models.VerificationResult(
            input=cit, status="WARN", resolved=_dapa_record(), diagnostics=[_diag("YEAR_MISMATCH", "WARNING")]
        )
        row = checklist.build_row(1, res)
        self.assertEqual(
            _cells(row),
            {
                "identifier": "pass",
                "integrity": "pass",
                "title": "skip",
                "author": "pass",
                "year": "warn",
                "journal": "skip",
                "claim": "skip",
            },
        )
        self.assertEqual(
            (row.status, row.location, row.paper, row.identifier),
            ("WARN", "line 4", "McMurray 2019", "10.1056/NEJMoa1911303"),
        )
        self.assertEqual([d.code for _, d in row.problems()], ["YEAR_MISMATCH"])

    def test_unresolved_identifier_fails_and_skips_everything_else(self) -> None:
        cit = models.CitationInput(doi="10.9999/fake", title="Some title", year="2019")
        res = models.VerificationResult(input=cit, status="FAIL", diagnostics=[_diag("DOI_NOT_FOUND", "ERROR")])
        cells = _cells(checklist.build_row(1, res))
        self.assertEqual(cells.pop("identifier"), "fail")
        self.assertEqual(set(cells.values()), {"skip"})

    def test_fail_closed_when_nothing_resolved_and_no_identifier_diagnostic(self) -> None:
        res = models.VerificationResult(input=models.CitationInput(doi="10.1/x"), status="PASS")
        self.assertEqual(checklist.build_row(1, res).cells["identifier"].verdict, "fail")

    def test_retraction_and_notices_drive_integrity_cell(self) -> None:
        cit = models.CitationInput(doi="10.1056/NEJMoa1911303")
        retracted = models.VerificationResult(
            input=cit,
            status="FAIL",
            resolved=_dapa_record(retracted=True),
            diagnostics=[_diag("RETRACTED_PAPER", "CRITICAL")],
        )
        corrected = models.VerificationResult(
            input=cit, status="PASS", resolved=_dapa_record(), diagnostics=[_diag("HAS_CORRECTION_NOTICE", "INFO")]
        )
        self.assertEqual(checklist.build_row(1, retracted).cells["integrity"].verdict, "fail")
        self.assertEqual(checklist.build_row(2, corrected).cells["integrity"].verdict, "info")

    def test_field_is_skipped_when_official_record_lacks_it(self) -> None:
        # A record with no years/journal cannot confirm the cited year/journal: never show a false tick.
        cit = models.CitationInput(doi="10.1056/NEJMoa1911303", year="2019", journal="NEJM")
        rec = _dapa_record(published_print=None, journal_full=None, journal_abbrev=None)
        row = checklist.build_row(1, models.VerificationResult(input=cit, status="PASS", resolved=rec))
        self.assertEqual((row.cells["year"].verdict, row.cells["journal"].verdict), ("skip", "skip"))

    def test_claim_cell_follows_claim_report(self) -> None:
        cit = models.CitationInput(doi="10.1056/NEJMoa1911303", claim="HR 0.74")
        res = models.VerificationResult(
            input=cit,
            status="WARN",
            resolved=_dapa_record(),
            claim_report=models.ClaimReport(claim="HR 0.74"),
            diagnostics=[_diag("CLAIM_NUMBER_NOT_IN_ABSTRACT", "WARNING")],
        )
        self.assertEqual(checklist.build_row(1, res).cells["claim"].verdict, "warn")

    def test_unlinked_entry_becomes_unverified_row(self) -> None:
        entry = models.UnlinkedEntry(
            location="line 9", text="1. Ali F. Counting the Burden. Acta Derm Venereol. 2020;100."
        )
        row = checklist.build_unlinked_row(7, entry)
        self.assertEqual((row.index, row.status, row.location), (7, checklist.UNVERIFIED, "line 9"))
        cells = _cells(row)
        self.assertEqual(cells.pop("identifier"), "warn")
        self.assertEqual(set(cells.values()), {"skip"})

    def test_rows_are_numbered_continuously(self) -> None:
        res = models.VerificationResult(input=models.CitationInput(doi="10.1/a"), status="PASS")
        entries = [models.UnlinkedEntry(location=f"line {i}", text=f"{i}. Ref {i}. J. 2020.") for i in range(3)]
        rows = checklist.build_checklist([res, res], entries)
        self.assertEqual([r.index for r in rows], [1, 2, 3, 4, 5])

    def test_every_emitted_diagnostic_code_maps_to_a_column(self) -> None:
        pkg_dir = Path(models.__file__).parent
        emitted = {
            code
            for path in pkg_dir.rglob("*.py")
            for code in re.findall(r'code="([A-Z_]+)"', path.read_text(encoding="utf-8"))
        }
        mapped = {code for spec in checklist.CHECKS for code in spec.codes}
        self.assertEqual(sorted(emitted - mapped), [])


def _sample_rows() -> list[checklist.ChecklistRow]:
    ok = models.VerificationResult(
        input=models.CitationInput(doi="10.1056/NEJMoa1911303", author="McMurray", location="line 3"),
        status="PASS",
        resolved=_dapa_record(),
    )
    bad = models.VerificationResult(
        input=models.CitationInput(doi="10.9999/<script>alert(1)</script>", location="Slide 4"),
        status="FAIL",
        diagnostics=[_diag("DOI_NOT_FOUND", "ERROR", fix="Use the DOI printed on the article PDF.")],
    )
    unlinked = models.UnlinkedEntry(
        location="line 9", text="1. Ali F. Counting the Burden. Acta Derm Venereol. 2020;100."
    )
    return checklist.build_checklist([ok, bad], [unlinked])


class TestTerminalTable(unittest.TestCase):
    def test_table_shows_one_row_per_citation_with_symbols_problems_and_legend(self) -> None:
        out = table.format_checklist_table(_sample_rows(), width=120)
        lines = out.splitlines()
        header = next(line for line in lines if "Title" in line and "Journal" in line)
        self.assertIn("Retraction", header)
        row1 = next(line for line in lines if "McMurray 2019" in line)
        self.assertEqual(row1.count("✓"), 3)  # identifier, integrity, author
        self.assertIn("PASS", row1)
        row2 = next(line for line in lines if "Slide 4" in line and "FAIL" in line)
        self.assertIn("✗", row2)
        self.assertTrue(any("line 9" in line and "UNVERIFIED" in line for line in lines))
        self.assertIn("Use the DOI printed on the article PDF.", out)
        self.assertIn("not checked", out)
        self.assertNotIn("\x1b[", out)
        self.assertTrue(all(len(line) <= 120 for line in lines), max(lines, key=len))

    def test_ascii_mode_and_color(self) -> None:
        ascii_out = table.format_checklist_table(_sample_rows(), symbols=table.ASCII_SYMBOLS, width=100)
        ascii_out.encode("ascii")  # raises if any non-ASCII symbol leaked
        self.assertTrue(all(len(line) <= 100 for line in ascii_out.splitlines()))
        self.assertIn("\x1b[", table.format_checklist_table(_sample_rows(), color=True))


class TestHtmlReport(unittest.TestCase):
    def test_page_is_self_contained_escaped_and_linked(self) -> None:
        page = html_report.render_html_report(_sample_rows(), source="paper.docx", generated="2026-11-27 10:00")
        self.assertTrue(page.lower().startswith("<!doctype html>"))
        self.assertIn('href="https://doi.org/10.1056/NEJMoa1911303"', page)
        self.assertIn("paper.docx", page)
        self.assertIn("Use the DOI printed on the article PDF.", page)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotRegex(page, r'<(?:script|link)[^>]+(?:src|href)="http')  # no external assets
        self.assertIn("UNVERIFIED", page)


class TestCliHumanReport(unittest.TestCase):
    def _http(self) -> FakeHttpClient:
        return FakeHttpClient(
            [
                (
                    "api.crossref.org/works/10.1056/NEJMoa1911303",
                    models.HttpResponse(200, json.dumps(DAPA_HF_CROSSREF), "cr"),
                ),
                (
                    "europepmc/webservices/rest/search",
                    models.HttpResponse(200, json.dumps({"resultList": {"result": []}}), "epmc"),
                ),
                ("esearch.fcgi", models.HttpResponse(200, json.dumps({"esearchresult": {"idlist": []}}), "es")),
            ]
        )

    def test_scan_prints_table_and_writes_html(self) -> None:
        md = (
            "# References\n"
            + VANCOUVER_DAPA
            + "\n2. Ali F. Counting the Burden. Acta Derm Venereol. 2020;100(12):adv00161.\n"
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "paper.md"
            p.write_text(md, encoding="utf-8")
            html_path = Path(td) / "report.html"
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch.object(rv, "HttpClient", return_value=self._http()),
                mock.patch("sys.stdout", out),
                mock.patch("sys.stderr", err),
            ):
                rc = rv.main(["scan", str(p), "--no-abstract", "--html", str(html_path)])
            self.assertEqual(rc, 0)
            text = out.getvalue()
            self.assertIn("CITATION CHECKLIST", text)
            self.assertIn("McMurray 2019", text)
            self.assertIn("UNVERIFIED", text)
            page = html_path.read_text(encoding="utf-8")
            self.assertIn("10.1056/NEJMoa1911303", page)
            self.assertIn(str(html_path), err.getvalue())

    def test_json_mode_stays_pure_json_with_html(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            refs = Path(td) / "refs.json"
            refs.write_text(json.dumps([{"doi": "10.1056/NEJMoa1911303", "year": 2019}]), encoding="utf-8")
            html_path = Path(td) / "out.html"
            out = io.StringIO()
            with (
                mock.patch.object(rv, "HttpClient", return_value=self._http()),
                mock.patch("sys.stdout", out),
                mock.patch("sys.stderr", io.StringIO()),
            ):
                rc = rv.main(["batch", str(refs), "--json", "--html", str(html_path)])
            self.assertEqual(rc, 0)
            json.loads(out.getvalue())
            self.assertTrue(html_path.exists())


if __name__ == "__main__":
    unittest.main()
