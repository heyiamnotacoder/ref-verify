#!/usr/bin/env python3
"""Offline tests for checking the manuscript's own citing sentences against the cited papers."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from test_ref_verify import DAPA_HF_CROSSREF, DAPA_HF_PUBMED_XML, FakeHttpClient

from ref_verify import checklist, citing, grounding, identity, models, scan
from ref_verify import cli as rv

DAPA_REF = (
    "1. McMurray JJV, Solomon SD, Inzucchi SE, et al. Dapagliflozin in Patients with Heart Failure and "
    "Reduced Ejection Fraction. N Engl J Med. 2019;381(21):1995-2008. doi:10.1056/NEJMoa1911303"
)
EMPA_REF = (
    "2. Zinman B, Wanner C, Lachin JM, et al. Empagliflozin, Cardiovascular Outcomes, and Mortality in "
    "Type 2 Diabetes. N Engl J Med. 2015;373(22):2117-28. doi:10.1056/NEJMoa1504720"
)
UNLINKED_REF = "3. Ali F, Vyas J, Finlay AY. Counting the Burden. Acta Derm Venereol. 2020;100(12):adv00161."


def _scan(segments: list[tuple[str, str]]) -> list[models.CitationInput]:
    cits = scan.extract_citations_from_segments(segments)
    citing.attach_citing_sentences(segments, cits)
    return cits


def _sentences(cit: models.CitationInput) -> list[str]:
    return [s.claim for s in cit.citing_sentences]


class TestFindingCitingSentences(unittest.TestCase):
    def test_bracket_markers_and_ranges_map_to_numbered_references(self) -> None:
        segments = [
            ("line 1", "Dapagliflozin lowered the primary outcome (HR 0.74) [1]. Background only here."),
            ("line 2", "Both drugs act on SGLT2 [1-2]. Burden is high [3]."),
            ("line 3", "## References"),
            ("line 4", DAPA_REF),
            ("line 5", EMPA_REF),
            ("line 6", UNLINKED_REF),
        ]
        dapa, empa = _scan(segments)
        self.assertEqual(
            _sentences(dapa), ["Dapagliflozin lowered the primary outcome (HR 0.74).", "Both drugs act on SGLT2."]
        )
        self.assertEqual(_sentences(empa), ["Both drugs act on SGLT2."])
        shared = dapa.citing_sentences[1]
        self.assertEqual(shared.location, "line 2")
        self.assertEqual(sorted(shared.cited_keys), ["10.1056/nejmoa1504720", "10.1056/nejmoa1911303"])
        self.assertEqual(shared.uncheckable_refs, 0)

    def test_sentence_citing_an_unlinked_reference_counts_it_as_uncheckable(self) -> None:
        segments = [
            ("line 1", "Quality of life fell by 40% [1,3]."),
            ("line 2", "## References"),
            ("line 3", DAPA_REF),
            ("line 4", "2. Placeholder entry without identifiers, 2018."),
            ("line 5", UNLINKED_REF),
        ]
        (dapa,) = _scan(segments)
        (sentence,) = dapa.citing_sentences
        self.assertEqual(sentence.uncheckable_refs, 1)

    def test_author_year_and_inline_doi_citations(self) -> None:
        segments = [
            ("line 1", "As McMurray et al. (2019) showed, 4744 patients were enrolled. Unrelated sentence."),
            ("line 2", "Mortality fell (Zinman et al., 2015; McMurray et al., 2019)."),
            ("line 3", "The trial is at doi:10.1056/NEJMoa1911303 and enrolled 4744 patients."),
            ("line 4", "## References"),
            (
                "line 5",
                "McMurray, J. J. V., Solomon, S. D. (2019). Dapagliflozin in patients with heart failure and reduced "
                "ejection fraction. New England Journal of Medicine, 381(21), 1995-2008. https://doi.org/10.1056/NEJMoa1911303",
            ),
        ]
        (dapa,) = _scan(segments)
        self.assertEqual(
            _sentences(dapa),
            [
                "As McMurray et al. showed, 4744 patients were enrolled.",
                "Mortality fell.",
                "The trial is at and enrolled 4744 patients.",
            ],
        )

    def test_unnumbered_reference_list_uses_entry_order(self) -> None:
        # Word auto-numbering does not put "1." into the text, so [n] maps to the n-th entry.
        segments = [
            ("document:p1", "Empagliflozin reduced death [2]."),
            ("document:p2", "References"),
            ("document:p3", DAPA_REF[3:]),
            ("document:p4", EMPA_REF[3:]),
        ]
        _dapa, empa = _scan(segments)
        self.assertEqual(_sentences(empa), ["Empagliflozin reduced death."])
        self.assertEqual(_dapa.citing_sentences, [])

    def test_docx_superscript_citations_become_markers_but_units_do_not(self) -> None:
        w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        sup = '<w:rPr><w:vertAlign w:val="superscript"/></w:rPr>'
        doc_xml = (
            f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{w}"><w:body>'
            f"<w:p><w:r><w:t>Risk fell by 26%.</w:t></w:r><w:r>{sup}<w:t>1,2</w:t></w:r>"
            f"<w:r><w:t> Patients over 30 kg/m</w:t></w:r><w:r>{sup}<w:t>2</w:t></w:r><w:r><w:t> were excluded.</w:t></w:r></w:p>"
            "</w:body></w:document>"
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "m.docx"
            with zipfile.ZipFile(p, "w") as zf:
                zf.writestr("word/document.xml", doc_xml)
            ((_, text),) = scan.extract_text_segments(p)
        self.assertEqual(text, "Risk fell by 26%.[1,2] Patients over 30 kg/m2 were excluded.")

    def test_statistics_superscripts_are_not_citations(self) -> None:
        w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        sup = '<w:rPr><w:vertAlign w:val="superscript"/></w:rPr>'
        runs = "".join(
            f"<w:r><w:t>{before}</w:t></w:r><w:r>{sup}<w:t>2</w:t></w:r>"
            for before in ("Heterogeneity was high (I", " = 45%), R", " = 0.8, \u03c7")
        )
        doc_xml = (
            f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{w}"><w:body>'
            f"<w:p>{runs}<w:r><w:t> = 12.1.</w:t></w:r></w:p></w:body></w:document>"
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "m.docx"
            with zipfile.ZipFile(p, "w") as zf:
                zf.writestr("word/document.xml", doc_xml)
            ((_, text),) = scan.extract_text_segments(p)
        self.assertNotIn("[2]", text)

    def test_parenthesised_and_typed_superscript_markers(self) -> None:
        segments = [
            ("line 1", "Empagliflozin lowered CV death by 38% (2). Steps were (1) screening and (2) dosing."),
            ("line 2", "Dapagliflozin cut events (HR 0.74)¹, and χ² was 4.1 at 30 kg/m²."),
            ("line 3", "## References"),
            ("line 4", DAPA_REF),
            ("line 5", EMPA_REF),
        ]
        dapa, empa = _scan(segments)
        self.assertEqual(_sentences(empa), ["Empagliflozin lowered CV death by 38%."])
        self.assertEqual(len(dapa.citing_sentences), 1)
        self.assertNotIn("¹", dapa.citing_sentences[0].claim)

    def test_superscript_after_a_number_is_a_citation_but_ten_cubed_is_not(self) -> None:
        w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        sup = '<w:rPr><w:vertAlign w:val="superscript"/></w:rPr>'
        doc_xml = (
            f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{w}"><w:body><w:p>'
            f"<w:r><w:t>HR 0.74</w:t></w:r><w:r>{sup}<w:t>1</w:t></w:r>"
            f"<w:r><w:t> with 10</w:t></w:r><w:r>{sup}<w:t>3</w:t></w:r><w:r><w:t> cells.</w:t></w:r>"
            "</w:p></w:body></w:document>"
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "m.docx"
            with zipfile.ZipFile(p, "w") as zf:
                zf.writestr("word/document.xml", doc_xml)
            ((_, text),) = scan.extract_text_segments(p)
        self.assertEqual(text, "HR 0.74[1] with 103 cells.")

    def test_same_number_listed_twice_for_the_same_paper_still_maps(self) -> None:
        segments = [
            ("line 1", "Mortality fell by 26% [1]."),
            ("line 2", "## References"),
            ("line 3", DAPA_REF),
            ("line 4", DAPA_REF),
        ]
        (dapa,) = _scan(segments)
        self.assertEqual(_sentences(dapa), ["Mortality fell by 26%."])

    def test_duplicated_reference_entry_still_maps_its_number(self) -> None:
        segments = [
            ("line 1", "Mortality fell by 26% [2]."),
            ("line 2", "## References"),
            ("line 3", DAPA_REF),
            ("line 4", "2." + DAPA_REF[2:]),
        ]
        (dapa,) = _scan(segments)
        self.assertEqual(_sentences(dapa), ["Mortality fell by 26%."])


class TestDuplicateReferences(unittest.TestCase):
    def test_same_paper_listed_twice_in_references_is_recorded(self) -> None:
        segments = [
            ("line 1", "SGLT2 inhibitors help (doi:10.1056/NEJMoa1911303)."),
            ("line 2", "## References"),
            ("line 3", DAPA_REF),
            ("line 4", EMPA_REF),
            (
                "line 5",
                "3. McMurray JJV, et al. Dapagliflozin in HFrEF. N Engl J Med. 2019. PMID: 31535829 doi:10.1056/NEJMoa1911303",
            ),
        ]
        dapa, empa = scan.extract_citations_from_segments(segments)
        self.assertEqual(dapa.location, "line 3")
        self.assertEqual(dapa.duplicate_locations, ["line 5"])  # the body mention is not a duplicate entry
        self.assertEqual(empa.duplicate_locations, [])

    def test_duplicate_reference_diagnostic_names_both_entries_and_marks_the_row(self) -> None:
        cit = models.CitationInput(doi="10.1056/NEJMoa1911303", location="line 3", duplicate_locations=["line 5"])
        diag = identity.duplicate_reference_diagnostic(cit)
        self.assertEqual((diag.code, diag.severity), ("DUPLICATE_REFERENCE", "WARNING"))
        self.assertIn("line 3", diag.actual)
        self.assertIn("line 5", diag.actual)
        self.assertTrue(diag.expected and diag.fix)
        res = models.VerificationResult(
            input=cit, status="WARN", resolved=models.PaperRecord(doi=cit.doi), diagnostics=[diag]
        )
        self.assertEqual(checklist.build_row(1, res).cells["identifier"].verdict, "warn")


class TestSentenceGrounding(unittest.TestCase):
    def test_sentence_numbers_ignore_years_and_stray_digits(self) -> None:
        nums = grounding.sentence_claim_numbers(
            "In 2019, type 2 diabetes patients (n=4744) had HR 0.74 and 16.3% events."
        )
        self.assertIn("HR 0.74", nums)
        self.assertIn("16.3%", nums)
        self.assertFalse({"2019", "2"} & set(nums), nums)

    def _result(
        self, key: str, sentence: models.CitingSentence, missing: list[str], numbers: list[str]
    ) -> models.VerificationResult:
        report = models.ClaimReport(
            claim=sentence.claim,
            extracted_numbers=numbers,
            missing_numbers=missing,
            matched_numbers=[n for n in numbers if n not in missing],
            abstract_available=True,
        )
        cit = models.CitationInput(doi=key, citing_sentences=[sentence])
        return models.VerificationResult(
            input=cit,
            status="PASS",
            resolved=models.PaperRecord(doi=key, title="T", abstract="A"),
            sentence_checks=[models.SentenceCheck(sentence=sentence, report=report, diagnostics=[])],
        )

    def test_single_cited_sentence_with_missing_number_warns_and_quotes_the_sentence(self) -> None:
        s = models.CitingSentence("line 7", "It cut events (HR 0.52) [1].", "It cut events (HR 0.52).", ["10.1/a"])
        (res,) = grounding.reconcile_sentence_checks([self._result("10.1/a", s, ["HR 0.52"], ["HR 0.52"])])
        self.assertEqual(res.status, "WARN")
        (diag,) = res.diagnostics
        self.assertEqual((diag.code, diag.severity), ("CLAIM_NUMBER_NOT_IN_ABSTRACT", "WARNING"))
        self.assertIn("line 7", diag.message)
        self.assertIn("It cut events (HR 0.52)", diag.message)

    def test_co_cited_numbers_count_if_any_cited_paper_has_them(self) -> None:
        s = models.CitingSentence("line 2", "x [1,2].", "HR 0.74 and 38%.", ["10.1/a", "10.1/b"])
        found_in_b = grounding.reconcile_sentence_checks(
            [
                self._result("10.1/a", s, ["38%"], ["HR 0.74", "38%"]),
                self._result("10.1/b", s, ["HR 0.74"], ["HR 0.74", "38%"]),
            ]
        )
        self.assertEqual([r.diagnostics for r in found_in_b], [[], []])
        # Each paper's panel can tell "missing here but found in a co-cited paper" from "missing everywhere".
        self.assertEqual([r.sentence_checks[0].missing_after_cocitation for r in found_in_b], [[], []])
        missing_everywhere = grounding.reconcile_sentence_checks(
            [
                self._result("10.1/a", s, ["38%"], ["HR 0.74", "38%"]),
                self._result("10.1/b", s, ["38%"], ["HR 0.74", "38%"]),
            ]
        )
        self.assertEqual(
            [[d.code for d in r.diagnostics] for r in missing_everywhere], [["CLAIM_NUMBER_NOT_IN_ABSTRACT"]] * 2
        )
        self.assertIn("38%", missing_everywhere[0].diagnostics[0].message)

    def test_relative_risk_reduction_derived_from_a_ratio_counts_as_found(self) -> None:
        s = models.CitingSentence("line 1", "x [1].", "Risk fell by 26% (HR 0.74).", ["10.1/a"])
        rec = models.PaperRecord(doi="10.1/a", abstract="The hazard ratio was 0.74 (95% CI, 0.65 to 0.85).")
        check = grounding.ground_sentence(s, rec)
        self.assertEqual(check.report.missing_numbers, [])
        self.assertTrue(any("26%" in m and "derived" in m for m in check.report.matched_numbers), check.report)

    def test_sentence_without_numbers_never_warns_about_a_missing_abstract(self) -> None:
        s = models.CitingSentence("line 1", "SGLT2i are recommended [1].", "SGLT2i are recommended.", ["10.1/a"])
        rec = models.PaperRecord(doi="10.1/a", title="Guideline")
        res = models.VerificationResult(
            input=models.CitationInput(doi="10.1/a", citing_sentences=[s]),
            status="PASS",
            resolved=rec,
            sentence_checks=[grounding.ground_sentence(s, rec)],
        )
        (out,) = grounding.reconcile_sentence_checks([res])
        self.assertEqual((out.status, out.diagnostics), ("PASS", []))

    def test_co_cited_population_mismatch_needs_every_cited_paper_to_disagree(self) -> None:
        s = models.CitingSentence("line 1", "x [1,2].", "In HFpEF (HR 0.74).", ["10.1/a", "10.1/b"])
        cohort = models.Diagnostic(code="CLAIM_COHORT_MISMATCH", severity="WARNING", message="different cohort")

        def res(key: str, diags: list[models.Diagnostic]) -> models.VerificationResult:
            r = self._result(key, s, [], ["HR 0.74"])
            r.sentence_checks[0].diagnostics.extend(diags)
            return r

        one_disagrees = grounding.reconcile_sentence_checks([res("10.1/a", [cohort]), res("10.1/b", [])])
        self.assertEqual([r.diagnostics for r in one_disagrees], [[], []])
        both_disagree = grounding.reconcile_sentence_checks([res("10.1/a", [cohort]), res("10.1/b", [cohort])])
        self.assertEqual(
            [[(d.code, d.severity) for d in r.diagnostics] for r in both_disagree],
            [[("CLAIM_COHORT_MISMATCH", "INFO")]] * 2,
        )
        # A sentence naming a population the paper contradicts (HFpEF vs HFrEF) stays a warning.
        conflict = models.Diagnostic(
            code="CLAIM_COHORT_MISMATCH",
            severity="WARNING",
            message="wrong cohort",
            details={"reason": "cohort_conflict"},
        )
        named = grounding.reconcile_sentence_checks([res("10.1/a", [conflict]), res("10.1/b", [conflict])])
        self.assertEqual([r.diagnostics[0].severity for r in named], ["WARNING", "WARNING"])

    def test_uncheckable_co_citation_downgrades_to_a_note(self) -> None:
        s = models.CitingSentence("line 1", "x [1,3].", "40% fewer.", ["10.1/a"], uncheckable_refs=1)
        (res,) = grounding.reconcile_sentence_checks([self._result("10.1/a", s, ["40%"], ["40%"])])
        self.assertEqual([(d.code, d.severity) for d in res.diagnostics], [("CLAIM_NUMBER_NOT_IN_ABSTRACT", "INFO")])
        self.assertEqual(res.status, "PASS")


class TestClaimColumnFromSentences(unittest.TestCase):
    def test_claim_cell_is_skipped_when_no_cited_sentence_has_numbers(self) -> None:
        s = models.CitingSentence("line 1", "SGLT2 matters [1].", "SGLT2 matters.", ["10.1/a"])
        res = models.VerificationResult(
            input=models.CitationInput(doi="10.1/a", citing_sentences=[s]),
            status="PASS",
            resolved=models.PaperRecord(doi="10.1/a", title="T"),
            sentence_checks=[models.SentenceCheck(s, models.ClaimReport(claim=s.claim), [])],
        )
        self.assertEqual(checklist.build_row(1, res).cells["claim"].verdict, "skip")
        res.sentence_checks[0].report.extracted_numbers.append("HR 0.74")
        self.assertEqual(checklist.build_row(1, res).cells["claim"].verdict, "pass")


class TestScanChecksClaimsEndToEnd(unittest.TestCase):
    def test_scan_flags_a_misquoted_hazard_ratio_in_the_manuscript(self) -> None:
        md = (
            "Dapagliflozin cut the primary outcome (HR 0.52) [1].\n"
            "It enrolled 4744 patients [1].\n\n"
            "## References\n" + DAPA_REF + "\n"
        )
        http = FakeHttpClient(
            [
                (
                    "api.crossref.org/works/10.1056/NEJMoa1911303",
                    models.HttpResponse(200, json.dumps(DAPA_HF_CROSSREF), "cr"),
                ),
                (
                    "europepmc/webservices/rest/search",
                    models.HttpResponse(200, json.dumps({"resultList": {"result": []}}), "e"),
                ),
                (
                    "esearch.fcgi",
                    models.HttpResponse(200, json.dumps({"esearchresult": {"idlist": ["31535829"]}}), "es"),
                ),
                ("efetch.fcgi?db=pubmed&id=31535829", models.HttpResponse(200, DAPA_HF_PUBMED_XML, "pm")),
            ]
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "paper.md"
            p.write_text(md, encoding="utf-8")
            out = io.StringIO()
            with mock.patch.object(rv, "HttpClient", return_value=http), mock.patch("sys.stdout", out):
                rc = rv.main(["scan", str(p), "--no-abstract"])
            json_out = io.StringIO()
            with mock.patch.object(rv, "HttpClient", return_value=http), mock.patch("sys.stdout", json_out):
                rv.main(["scan", str(p), "--json"])
        self.assertEqual(rc, 0)  # claim problems are warnings
        text = out.getvalue()
        row = next(line for line in text.splitlines() if "McMurray 2019" in line and "WARN" in line)
        self.assertRegex(row, r"(⚠|!)\s+WARN$")  # Claim column; ASCII "!" when stdout has no encoding
        self.assertIn("HR 0.52", text)
        self.assertIn("Dapagliflozin cut the primary outcome", text)
        result = json.loads(json_out.getvalue())["results"][0]
        self.assertEqual(len(result["sentence_checks"]), 2)


if __name__ == "__main__":
    unittest.main()
