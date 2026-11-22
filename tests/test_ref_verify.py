#!/usr/bin/env python3
"""Offline unit tests for ref_verify (mocked HTTP + real file scanning)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Dict, List, Optional
from unittest import mock

from ref_verify import __version__
from ref_verify import cli as rv

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

DAPA_HF_CROSSREF = {
    "message": {
        "DOI": "10.1056/NEJMoa1911303",
        "title": ["Dapagliflozin in Patients with Heart Failure and Reduced Ejection Fraction"],
        "author": [
            {"family": "McMurray", "given": "John J. V."},
            {"family": "Solomon", "given": "Scott D."},
            {"family": "Inzucchi", "given": "Silvio E."},
            {"family": "Køber", "given": "Lars"},
        ],
        "published-print": {"date-parts": [[2019, 11, 21]]},
        "published-online": {"date-parts": [[2019, 9, 19]]},
        "issued": {"date-parts": [[2019, 11, 21]]},
        "container-title": ["New England Journal of Medicine"],
        "short-container-title": ["N Engl J Med"],
    }
}

DAPA_HF_PUBMED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>31535829</PMID>
      <Article>
        <Journal>
          <Title>The New England journal of medicine</Title>
          <ISOAbbreviation>N Engl J Med</ISOAbbreviation>
          <JournalIssue>
            <PubDate><Year>2019</Year><Month>Nov</Month><Day>21</Day></PubDate>
          </JournalIssue>
        </Journal>
        <ArticleTitle>Dapagliflozin in Patients with Heart Failure and Reduced Ejection Fraction.</ArticleTitle>
        <Abstract>
          <AbstractText Label="METHODS">In this phase 3, placebo-controlled trial, we randomly assigned 4744 patients with New York Heart Association class II, III, or IV heart failure and an ejection fraction of 40% or less to receive either dapagliflozin (at a dose of 10 mg once daily) or placebo.</AbstractText>
          <AbstractText Label="RESULTS">Over a median of 18.2 months, the primary outcome occurred in 386 of 2373 patients (16.3%) in the dapagliflozin group and in 502 of 2371 patients (21.2%) in the placebo group (hazard ratio, 0.74; 95% confidence interval [CI], 0.65 to 0.85; P&lt;0.001).</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><LastName>McMurray</LastName><ForeName>John J V</ForeName><Initials>JJV</Initials></Author>
          <Author><LastName>Solomon</LastName><ForeName>Scott D</ForeName><Initials>SD</Initials></Author>
          <Author><LastName>Inzucchi</LastName><ForeName>Silvio E</ForeName><Initials>SE</Initials></Author>
          <Author><LastName>Køber</LastName><ForeName>Lars</ForeName><Initials>L</Initials></Author>
        </AuthorList>
        <PublicationTypeList>
          <PublicationType>Randomized Controlled Trial</PublicationType>
        </PublicationTypeList>
      </Article>
      <MedlineJournalInfo><MedlineTA>N Engl J Med</MedlineTA></MedlineJournalInfo>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="pubmed">31535829</ArticleId>
        <ArticleId IdType="doi">10.1056/NEJMoa1911303</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""

RETRACTED_PUBMED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>99999991</PMID>
      <Article>
        <Journal>
          <Title>The Lancet</Title>
          <JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue>
        </Journal>
        <ArticleTitle>Hydroxychloroquine or chloroquine for COVID-19: a multinational registry analysis.</ArticleTitle>
        <Abstract><AbstractText>Retrospective registry analysis of 96032 patients.</AbstractText></Abstract>
        <AuthorList>
          <Author><LastName>Mehra</LastName><ForeName>Mandeep R</ForeName><Initials>MR</Initials></Author>
        </AuthorList>
        <PublicationTypeList>
          <PublicationType>Journal Article</PublicationType>
          <PublicationType>Retracted Publication</PublicationType>
        </PublicationTypeList>
      </Article>
      <MedlineJournalInfo><MedlineTA>Lancet</MedlineTA></MedlineJournalInfo>
      <CommentsCorrectionsList>
        <CommentsCorrections RefType="RetractionIn">
          <RefSource>Lancet. 2020 Jun 13;395(10240):1820</RefSource>
          <PMID>32511943</PMID>
        </CommentsCorrections>
      </CommentsCorrectionsList>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="doi">10.1016/S0140-6736(20)31180-6</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""

OTHER_PAPER_PUBMED_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>26378978</PMID>
      <Article>
        <Journal>
          <Title>The New England journal of medicine</Title>
          <JournalIssue><PubDate><Year>2015</Year></PubDate></JournalIssue>
        </Journal>
        <ArticleTitle>Empagliflozin, Cardiovascular Outcomes, and Mortality in Type 2 Diabetes.</ArticleTitle>
        <AuthorList>
          <Author><LastName>Zinman</LastName><ForeName>Bernard</ForeName><Initials>B</Initials></Author>
        </AuthorList>
        <PublicationTypeList><PublicationType>Randomized Controlled Trial</PublicationType></PublicationTypeList>
      </Article>
      <MedlineJournalInfo><MedlineTA>N Engl J Med</MedlineTA></MedlineJournalInfo>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="doi">10.1056/NEJMoa1504720</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


class FakeHttpClient(rv.HttpClient):
    """Deterministic HTTP mock keyed by substring rules."""

    def __init__(self, routes: List[tuple[str, rv.HttpResponse]]) -> None:
        super().__init__(timeout=5.0, retries=0)
        self.routes = routes
        self.calls: List[str] = []

    def get(self, url: str, headers: Optional[Dict[str, str]] = None) -> rv.HttpResponse:
        self.calls.append(url)
        for pattern, resp in self.routes:
            if pattern in url:
                return resp
        return rv.HttpResponse(status=404, body="", url=url, error="HTTP 404 Not Found")


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


class TestPackageMetadata(unittest.TestCase):
    def test_package_version_matches_cli(self) -> None:
        self.assertEqual(__version__, "0.1.0")
        self.assertEqual(rv.__version__, __version__)

    def test_cli_version_flag(self) -> None:
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            with self.assertRaises(SystemExit) as ctx:
                rv.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(buf.getvalue().strip(), f"ref-verify {__version__}")


class TestSyntaxValidation(unittest.TestCase):
    def test_valid_doi_variants(self) -> None:
        for raw, expected in [
            ("10.1056/NEJMoa1911303", "10.1056/NEJMoa1911303"),
            ("https://doi.org/10.1056/NEJMoa1911303", "10.1056/NEJMoa1911303"),
            ("doi: 10.1016/S0140-6736(20)31180-6", "10.1016/S0140-6736(20)31180-6"),
            (
                "10.1002/(SICI)1097-0142(19991115)86:10<1992::AID-CNCR16>3.0.CO;2-4",
                "10.1002/(SICI)1097-0142(19991115)86:10<1992::AID-CNCR16>3.0.CO;2-4",
            ),
        ]:
            clean, diag = rv.validate_doi(raw)
            self.assertIsNone(diag, f"Unexpected error for {raw}: {diag}")
            self.assertEqual(clean, expected)

    def test_doi_trailing_punctuation_and_missing_prefix(self) -> None:
        clean, diag = rv.validate_doi("10.1056/NEJMoa1911303.")
        self.assertIsNotNone(diag)
        assert diag is not None
        self.assertEqual(diag.code, "DOI_SYNTAX_INVALID")
        self.assertEqual(clean, "10.1056/NEJMoa1911303")
        self.assertEqual(diag.details["cleaned_candidate"], "10.1056/NEJMoa1911303")
        self.assertIn("trailing punctuation", diag.message)

        clean2, diag2 = rv.validate_doi("1056/NEJMoa1911303")
        self.assertIsNotNone(diag2)
        assert diag2 is not None
        self.assertEqual(diag2.code, "DOI_SYNTAX_INVALID")
        self.assertEqual(clean2, "10.1056/NEJMoa1911303")
        self.assertIn("missing '10.' prefix", diag2.message)

        clean3, diag3 = rv.validate_doi("10.1056/NEJMoa1911303)")
        self.assertIsNotNone(diag3)
        self.assertEqual(clean3, "10.1056/NEJMoa1911303")

        clean4, diag4 = rv.validate_doi("not-a-doi")
        self.assertIsNotNone(diag4)
        self.assertIsNone(clean4)

    def test_pmid_validation(self) -> None:
        clean, diag = rv.validate_pmid("PMID: 31535829")
        self.assertIsNone(diag)
        self.assertEqual(clean, "31535829")

        clean2, diag2 = rv.validate_pmid("https://pubmed.ncbi.nlm.nih.gov/31535829/")
        self.assertIsNone(diag2)
        self.assertEqual(clean2, "31535829")

        clean3, diag3 = rv.validate_pmid("PMC123456")
        self.assertIsNotNone(diag3)
        assert diag3 is not None
        self.assertEqual(diag3.code, "PMID_SYNTAX_INVALID")
        self.assertEqual(clean3, "123456")


class TestMetadataDiagnostics(unittest.TestCase):
    def test_title_mismatch_truncation_vs_different_paper(self) -> None:
        official = "Dapagliflozin in Patients with Heart Failure and Reduced Ejection Fraction"
        self.assertIsNone(rv.check_title(official, official))

        # Subtitle / prefix truncation -> WARNING
        d_trunc = rv.check_title("Dapagliflozin in Patients with Heart Failure", official)
        self.assertIsNotNone(d_trunc)
        assert d_trunc is not None
        self.assertEqual(d_trunc.code, "TITLE_MISMATCH")
        self.assertEqual(d_trunc.severity, "WARNING")
        self.assertTrue(d_trunc.details["truncation"])
        self.assertIn("reduced", d_trunc.details["missing_tokens_from_cited"])

        # Completely wrong paper -> ERROR
        d_wrong = rv.check_title("Metformin for Prevention of Type 2 Diabetes in Adults", official)
        self.assertIsNotNone(d_wrong)
        assert d_wrong is not None
        self.assertEqual(d_wrong.code, "TITLE_MISMATCH")
        self.assertEqual(d_wrong.severity, "ERROR")
        self.assertLess(d_wrong.details["similarity"], 0.65)
        self.assertIn("metformin", d_wrong.details["extra_tokens_in_cited"])

    def test_author_mismatch_first_middle_absent(self) -> None:
        authors = [
            rv.Author(family="McMurray", given="John J. V.", initials="JJV"),
            rv.Author(family="Solomon", given="Scott D.", initials="SD"),
            rv.Author(family="Køber", given="Lars", initials="L"),
        ]
        # Clean matches across formats
        self.assertIsNone(rv.check_author("McMurray", authors))
        self.assertIsNone(rv.check_author("McMurray JJV", authors))
        self.assertIsNone(rv.check_author("John McMurray", authors))
        self.assertIsNone(rv.check_author("McMurray, John", authors))
        self.assertIsNone(rv.check_author("McMurray et al.", authors))

        # Middle author -> WARNING noting exact position
        d_mid = rv.check_author("Solomon", authors)
        self.assertIsNotNone(d_mid)
        assert d_mid is not None
        self.assertEqual(d_mid.code, "AUTHOR_MISMATCH")
        self.assertEqual(d_mid.severity, "WARNING")
        self.assertEqual(d_mid.details["matched_position"], 2)
        self.assertIn("McMurray JJV", d_mid.actual)

        # Absent author -> ERROR
        d_abs = rv.check_author("Smith", authors)
        self.assertIsNotNone(d_abs)
        assert d_abs is not None
        self.assertEqual(d_abs.code, "AUTHOR_MISMATCH")
        self.assertEqual(d_abs.severity, "ERROR")
        self.assertIsNone(d_abs.details["matched_position"])
        self.assertIn("McMurray JJV", d_abs.actual)

    def test_year_mismatch_epub_vs_genuine(self) -> None:
        rec = rv.PaperRecord(published_print=2021, published_online=2020)
        # Either print or online passes cleanly
        self.assertIsNone(rv.check_year("2021", rec))
        self.assertIsNone(rv.check_year("2020", rec))

        # ±1 yr from known dates -> WARNING
        d_epub = rv.check_year("2022", rec)
        self.assertIsNotNone(d_epub)
        assert d_epub is not None
        self.assertEqual(d_epub.code, "YEAR_MISMATCH")
        self.assertEqual(d_epub.severity, "WARNING")
        self.assertTrue(d_epub.details["likely_epub_offset"])

        # ≥2 yr diff -> ERROR
        d_err = rv.check_year("2024", rec)
        self.assertIsNotNone(d_err)
        assert d_err is not None
        self.assertEqual(d_err.code, "YEAR_MISMATCH")
        self.assertEqual(d_err.severity, "ERROR")
        self.assertFalse(d_err.details["likely_epub_offset"])
        self.assertIn("published_print=2021", d_err.actual)
        self.assertIn("published_online=2020", d_err.actual)

    def test_journal_matching_and_mismatch(self) -> None:
        rec = rv.PaperRecord(journal_full="New England Journal of Medicine", journal_abbrev="N Engl J Med")
        self.assertIsNone(rv.check_journal("NEJM", rec))
        self.assertIsNone(rv.check_journal("N. Engl. J. Med.", rec))
        self.assertIsNone(rv.check_journal("The New England Journal of Medicine", rec))

        d_j = rv.check_journal("The Lancet", rec)
        self.assertIsNotNone(d_j)
        assert d_j is not None
        self.assertEqual(d_j.code, "JOURNAL_MISMATCH")
        self.assertEqual(d_j.severity, "ERROR")


class TestClaimValidityPack(unittest.TestCase):
    def test_number_extraction_and_matching(self) -> None:
        abstract = (
            "METHODS: We randomly assigned 4744 patients with heart failure and EF <= 40% to dapagliflozin 10 mg daily or placebo. "
            "RESULTS: Over a median of 18.2 months, the primary outcome occurred in 16.3% vs 21.2% "
            "(hazard ratio, 0.74; 95% confidence interval [CI], 0.65 to 0.85; P<0.001)."
        )
        rec = rv.PaperRecord(abstract=abstract)

        good_claim = (
            "In DAPA-HF (N=4,744), dapagliflozin 10 mg reduced the primary outcome "
            "(16.3% vs 21.2%; HR 0.74, 95% CI 0.65-0.85, p < 0.001) over 18.2 months."
        )
        rep, diags = rv.evaluate_claim(good_claim, rec)
        self.assertEqual(rep.missing_numbers, [], f"Expected zero missing numbers, got {rep.missing_numbers}")
        self.assertEqual([d.code for d in diags], [])
        self.assertTrue(len(rep.matched_sentences) >= 1)

        bad_claim = "In DAPA-HF (N=9,999), dapagliflozin cut mortality by 52% (HR 0.48, 95% CI 0.30-0.60, p = 0.04)."
        rep_bad, diags_bad = rv.evaluate_claim(bad_claim, rec)
        self.assertIn("CLAIM_NUMBER_NOT_IN_ABSTRACT", [d.code for d in diags_bad])
        self.assertTrue(any("0.48" in m for m in rep_bad.missing_numbers))
        self.assertTrue(any("9,999" in m for m in rep_bad.missing_numbers))
        self.assertTrue(len(rep_bad.abstract_numbers) > 0)

    def test_no_partial_digit_false_positive(self) -> None:
        # "16%" should NOT match "16.3%" and "7" should NOT match "0.74"
        self.assertFalse(rv.quant_token_in_text("16%", "Occurred in 16.3% of patients."))
        self.assertFalse(rv.quant_token_in_text("HR 7", "Hazard ratio was 0.74."))
        self.assertTrue(rv.quant_token_in_text("HR 0.74", "Hazard ratio was .74 in the main analysis."))

    def test_spelled_out_hazard_ratio_and_ci_extracted_from_abstract(self) -> None:
        abstract = "Primary outcome (hazard ratio, 0.74; 95% confidence interval [CI], 0.65 to 0.85; P<0.001)."
        nums = rv.extract_abstract_numbers(abstract)
        self.assertTrue(any("0.74" in n for n in nums), f"Expected 0.74 in {nums}")
        self.assertTrue(any("0.65" in n and "0.85" in n for n in nums), f"Expected CI 0.65-0.85 in {nums}")


class TestEndToEndMockedVerification(unittest.TestCase):
    def test_full_pass_citation(self) -> None:
        http = FakeHttpClient(
            [
                (
                    "api.crossref.org/works/10.1056/NEJMoa1911303",
                    rv.HttpResponse(200, json.dumps(DAPA_HF_CROSSREF), "cr"),
                ),
                (
                    "europepmc/webservices/rest/search",
                    rv.HttpResponse(200, json.dumps({"resultList": {"result": []}}), "epmc"),
                ),
                ("efetch.fcgi?db=pubmed&id=31535829", rv.HttpResponse(200, DAPA_HF_PUBMED_XML, "pm")),
            ]
        )
        cit = rv.CitationInput(
            doi="10.1056/NEJMoa1911303",
            pmid="31535829",
            title="Dapagliflozin in Patients with Heart Failure and Reduced Ejection Fraction",
            author="McMurray JJV",
            year="2019",
            journal="NEJM",
            claim="Dapagliflozin 10 mg in 4,744 patients reduced the primary outcome (16.3% vs 21.2%, HR 0.74, 95% CI 0.65-0.85, p<0.001).",
        )
        res = rv.verify_citation(cit, http)
        self.assertEqual(res.status, "PASS")
        self.assertEqual(res.diagnostics, [])
        txt = rv.format_result_text(res)
        self.assertIn("STATUS: PASS", txt)
        self.assertIn("CLAIM_CHECK:", txt)
        self.assertIn("OFFICIAL ABSTRACT:", txt)

    def test_pmid_doi_conflict(self) -> None:
        http = FakeHttpClient(
            [
                (
                    "api.crossref.org/works/10.1056/NEJMoa1911303",
                    rv.HttpResponse(200, json.dumps(DAPA_HF_CROSSREF), "cr"),
                ),
                (
                    "europepmc/webservices/rest/search",
                    rv.HttpResponse(200, json.dumps({"resultList": {"result": []}}), "epmc"),
                ),
                ("efetch.fcgi?db=pubmed&id=26378978", rv.HttpResponse(200, OTHER_PAPER_PUBMED_XML, "pm")),
            ]
        )
        cit = rv.CitationInput(doi="10.1056/NEJMoa1911303", pmid="26378978")
        res = rv.verify_citation(cit, http)
        self.assertEqual(res.status, "FAIL")
        codes = [d.code for d in res.diagnostics]
        self.assertIn("PMID_DOI_CONFLICT", codes)
        conflict = next(d for d in res.diagnostics if d.code == "PMID_DOI_CONFLICT")
        self.assertEqual(conflict.severity, "CRITICAL")
        self.assertIn("10.1056/NEJMoa1504720", conflict.message)
        self.assertIn("10.1056/NEJMoa1911303", conflict.message)

    def test_retracted_paper_critical(self) -> None:
        http = FakeHttpClient(
            [
                ("efetch.fcgi?db=pubmed&id=99999991", rv.HttpResponse(200, RETRACTED_PUBMED_XML, "pm")),
                (
                    "europepmc/webservices/rest/search",
                    rv.HttpResponse(200, json.dumps({"resultList": {"result": []}}), "epmc"),
                ),
                ("api.crossref.org/works/", rv.HttpResponse(404, "", "cr", "HTTP 404 Not Found")),
            ]
        )
        res = rv.verify_citation(rv.CitationInput(pmid="99999991"), http)
        self.assertEqual(res.status, "FAIL")
        codes = [d.code for d in res.diagnostics]
        self.assertIn("RETRACTED_PAPER", codes)
        ret = next(d for d in res.diagnostics if d.code == "RETRACTED_PAPER")
        self.assertEqual(ret.severity, "CRITICAL")
        self.assertIn("32511943", ret.actual)

    def test_doi_not_found_with_did_you_mean(self) -> None:
        letter_same_title = {
            "DOI": "10.1056/NEJMc1917241",
            "title": ["Dapagliflozin in Patients with Heart Failure and Reduced Ejection Fraction"],
            "issued": {"date-parts": [[2020]]},
            "is-referenced-by-count": 4,
        }
        primary_paper = dict(DAPA_HF_CROSSREF["message"])
        primary_paper["is-referenced-by-count"] = 6500
        search_payload = {
            "message": {
                "items": [letter_same_title, primary_paper],
            }
        }
        http = FakeHttpClient(
            [
                ("api.crossref.org/works/10.1056/FAKE99999", rv.HttpResponse(404, "", "cr", "HTTP 404 Not Found")),
                ("api.crossref.org/works?query.title=", rv.HttpResponse(200, json.dumps(search_payload), "cr-search")),
                (
                    "europepmc/webservices/rest/search",
                    rv.HttpResponse(200, json.dumps({"resultList": {"result": []}}), "epmc"),
                ),
                ("esearch.fcgi", rv.HttpResponse(200, json.dumps({"esearchresult": {"idlist": []}}), "pm-search")),
            ]
        )
        cit = rv.CitationInput(
            doi="10.1056/FAKE99999",
            title="Dapagliflozin in Patients with Heart Failure and Reduced Ejection Fraction",
        )
        res = rv.verify_citation(cit, http)
        self.assertEqual(res.status, "FAIL")
        self.assertIn("DOI_NOT_FOUND", [d.code for d in res.diagnostics])
        self.assertTrue(len(res.did_you_mean) >= 1)
        # Primary paper with author + high citations ranks #1 above unauthored letter with same title
        self.assertEqual(res.did_you_mean[0]["doi"], "10.1056/NEJMoa1911303")
        txt = rv.format_result_text(res)
        self.assertIn("DID_YOU_MEAN", txt)

    def test_oa_fulltext_fallback_matches_claim_number(self) -> None:
        fulltext_xml = """<article><body><p>In the prespecified subgroup of patients with diabetes, the hazard ratio was 0.75 (95% CI, 0.63 to 0.90) with 1983 diabetic participants enrolled.</p></body></article>"""
        epmc_json = {
            "resultList": {
                "result": [
                    {
                        "doi": "10.1056/NEJMoa1911303",
                        "pmid": "31535829",
                        "pmcid": "PMC7654321",
                        "isOpenAccess": "Y",
                        "title": "Dapagliflozin in Patients with Heart Failure and Reduced Ejection Fraction",
                    }
                ]
            }
        }
        http = FakeHttpClient(
            [
                (
                    "api.crossref.org/works/10.1056/NEJMoa1911303",
                    rv.HttpResponse(200, json.dumps(DAPA_HF_CROSSREF), "cr"),
                ),
                ("europepmc/webservices/rest/search", rv.HttpResponse(200, json.dumps(epmc_json), "epmc")),
                ("efetch.fcgi?db=pubmed&id=31535829", rv.HttpResponse(200, DAPA_HF_PUBMED_XML, "pm")),
                ("PMC7654321/fullTextXML", rv.HttpResponse(200, fulltext_xml, "ft")),
            ]
        )
        cit = rv.CitationInput(
            doi="10.1056/NEJMoa1911303",
            claim="In the diabetes subgroup (N=1983), HR was 0.75 (95% CI 0.63-0.90).",
            fulltext=True,
        )
        res = rv.verify_citation(cit, http)
        self.assertEqual(res.status, "PASS")
        assert res.claim_report is not None
        self.assertEqual(res.claim_report.missing_numbers, [])
        self.assertTrue(len(res.claim_report.fulltext_excerpts) >= 1)


class TestDocumentScanning(unittest.TestCase):
    def test_markdown_scan_pairs_colocated_doi_pmid_and_deduplicates(self) -> None:
        md = (
            "# References\n"
            "1. McMurray et al. (2019). doi:10.1056/NEJMoa1911303. PMID: 31535829.\n"
            "2. Repeated citation (https://doi.org/10.1056/NEJMoa1911303).\n"
            "3. Standalone PubMed link: https://pubmed.ncbi.nlm.nih.gov/26378978/\n"
            "4. Balanced parens DOI: 10.1016/S0140-6736(20)31180-6.\n"
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "paper.md"
            p.write_text(md, encoding="utf-8")
            segs = rv.extract_text_segments(p)
            cits = rv.extract_citations_from_segments(segs)
            self.assertEqual(len(cits), 3)
            self.assertEqual(cits[0].doi, "10.1056/NEJMoa1911303")
            self.assertEqual(cits[0].pmid, "31535829")
            self.assertIsNone(cits[1].doi)
            self.assertEqual(cits[1].pmid, "26378978")
            self.assertEqual(cits[2].doi, "10.1016/S0140-6736(20)31180-6")

    def test_docx_scan_joins_split_xml_runs(self) -> None:
        doc_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body>"
            "<w:p><w:r><w:t>See 10.1056/</w:t></w:r><w:r><w:t>NEJMoa1911303.</w:t></w:r></w:p>"
            "</w:body></w:document>"
        )
        foot_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:footnote><w:p><w:r><w:t>Also PMID: 26378978</w:t></w:r></w:p></w:footnote>"
            "</w:footnotes>"
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "manuscript.docx"
            with zipfile.ZipFile(p, "w") as zf:
                zf.writestr("word/document.xml", doc_xml)
                zf.writestr("word/footnotes.xml", foot_xml)
            segs = rv.extract_text_segments(p)
            cits = rv.extract_citations_from_segments(segs)
            self.assertEqual(len(cits), 2)
            self.assertEqual(cits[0].doi, "10.1056/NEJMoa1911303")
            self.assertEqual(cits[1].pmid, "26378978")

    def test_pptx_scan_slides_and_notes(self) -> None:
        slide1 = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            "<p:cSld><p:spTree><p:sp><p:txBody>"
            "<a:p><a:r><a:t>DAPA-HF Trial (10.1056/NEJMoa1911303)</a:t></a:r></a:p>"
            "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
        )
        notes1 = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<p:notes xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            "<p:cSld><p:spTree><p:sp><p:txBody>"
            "<a:p><a:r><a:t>Speaker note: compare with PMID 26378978</a:t></a:r></a:p>"
            "</p:txBody></p:sp></p:spTree></p:cSld></p:notes>"
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "journal_club.pptx"
            with zipfile.ZipFile(p, "w") as zf:
                zf.writestr("ppt/slides/slide1.xml", slide1)
                zf.writestr("ppt/notesSlides/notesSlide1.xml", notes1)
            segs = rv.extract_text_segments(p)
            cits = rv.extract_citations_from_segments(segs)
            self.assertEqual(len(cits), 2)
            self.assertEqual(cits[0].location, "Slide 1")
            self.assertEqual(cits[0].doi, "10.1056/NEJMoa1911303")
            self.assertEqual(cits[1].location, "Slide 1 notes")
            self.assertEqual(cits[1].pmid, "26378978")


class TestBatchCLI(unittest.TestCase):
    def test_batch_json_output_and_exit_code(self) -> None:
        batch_data = [
            {"id": "dapa", "doi": "10.1056/NEJMoa1911303", "year": 2019, "author": "McMurray"},
            {"id": "bad", "doi": "10.1056/NEJMoa1911303.", "year": 2024},
        ]
        fake_http = FakeHttpClient(
            [
                (
                    "api.crossref.org/works/10.1056/NEJMoa1911303",
                    rv.HttpResponse(200, json.dumps(DAPA_HF_CROSSREF), "cr"),
                ),
                (
                    "europepmc/webservices/rest/search",
                    rv.HttpResponse(200, json.dumps({"resultList": {"result": []}}), "epmc"),
                ),
                ("esearch.fcgi", rv.HttpResponse(200, json.dumps({"esearchresult": {"idlist": []}}), "es")),
            ]
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "refs.json"
            p.write_text(json.dumps(batch_data), encoding="utf-8")
            buf = io.StringIO()
            with mock.patch.object(rv, "HttpClient", return_value=fake_http), mock.patch("sys.stdout", buf):
                rc = rv.main(["batch", str(p), "--json"])
            self.assertEqual(rc, 1)
            parsed = json.loads(buf.getvalue())
            self.assertEqual(parsed["summary"]["total"], 2)
            self.assertEqual(parsed["summary"]["pass"], 1)
            self.assertEqual(parsed["summary"]["fail"], 1)


if __name__ == "__main__":
    unittest.main()
