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

from ref_verify import __version__, claims, http_client, identifiers, metadata, models, quant, report, scan, verify
from ref_verify import cli as rv
from ref_verify.sources import fulltext, pubmed

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


DAPA_HF_SUBGROUP_PUBMED_XML = """<?xml version="1.0" encoding="UTF-8"?>
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
          <AbstractText Label="RESULTS">Over a median of 18.2 months, the primary outcome occurred in 386 of 2373 patients (16.3%) in the dapagliflozin group and in 502 of 2371 patients (21.2%) in the placebo group (hazard ratio, 0.74; 95% confidence interval [CI], 0.65 to 0.85; P&lt;0.001). In the prespecified subgroup of patients with diabetes (n=1983), the primary outcome occurred in 20.0% vs 25.5% (hazard ratio, 0.75; 95% CI, 0.63 to 0.90), whereas among patients without diabetes (n=2761), the primary outcome occurred in 13.2% vs 17.7% (hazard ratio, 0.73; 95% CI, 0.60 to 0.88).</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><LastName>McMurray</LastName><ForeName>John J V</ForeName><Initials>JJV</Initials></Author>
          <Author><LastName>Solomon</LastName><ForeName>Scott D</ForeName><Initials>SD</Initials></Author>
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


class FakeHttpClient(http_client.HttpClient):
    """Deterministic HTTP mock keyed by substring rules."""

    def __init__(self, routes: List[tuple[str, http_client.HttpResponse]]) -> None:
        super().__init__(timeout=5.0, retries=0)
        self.routes = routes
        self.calls: List[str] = []

    def get(self, url: str, headers: Optional[Dict[str, str]] = None) -> http_client.HttpResponse:
        self.calls.append(url)
        for pattern, resp in self.routes:
            if pattern in url:
                return resp
        return http_client.HttpResponse(status=404, body="", url=url, error="HTTP 404 Not Found")


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


class TestPackageMetadata(unittest.TestCase):
    def test_package_version_matches_cli(self) -> None:
        self.assertEqual(__version__, "0.1.0")
        self.assertEqual(__version__, __version__)

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
            clean, diag = identifiers.validate_doi(raw)
            self.assertIsNone(diag, f"Unexpected error for {raw}: {diag}")
            self.assertEqual(clean, expected)

    def test_doi_trailing_punctuation_and_missing_prefix(self) -> None:
        clean, diag = identifiers.validate_doi("10.1056/NEJMoa1911303.")
        self.assertIsNotNone(diag)
        assert diag is not None
        self.assertEqual(diag.code, "DOI_SYNTAX_INVALID")
        self.assertEqual(clean, "10.1056/NEJMoa1911303")
        self.assertEqual(diag.details["cleaned_candidate"], "10.1056/NEJMoa1911303")
        self.assertIn("trailing punctuation", diag.message)

        clean2, diag2 = identifiers.validate_doi("1056/NEJMoa1911303")
        self.assertIsNotNone(diag2)
        assert diag2 is not None
        self.assertEqual(diag2.code, "DOI_SYNTAX_INVALID")
        self.assertEqual(clean2, "10.1056/NEJMoa1911303")
        self.assertIn("missing '10.' prefix", diag2.message)

        clean3, diag3 = identifiers.validate_doi("10.1056/NEJMoa1911303)")
        self.assertIsNotNone(diag3)
        self.assertEqual(clean3, "10.1056/NEJMoa1911303")

        clean4, diag4 = identifiers.validate_doi("not-a-doi")
        self.assertIsNotNone(diag4)
        self.assertIsNone(clean4)

    def test_pmid_validation(self) -> None:
        clean, diag = identifiers.validate_pmid("PMID: 31535829")
        self.assertIsNone(diag)
        self.assertEqual(clean, "31535829")

        clean2, diag2 = identifiers.validate_pmid("https://pubmed.ncbi.nlm.nih.gov/31535829/")
        self.assertIsNone(diag2)
        self.assertEqual(clean2, "31535829")

        clean3, diag3 = identifiers.validate_pmid("PMC123456")
        self.assertIsNotNone(diag3)
        assert diag3 is not None
        self.assertEqual(diag3.code, "PMID_SYNTAX_INVALID")
        self.assertEqual(clean3, "123456")


class TestMetadataDiagnostics(unittest.TestCase):
    def test_title_mismatch_truncation_vs_different_paper(self) -> None:
        official = "Dapagliflozin in Patients with Heart Failure and Reduced Ejection Fraction"
        self.assertIsNone(metadata.check_title(official, official))

        # Subtitle / prefix truncation -> WARNING
        d_trunc = metadata.check_title("Dapagliflozin in Patients with Heart Failure", official)
        self.assertIsNotNone(d_trunc)
        assert d_trunc is not None
        self.assertEqual(d_trunc.code, "TITLE_MISMATCH")
        self.assertEqual(d_trunc.severity, "WARNING")
        self.assertTrue(d_trunc.details["truncation"])
        self.assertIn("reduced", d_trunc.details["missing_tokens_from_cited"])

        # Completely wrong paper -> ERROR
        d_wrong = metadata.check_title("Metformin for Prevention of Type 2 Diabetes in Adults", official)
        self.assertIsNotNone(d_wrong)
        assert d_wrong is not None
        self.assertEqual(d_wrong.code, "TITLE_MISMATCH")
        self.assertEqual(d_wrong.severity, "ERROR")
        self.assertLess(d_wrong.details["similarity"], 0.65)
        self.assertIn("metformin", d_wrong.details["extra_tokens_in_cited"])

    def test_author_mismatch_first_middle_absent(self) -> None:
        authors = [
            models.Author(family="McMurray", given="John J. V.", initials="JJV"),
            models.Author(family="Solomon", given="Scott D.", initials="SD"),
            models.Author(family="Køber", given="Lars", initials="L"),
        ]
        # Clean matches across formats
        self.assertIsNone(metadata.check_author("McMurray", authors))
        self.assertIsNone(metadata.check_author("McMurray JJV", authors))
        self.assertIsNone(metadata.check_author("John McMurray", authors))
        self.assertIsNone(metadata.check_author("McMurray, John", authors))
        self.assertIsNone(metadata.check_author("McMurray et al.", authors))

        # Middle author -> WARNING noting exact position
        d_mid = metadata.check_author("Solomon", authors)
        self.assertIsNotNone(d_mid)
        assert d_mid is not None
        self.assertEqual(d_mid.code, "AUTHOR_MISMATCH")
        self.assertEqual(d_mid.severity, "WARNING")
        self.assertEqual(d_mid.details["matched_position"], 2)
        self.assertIn("McMurray JJV", d_mid.actual)

        # Absent author -> ERROR
        d_abs = metadata.check_author("Smith", authors)
        self.assertIsNotNone(d_abs)
        assert d_abs is not None
        self.assertEqual(d_abs.code, "AUTHOR_MISMATCH")
        self.assertEqual(d_abs.severity, "ERROR")
        self.assertIsNone(d_abs.details["matched_position"])
        self.assertIn("McMurray JJV", d_abs.actual)

    def test_year_mismatch_epub_vs_genuine(self) -> None:
        rec = models.PaperRecord(published_print=2021, published_online=2020)
        # Either print or online passes cleanly
        self.assertIsNone(metadata.check_year("2021", rec))
        self.assertIsNone(metadata.check_year("2020", rec))

        # ±1 yr from known dates -> WARNING
        d_epub = metadata.check_year("2022", rec)
        self.assertIsNotNone(d_epub)
        assert d_epub is not None
        self.assertEqual(d_epub.code, "YEAR_MISMATCH")
        self.assertEqual(d_epub.severity, "WARNING")
        self.assertTrue(d_epub.details["likely_epub_offset"])

        # ≥2 yr diff -> ERROR
        d_err = metadata.check_year("2024", rec)
        self.assertIsNotNone(d_err)
        assert d_err is not None
        self.assertEqual(d_err.code, "YEAR_MISMATCH")
        self.assertEqual(d_err.severity, "ERROR")
        self.assertFalse(d_err.details["likely_epub_offset"])
        self.assertIn("published_print=2021", d_err.actual)
        self.assertIn("published_online=2020", d_err.actual)

    def test_journal_matching_and_mismatch(self) -> None:
        rec = models.PaperRecord(journal_full="New England Journal of Medicine", journal_abbrev="N Engl J Med")
        self.assertIsNone(metadata.check_journal("NEJM", rec))
        self.assertIsNone(metadata.check_journal("N. Engl. J. Med.", rec))
        self.assertIsNone(metadata.check_journal("The New England Journal of Medicine", rec))

        d_j = metadata.check_journal("The Lancet", rec)
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
        rec = models.PaperRecord(abstract=abstract)

        good_claim = (
            "In DAPA-HF (N=4,744), dapagliflozin 10 mg reduced the primary outcome "
            "(16.3% vs 21.2%; HR 0.74, 95% CI 0.65-0.85, p < 0.001) over 18.2 months."
        )
        rep, diags = claims.evaluate_claim(good_claim, rec)
        self.assertEqual(rep.missing_numbers, [], f"Expected zero missing numbers, got {rep.missing_numbers}")
        self.assertEqual([d.code for d in diags], [])
        self.assertTrue(len(rep.matched_sentences) >= 1)

        bad_claim = "In DAPA-HF (N=9,999), dapagliflozin cut mortality by 52% (HR 0.48, 95% CI 0.30-0.60, p = 0.04)."
        rep_bad, diags_bad = claims.evaluate_claim(bad_claim, rec)
        self.assertIn("CLAIM_NUMBER_NOT_IN_ABSTRACT", [d.code for d in diags_bad])
        self.assertTrue(any("0.48" in m for m in rep_bad.missing_numbers))
        self.assertTrue(any("9,999" in m for m in rep_bad.missing_numbers))
        self.assertTrue(len(rep_bad.abstract_numbers) > 0)

    def test_no_partial_digit_false_positive(self) -> None:
        # "16%" should NOT match "16.3%" and "7" should NOT match "0.74"
        self.assertFalse(quant.quant_token_in_text("16%", "Occurred in 16.3% of patients."))
        self.assertFalse(quant.quant_token_in_text("HR 7", "Hazard ratio was 0.74."))
        self.assertTrue(quant.quant_token_in_text("HR 0.74", "Hazard ratio was .74 in the main analysis."))

    def test_percentage_does_not_match_unrelated_integers_or_identifiers(self) -> None:
        # Regression for refs 4, 12, 15 + code-review edge case: comma/conjunction after bare integers
        text = (
            "In phase 3, 22.6% of patients in group 1 and 50% of oral JAK1 recipients had "
            "1-year prevalence ranging from 0.96% to 22.6% and rates of 0.5-1.3%."
        )
        self.assertFalse(quant.quant_token_in_text("3%", text))
        self.assertFalse(quant.quant_token_in_text("1%", text))
        self.assertTrue(quant.quant_token_in_text("0.96%", text))
        self.assertTrue(quant.quant_token_in_text("0.5%", text))
        # Paired percentage comparison without % on first value still matches
        self.assertTrue(
            quant.quant_token_in_text("16.3%", "Primary outcome occurred in 16.3 versus 21.2% of patients.")
        )
        self.assertTrue(quant.quant_token_in_text("0.96%", "Prevalence ranged from 0.96 to 22.6% in children."))

    def test_sentence_ending_numbers_extracted_and_matched(self) -> None:
        # Regression for refs 19 & 21: trailing period must not drop numbers at sentence end
        nums_easi = quant.extract_claim_numbers("EASI combines area and intensity for a score from 0 to 72.")
        self.assertIn("0", nums_easi)
        self.assertIn("72", nums_easi)
        nums_cdlqi = quant.extract_claim_numbers(
            "CDLQI is a 10-item scale scored 0 to 3 with a final score from 0 to 30."
        )
        self.assertEqual(nums_cdlqi, ["10", "0", "3", "30"])
        self.assertTrue(quant.quant_token_in_text("30", "The maximum CDLQI score is 30."))
        self.assertFalse(quant.quant_token_in_text("16", "The mean score was 16.3."))

    def test_middle_dot_decimals_normalized_for_extraction_and_matching(self) -> None:
        # Regression for BJD/Lancet abstracts (refs 6, 20, 24) using U+00B7 and U+2219 middle-dot decimals
        abstract = (
            "The mean percentage CFB at week 4 in EASI score was significantly greater "
            "(P < 0·001) for tofacitinib (-81·7%) vs. vehicle (-29·9%), with DLQI 5·41 ± 5∙20."
        )
        nums = quant.extract_abstract_numbers(abstract)
        self.assertIn("-81.7%", nums)
        self.assertIn("-29.9%", nums)
        self.assertIn("5.41", nums)
        self.assertIn("5.20", nums)
        self.assertTrue(any("0.001" in n for n in nums), f"Expected p-value 0.001 in {nums}")
        self.assertNotIn("001", nums)
        self.assertTrue(quant.quant_token_in_text("81.7%", abstract))
        self.assertTrue(quant.quant_token_in_text("p < 0.001", abstract))
        self.assertTrue(quant.quant_token_in_text("5.20", abstract))

    def test_spelled_out_hazard_ratio_and_ci_extracted_from_abstract(self) -> None:
        abstract = "Primary outcome (hazard ratio, 0.74; 95% confidence interval [CI], 0.65 to 0.85; P<0.001)."
        nums = quant.extract_abstract_numbers(abstract)
        self.assertTrue(any("0.74" in n for n in nums), f"Expected 0.74 in {nums}")
        self.assertTrue(any("0.65" in n and "0.85" in n for n in nums), f"Expected CI 0.65-0.85 in {nums}")

    def test_cohort_mismatch_without_literal_subgroup_word_and_edge_cases(self) -> None:
        # Abstract contrasts subgroups (women vs men) without using the word "subgroup"
        sex_rec = models.PaperRecord(
            title="Drug X in Adults with Chronic Inflammatory Disease",
            abstract=(
                "METHODS: We enrolled 800 adults with chronic inflammatory disease. "
                "RESULTS: Overall response was 34.0% vs 28.5% (P=0.04). "
                "In women, response was 41.2% vs 26.1% (P<0.001), whereas in men it was 28.9% vs 30.2% (P=0.68)."
            ),
        )
        # Wrong sex subgroup (men claimed for women's 41.2%)
        rep_bad, diags_bad = claims.evaluate_claim("In men, response was 41.2% vs 26.1%.", sex_rec)
        self.assertEqual(rep_bad.missing_numbers, [])
        self.assertIn("CLAIM_COHORT_MISMATCH", [d.code for d in diags_bad])

        # Overall attributed to women-only figure
        _, diags_overall = claims.evaluate_claim("Overall, response was 41.2% vs 26.1%.", sex_rec)
        self.assertIn("CLAIM_COHORT_MISMATCH", [d.code for d in diags_overall])

        # Correctly scoped to women -> PASS
        _, diags_ok = claims.evaluate_claim("In women, response was 41.2% vs 26.1%.", sex_rec)
        self.assertEqual(diags_ok, [])

        # Age threshold swap (<65 vs >=65)
        age_rec = models.PaperRecord(
            title="Antihypertensive Therapy in Adults with Hypertension",
            abstract=(
                "METHODS: We enrolled 1200 adults with hypertension. "
                "RESULTS: Systolic blood pressure fell by 8.4 mmHg overall. "
                "In patients aged >=65 years (n=410), systolic blood pressure fell by 14.2 mmHg (P<0.001)."
            ),
        )
        _, diags_age = claims.evaluate_claim("In patients aged <65 years, systolic BP fell by 14.2 mmHg.", age_rec)
        self.assertIn("CLAIM_COHORT_MISMATCH", [d.code for d in diags_age])
        _, diags_age_ok = claims.evaluate_claim("In patients aged >=65 years, systolic BP fell by 14.2 mmHg.", age_rec)
        self.assertEqual(diags_age_ok, [])

        # Whole-paper population restatement is NOT a subgroup; unqualified claim passes cleanly
        t2d_rec = models.PaperRecord(
            title="Empagliflozin, Cardiovascular Outcomes, and Mortality in Type 2 Diabetes",
            abstract=(
                "METHODS: We assigned 7020 patients with type 2 diabetes to empagliflozin or placebo. "
                "RESULTS: In patients with type 2 diabetes, the primary outcome occurred in 10.5% vs 12.1% (HR 0.86)."
            ),
        )
        _, diags_t2d = claims.evaluate_claim(
            "Empagliflozin reduced the primary outcome (10.5% vs 12.1%, HR 0.86).", t2d_rec
        )
        self.assertEqual(diags_t2d, [])

        # "overall survival" is an endpoint, NOT an overall-cohort assertion
        os_rec = models.PaperRecord(
            title="Pembrolizumab in Non-Small-Cell Lung Cancer",
            abstract=(
                "RESULTS: Median overall survival was 12.1 months vs 9.4 months (HR 0.78). "
                "In the subgroup of patients with diabetes, median overall survival was 20.2 months (HR 0.54)."
            ),
        )
        _, diags_os = claims.evaluate_claim(
            "In the diabetes subgroup, median overall survival was 20.2 months (HR 0.54).", os_rec
        )
        self.assertEqual(diags_os, [])

        # Generic word "chronic" must not let "chronic kidney disease" match "chronic heart failure"
        hf_rec = models.PaperRecord(
            title="Treatment of Chronic Heart Failure",
            abstract="RESULTS: Primary outcome occurred in 16.3% vs 21.2% (HR 0.74, 95% CI 0.65-0.85).",
        )
        _, diags_ckd = claims.evaluate_claim(
            "In patients with chronic kidney disease, primary outcome was 16.3% vs 21.2% (HR 0.74).", hf_rec
        )
        self.assertIn("CLAIM_COHORT_MISMATCH", [d.code for d in diags_ckd])

        # Biomarker threshold subgroup swap (PD-L1 <1% vs PD-L1 >=50%) vs matching claim
        pdl1_rec = models.PaperRecord(
            title="Pembrolizumab in Non-Small-Cell Lung Cancer",
            abstract=(
                "RESULTS: In patients with PD-L1 expression >=50%, median overall survival was 20.2 months (HR 0.54), "
                "whereas in patients with PD-L1 <1%, median overall survival was 10.1 months (HR 0.91)."
            ),
        )
        _, diags_pdl1_bad = claims.evaluate_claim(
            "In patients with PD-L1 <1%, median overall survival was 20.2 months (HR 0.54).", pdl1_rec
        )
        self.assertIn("CLAIM_COHORT_MISMATCH", [d.code for d in diags_pdl1_bad])
        _, diags_pdl1_ok = claims.evaluate_claim(
            "In patients with PD-L1 >=50%, median overall survival was 20.2 months (HR 0.54).", pdl1_rec
        )
        self.assertEqual(diags_pdl1_ok, [])

        # Compound "and"-separated subgroups in a single sentence
        and_rec = models.PaperRecord(
            title="Dapagliflozin in Heart Failure with Reduced Ejection Fraction",
            abstract=(
                "RESULTS: The hazard ratio was 0.75 (95% CI, 0.63 to 0.90) among patients with diabetes "
                "and 0.73 (95% CI, 0.60 to 0.88) among patients without diabetes."
            ),
        )
        _, diags_and_bad = claims.evaluate_claim(
            "Among patients without diabetes, the hazard ratio was 0.75 (95% CI 0.63-0.90).", and_rec
        )
        self.assertIn("CLAIM_COHORT_MISMATCH", [d.code for d in diags_and_bad])
        _, diags_and_ok = claims.evaluate_claim(
            "Among patients without diabetes, the hazard ratio was 0.73 (95% CI 0.60-0.88).", and_rec
        )
        self.assertEqual(diags_and_ok, [])

    def test_cohort_review_regressions(self) -> None:
        # 1. Symbol-prefixed age bounds without "aged" ("patients <65 years" vs "patients >=65 years")
        age_rec = models.PaperRecord(
            title="Antihypertensive Therapy in Adults with Hypertension",
            abstract=(
                "METHODS: We enrolled 1200 adults with hypertension.\n"
                "RESULTS: Systolic BP fell by 8.4 mmHg overall. "
                "In patients >=65 years, systolic BP fell by 14.2 mmHg."
            ),
        )
        _, diags_sym_age = claims.evaluate_claim("In patients <65 years, systolic BP fell by 14.2 mmHg.", age_rec)
        self.assertIn("CLAIM_COHORT_MISMATCH", [d.code for d in diags_sym_age])
        _, diags_sym_ok = claims.evaluate_claim("In patients >=65 years, systolic BP fell by 14.2 mmHg.", age_rec)
        self.assertEqual(diags_sym_ok, [])

        # 2. Named wrong subgroup ("HFrEF subgroup") must NOT bypass via generic "subgroup" keyword
        dapa_sg = models.PaperRecord(
            title="Dapagliflozin in Patients with Heart Failure and Reduced Ejection Fraction",
            abstract=(
                "METHODS: We randomly assigned 4744 patients with heart failure and EF <= 40%.\n"
                "RESULTS: Primary outcome occurred in 16.3% vs 21.2% (HR 0.74; 95% CI, 0.65 to 0.85). "
                "In the prespecified subgroup of patients with diabetes (n=1983), HR was 0.75 (95% CI, 0.63 to 0.90)."
            ),
        )
        _, diags_wrong_sg = claims.evaluate_claim("In the HFrEF subgroup, HR was 0.75 (95% CI 0.63-0.90).", dapa_sg)
        self.assertIn("CLAIM_COHORT_MISMATCH", [d.code for d in diags_wrong_sg])

        # 3. Mixed-cohort claim (subgroup sample size n=1983 + overall HR 0.74) caught by per-token Check 2,
        # while valid trial N=4,744 + diabetes subgroup HR 0.75 still passes
        _, diags_mixed = claims.evaluate_claim("In patients with diabetes (n=1983), HR was 0.74.", dapa_sg)
        self.assertIn("CLAIM_COHORT_MISMATCH", [d.code for d in diags_mixed])
        _, diags_valid_mixed = claims.evaluate_claim(
            "In DAPA-HF (N=4,744), among patients with diabetes, HR was 0.75 (95% CI 0.63-0.90).", dapa_sg
        )
        self.assertEqual(diags_valid_mixed, [])

        # 4. t2dm vs diabetes polarity conflict ("with diabetes" vs "without type 2 diabetes")
        _, diags_t2d_pol = claims.evaluate_claim(
            "In patients without type 2 diabetes, HR was 0.75 (95% CI 0.63-0.90).", dapa_sg
        )
        self.assertIn("CLAIM_COHORT_MISMATCH", [d.code for d in diags_t2d_pol])

        # 5. vs/versus and 3-way comma/and subgroup splitting
        vs_rec = models.PaperRecord(
            title="Biomarker Trial in Solid Tumors",
            abstract=(
                "RESULTS: Response was 68.0% in stage II, 45.0% in stage III, and 22.0% in stage IV. "
                "HR was 0.75 among patients with diabetes vs 0.91 among patients without diabetes."
            ),
        )
        _, diags_stage = claims.evaluate_claim("In stage IV, response was 68.0%.", vs_rec)
        self.assertIn("CLAIM_COHORT_MISMATCH", [d.code for d in diags_stage])
        _, diags_vs = claims.evaluate_claim("Among patients without diabetes, HR was 0.75.", vs_rec)
        self.assertIn("CLAIM_COHORT_MISMATCH", [d.code for d in diags_vs])

        # 6. Unlisted race/class cohorts ("In Asian patients" vs "In Black patients", "NYHA class III/IV")
        race_rec = models.PaperRecord(
            title="Drug Z in Heart Failure",
            abstract=(
                "RESULTS: Overall response was 30.0%. "
                "In Asian patients, response was 52.0%, whereas in Black patients response was 28.0%. "
                "In NYHA class III/IV, mortality was 19.5%."
            ),
        )
        _, diags_race = claims.evaluate_claim("In Black patients, response was 52.0%.", race_rec)
        self.assertIn("CLAIM_COHORT_MISMATCH", [d.code for d in diags_race])
        _, diags_nyha = claims.evaluate_claim("Overall mortality was 19.5%.", race_rec)
        self.assertIn("CLAIM_COHORT_MISMATCH", [d.code for d in diags_nyha])
        _, diags_nyha_ok = claims.evaluate_claim("In NYHA class III/IV, mortality was 19.5%.", race_rec)
        self.assertEqual(diags_nyha_ok, [])

        # 7. False-positive guards:
        # (a) Mixed overall + subgroup sentence does NOT flag overall clause as subgroup
        mixed_sent_rec = models.PaperRecord(
            title="Trial of Drug Q",
            abstract="RESULTS: Overall HR was 0.74; in the diabetes subgroup, HR was 0.75.",
        )
        _, diags_overall_ok = claims.evaluate_claim("Overall, HR was 0.74.", mixed_sent_rec)
        self.assertEqual(diags_overall_ok, [])

        # (b) Multi-sentence METHODS enrollment & outcome count ("42 patients with serious adverse events")
        multi_methods_rec = models.PaperRecord(
            title="Drug M in Chronic Migraine",
            abstract=(
                "METHODS: This was a multicenter phase 3 trial. "
                "Eligible adults with chronic migraine received drug M 10 mg or placebo for 12 weeks.\n"
                "RESULTS: Monthly migraine days fell by 4.2 days (P<0.001). "
                "There were 42 patients with serious adverse events (12.1%)."
            ),
        )
        _, diags_ae_ok = claims.evaluate_claim(
            "Serious adverse events occurred in 12.1% of patients.", multi_methods_rec
        )
        self.assertEqual(diags_ae_ok, [])


class TestEndToEndMockedVerification(unittest.TestCase):
    def test_full_pass_citation(self) -> None:
        http = FakeHttpClient(
            [
                (
                    "api.crossref.org/works/10.1056/NEJMoa1911303",
                    http_client.HttpResponse(200, json.dumps(DAPA_HF_CROSSREF), "cr"),
                ),
                (
                    "europepmc/webservices/rest/search",
                    http_client.HttpResponse(200, json.dumps({"resultList": {"result": []}}), "epmc"),
                ),
                ("efetch.fcgi?db=pubmed&id=31535829", http_client.HttpResponse(200, DAPA_HF_PUBMED_XML, "pm")),
            ]
        )
        cit = models.CitationInput(
            doi="10.1056/NEJMoa1911303",
            pmid="31535829",
            title="Dapagliflozin in Patients with Heart Failure and Reduced Ejection Fraction",
            author="McMurray JJV",
            year="2019",
            journal="NEJM",
            claim="Dapagliflozin 10 mg in 4,744 patients reduced the primary outcome (16.3% vs 21.2%, HR 0.74, 95% CI 0.65-0.85, p<0.001).",
        )
        res = verify.verify_citation(cit, http)
        self.assertEqual(res.status, "PASS")
        self.assertEqual(res.diagnostics, [])
        txt = report.format_result_text(res)
        self.assertIn("STATUS: PASS", txt)
        self.assertIn("CLAIM_CHECK:", txt)
        self.assertIn("OFFICIAL ABSTRACT:", txt)

    def test_correct_doi_correct_number_wrong_cohort_flagged(self) -> None:
        """Fixture: correct DOI, correct number, wrong cohort (subgroup number misattributed)."""
        http = FakeHttpClient(
            [
                (
                    "api.crossref.org/works/10.1056/NEJMoa1911303",
                    http_client.HttpResponse(200, json.dumps(DAPA_HF_CROSSREF), "cr"),
                ),
                (
                    "esearch.fcgi",
                    http_client.HttpResponse(200, json.dumps({"esearchresult": {"idlist": ["31535829"]}}), "es"),
                ),
                ("efetch.fcgi?db=pubmed&id=31535829", http_client.HttpResponse(200, DAPA_HF_SUBGROUP_PUBMED_XML, "pm")),
            ]
        )

        # 1. Subgroup number (20.0% vs 25.5%, HR 0.75, 95% CI 0.63-0.90) attributed to overall population
        res_overall = verify.verify_citation(
            models.CitationInput(
                doi="10.1056/NEJMoa1911303",
                author="McMurray",
                year="2019",
                journal="NEJM",
                claim=(
                    "In DAPA-HF, dapagliflozin reduced the primary outcome in the overall population "
                    "(20.0% vs 25.5%; HR 0.75, 95% CI 0.63-0.90)."
                ),
            ),
            http,
        )
        self.assertEqual(res_overall.status, "WARN")
        assert res_overall.claim_report is not None
        self.assertEqual(res_overall.claim_report.missing_numbers, [])
        diag_overall = next(d for d in res_overall.diagnostics if d.code == "CLAIM_COHORT_MISMATCH")
        self.assertEqual(diag_overall.severity, "WARNING")
        self.assertEqual(diag_overall.details["reason"], "subgroup_attributed_to_overall")
        self.assertIn("HR 0.75", diag_overall.details["matched_numbers"])
        self.assertIn("diabetes", diag_overall.actual.lower())

        # 2. Subgroup number attributed to opposite subgroup ("without diabetes" / "nondiabetic")
        for wrong_claim in (
            "Among patients without diabetes, dapagliflozin reduced the primary outcome (HR 0.75, 95% CI 0.63-0.90).",
            "In nondiabetic patients, dapagliflozin achieved HR 0.75 (95% CI 0.63-0.90).",
        ):
            res_flip = verify.verify_citation(
                models.CitationInput(doi="10.1056/NEJMoa1911303", claim=wrong_claim),
                http,
            )
            self.assertEqual(res_flip.status, "WARN", f"Expected WARN for: {wrong_claim}")
            assert res_flip.claim_report is not None
            self.assertEqual(res_flip.claim_report.missing_numbers, [])
            diag_flip = next(d for d in res_flip.diagnostics if d.code == "CLAIM_COHORT_MISMATCH")
            self.assertEqual(diag_flip.details["reason"], "cohort_conflict")

        # 3. Subgroup number cited without mentioning the subgroup at all (mixed with overall N=4,744)
        res_omitted = verify.verify_citation(
            models.CitationInput(
                doi="10.1056/NEJMoa1911303",
                claim="In DAPA-HF (N=4,744), dapagliflozin reduced the primary outcome (20.0% vs 25.5%; HR 0.75, 95% CI 0.63-0.90).",
            ),
            http,
        )
        self.assertEqual(res_omitted.status, "WARN")
        assert res_omitted.claim_report is not None
        self.assertEqual(res_omitted.claim_report.missing_numbers, [])
        diag_omitted = next(d for d in res_omitted.diagnostics if d.code == "CLAIM_COHORT_MISMATCH")
        self.assertEqual(diag_omitted.details["reason"], "subgroup_qualifier_omitted")

        # 4. Overall number attributed to a different/absent cohort (CKD, HFpEF, or diabetes subgroup)
        for wrong_overall_claim in (
            "In patients with chronic kidney disease, dapagliflozin reduced the primary outcome (16.3% vs 21.2%; HR 0.74, 95% CI 0.65-0.85).",
            "In patients with HFpEF, dapagliflozin reduced the primary outcome (16.3% vs 21.2%; HR 0.74, 95% CI 0.65-0.85).",
            "In patients with diabetes, dapagliflozin reduced the primary outcome (16.3% vs 21.2%; HR 0.74, 95% CI 0.65-0.85).",
        ):
            res_wrong = verify.verify_citation(
                models.CitationInput(doi="10.1056/NEJMoa1911303", claim=wrong_overall_claim),
                http,
            )
            self.assertEqual(res_wrong.status, "WARN", f"Expected WARN for: {wrong_overall_claim}")
            assert res_wrong.claim_report is not None
            self.assertEqual(res_wrong.claim_report.missing_numbers, [])
            self.assertIn("CLAIM_COHORT_MISMATCH", [d.code for d in res_wrong.diagnostics])

        # 5. Controls on the exact same fixture: valid overall, alias (HFrEF), and matching subgroup claims still PASS
        for valid_claim in (
            "In DAPA-HF (N=4,744), dapagliflozin 10 mg reduced the primary outcome (16.3% vs 21.2%; HR 0.74, 95% CI 0.65-0.85, p<0.001).",
            "In patients with HFrEF, dapagliflozin reduced the primary outcome (16.3% vs 21.2%; HR 0.74, 95% CI 0.65-0.85).",
            "In diabetic patients, dapagliflozin reduced the primary outcome (20.0% vs 25.5%; HR 0.75, 95% CI 0.63-0.90).",
            "Among patients without diabetes (n=2761), the primary outcome occurred in 13.2% vs 17.7% (HR 0.73, 95% CI 0.60-0.88).",
        ):
            res_ok = verify.verify_citation(
                models.CitationInput(doi="10.1056/NEJMoa1911303", claim=valid_claim),
                http,
            )
            self.assertEqual(res_ok.status, "PASS", f"Expected PASS for: {valid_claim}; got {res_ok.diagnostics}")
            self.assertEqual(res_ok.diagnostics, [])

        # 6. CLI --strict treats CLAIM_COHORT_MISMATCH as non-zero exit
        buf = io.StringIO()
        with mock.patch.object(rv, "HttpClient", return_value=http), mock.patch("sys.stdout", buf):
            rc = rv.main(
                [
                    "check",
                    "--doi",
                    "10.1056/NEJMoa1911303",
                    "--claim",
                    "In the overall population, HR was 0.75 (95% CI 0.63-0.90).",
                    "--strict",
                    "--no-abstract",
                ]
            )
        self.assertEqual(rc, 1)
        self.assertIn("CLAIM_COHORT_MISMATCH", buf.getvalue())

    def test_pmid_doi_conflict(self) -> None:
        http = FakeHttpClient(
            [
                (
                    "api.crossref.org/works/10.1056/NEJMoa1911303",
                    http_client.HttpResponse(200, json.dumps(DAPA_HF_CROSSREF), "cr"),
                ),
                (
                    "europepmc/webservices/rest/search",
                    http_client.HttpResponse(200, json.dumps({"resultList": {"result": []}}), "epmc"),
                ),
                ("efetch.fcgi?db=pubmed&id=26378978", http_client.HttpResponse(200, OTHER_PAPER_PUBMED_XML, "pm")),
            ]
        )
        cit = models.CitationInput(doi="10.1056/NEJMoa1911303", pmid="26378978")
        res = verify.verify_citation(cit, http)
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
                ("efetch.fcgi?db=pubmed&id=99999991", http_client.HttpResponse(200, RETRACTED_PUBMED_XML, "pm")),
                (
                    "europepmc/webservices/rest/search",
                    http_client.HttpResponse(200, json.dumps({"resultList": {"result": []}}), "epmc"),
                ),
                ("api.crossref.org/works/", http_client.HttpResponse(404, "", "cr", "HTTP 404 Not Found")),
            ]
        )
        res = verify.verify_citation(models.CitationInput(pmid="99999991"), http)
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
                (
                    "api.crossref.org/works/10.1056/FAKE99999",
                    http_client.HttpResponse(404, "", "cr", "HTTP 404 Not Found"),
                ),
                (
                    "api.crossref.org/works?query.title=",
                    http_client.HttpResponse(200, json.dumps(search_payload), "cr-search"),
                ),
                (
                    "europepmc/webservices/rest/search",
                    http_client.HttpResponse(200, json.dumps({"resultList": {"result": []}}), "epmc"),
                ),
                (
                    "esearch.fcgi",
                    http_client.HttpResponse(200, json.dumps({"esearchresult": {"idlist": []}}), "pm-search"),
                ),
            ]
        )
        cit = models.CitationInput(
            doi="10.1056/FAKE99999",
            title="Dapagliflozin in Patients with Heart Failure and Reduced Ejection Fraction",
        )
        res = verify.verify_citation(cit, http)
        self.assertEqual(res.status, "FAIL")
        self.assertIn("DOI_NOT_FOUND", [d.code for d in res.diagnostics])
        self.assertTrue(len(res.did_you_mean) >= 1)
        # Primary paper with author + high citations ranks #1 above unauthored letter with same title
        self.assertEqual(res.did_you_mean[0]["doi"], "10.1056/NEJMoa1911303")
        txt = report.format_result_text(res)
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
                    http_client.HttpResponse(200, json.dumps(DAPA_HF_CROSSREF), "cr"),
                ),
                ("europepmc/webservices/rest/search", http_client.HttpResponse(200, json.dumps(epmc_json), "epmc")),
                ("efetch.fcgi?db=pubmed&id=31535829", http_client.HttpResponse(200, DAPA_HF_PUBMED_XML, "pm")),
                ("PMC7654321/fullTextXML", http_client.HttpResponse(200, fulltext_xml, "ft")),
            ]
        )
        cit = models.CitationInput(
            doi="10.1056/NEJMoa1911303",
            claim="In the diabetes subgroup (N=1983), HR was 0.75 (95% CI 0.63-0.90).",
            fulltext=True,
        )
        res = verify.verify_citation(cit, http)
        self.assertEqual(res.status, "PASS")
        assert res.claim_report is not None
        self.assertEqual(res.claim_report.missing_numbers, [])
        self.assertTrue(len(res.claim_report.fulltext_excerpts) >= 1)

    def test_oa_fulltext_falls_back_to_ncbi_pmc_when_europepmc_fails(self) -> None:
        ncbi_xml = "<pmc-articleset><article><body><p>Application site burning occurred in 46.3% of treated patients during induction therapy.</p></body></article></pmc-articleset>"
        epmc_json = {
            "resultList": {
                "result": [
                    {
                        "doi": "10.1056/NEJMoa1911303",
                        "pmid": "31535829",
                        "pmcid": "PMC9930707",
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
                    http_client.HttpResponse(200, json.dumps(DAPA_HF_CROSSREF), "cr"),
                ),
                ("europepmc/webservices/rest/search", http_client.HttpResponse(200, json.dumps(epmc_json), "epmc")),
                ("efetch.fcgi?db=pubmed&id=31535829", http_client.HttpResponse(200, DAPA_HF_PUBMED_XML, "pm")),
                (
                    "PMC9930707/fullTextXML",
                    http_client.HttpResponse(500, "", "ft-epmc", "HTTP 500 Internal Server Error"),
                ),
                ("efetch.fcgi?db=pmc&id=PMC9930707", http_client.HttpResponse(200, ncbi_xml, "ft-ncbi")),
            ]
        )
        cit = models.CitationInput(
            doi="10.1056/NEJMoa1911303",
            claim="Burning occurred in 46.3% of patients.",
            fulltext=True,
        )
        res = verify.verify_citation(cit, http)
        self.assertEqual(res.status, "PASS")
        assert res.claim_report is not None
        self.assertEqual(res.claim_report.missing_numbers, [])
        self.assertIn("46.3% [fulltext]", res.claim_report.matched_numbers)
        self.assertIn("NCBI PMC", res.claim_report.fulltext_status or "")

    def test_oa_fulltext_surfaces_timeout_error_and_ignores_bodyless_xml(self) -> None:
        restricted_ncbi_xml = (
            "<pmc-articleset><article><front><article-meta><abstract>"
            "<p>Restricted abstract mentioning 46.3% without full text body.</p>"
            "</abstract></article-meta></front></article></pmc-articleset>"
        )
        http = FakeHttpClient(
            [
                ("PMC8074061/fullTextXML", http_client.HttpResponse(0, "", "ft-epmc", "timed out")),
                ("efetch.fcgi?db=pmc&id=PMC8074061", http_client.HttpResponse(200, restricted_ncbi_xml, "ft-ncbi")),
            ]
        )
        rec = models.PaperRecord(pmcid="PMC8074061", abstract="Short abstract with no percentages.")
        rep, diags = claims.evaluate_claim(
            "Burning occurred in 46.3% of patients.", rec, http=http, fetch_fulltext=True
        )
        self.assertEqual(rep.missing_numbers, ["46.3%"])
        ft_diag = next(d for d in diags if d.code == "FULLTEXT_UNAVAILABLE")
        self.assertIn("timed out", ft_diag.message)
        self.assertNotIn("HTTP 0", ft_diag.message)
        self.assertIn("no <body>", ft_diag.message)
        self.assertIn("Transient", ft_diag.hint)


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
            segs = scan.extract_text_segments(p)
            cits = scan.extract_citations_from_segments(segs)
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
            segs = scan.extract_text_segments(p)
            cits = scan.extract_citations_from_segments(segs)
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
            segs = scan.extract_text_segments(p)
            cits = scan.extract_citations_from_segments(segs)
            self.assertEqual(len(cits), 2)
            self.assertEqual(cits[0].location, "Slide 1")
            self.assertEqual(cits[0].doi, "10.1056/NEJMoa1911303")
            self.assertEqual(cits[1].location, "Slide 1 notes")
            self.assertEqual(cits[1].pmid, "26378978")

    def test_unlinked_bibliography_entries_detected_and_reported_by_scan(self) -> None:
        segments = [
            ("document:p10", "12. BIBLIOGRAPHY15"),
            ("document:p11", "Body paragraph mentioning 2020 findings."),
            ("document:p194", "BIBLIOGRAPHY"),
            (
                "document:p195",
                "1. Ali F, Vyas J, Finlay AY. Counting the Burden. Acta Derm Venereol. 2020;100(12):adv00161.",
            ),
            ("document:p196", "2. Kramer ON et al. The history of atopic dermatitis. Clin Dermatol. 2017;35(4):344-8."),
            ("document:p197", "3. McMurray JJV et al. Dapagliflozin. N Engl J Med. 2019. doi:10.1056/NEJMoa1911303"),
            ("document:p198", "APPENDIX"),
            ("document:p199", "1. Aged 2 years or older with mild to moderate atopic dermatitis."),
            ("footer1:p1", "Department of Pharmacology Thesis Protocol 2026"),
        ]
        unlinked = scan.extract_unlinked_bibliography_entries(segments)
        self.assertEqual(len(unlinked), 2)
        self.assertEqual([u.location for u in unlinked], ["document:p195", "document:p196"])

        md = "\n".join(text for loc, text in segments if loc.startswith("document:"))
        fake_http = FakeHttpClient(
            [
                (
                    "api.crossref.org/works/10.1056/NEJMoa1911303",
                    http_client.HttpResponse(200, json.dumps(DAPA_HF_CROSSREF), "cr"),
                ),
                (
                    "europepmc/webservices/rest/search",
                    http_client.HttpResponse(200, json.dumps({"resultList": {"result": []}}), "epmc"),
                ),
                ("esearch.fcgi", http_client.HttpResponse(200, json.dumps({"esearchresult": {"idlist": []}}), "es")),
            ]
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "protocol.md"
            p.write_text(md, encoding="utf-8")
            buf = io.StringIO()
            with mock.patch.object(rv, "HttpClient", return_value=fake_http), mock.patch("sys.stdout", buf):
                rc = rv.main(["scan", str(p), "--json"])
            self.assertEqual(rc, 0)
            parsed = json.loads(buf.getvalue())
            self.assertEqual(parsed["extracted_count"], 1)
            self.assertEqual(parsed["unlinked_bibliography_count"], 2)
            self.assertEqual(parsed["scan_diagnostics"][0]["code"], "UNLINKED_BIBLIOGRAPHY_ENTRIES")

            buf_text = io.StringIO()
            with mock.patch.object(rv, "HttpClient", return_value=fake_http), mock.patch("sys.stdout", buf_text):
                rc_text = rv.main(["scan", str(p), "--no-abstract"])
            self.assertEqual(rc_text, 0)
            text_out = buf_text.getvalue()
            self.assertIn("UNLINKED_BIBLIOGRAPHY_ENTRIES", text_out)
            self.assertIn("Hint:", text_out)
            self.assertIn("Ali F, Vyas J", text_out)

            buf_strict = io.StringIO()
            with mock.patch.object(rv, "HttpClient", return_value=fake_http), mock.patch("sys.stdout", buf_strict):
                rc_strict = rv.main(["scan", str(p), "--json", "--strict"])
            self.assertEqual(rc_strict, 1)

    def test_scan_zero_doi_bibliography_and_docx_pptx_part_boundaries(self) -> None:
        # Zero-DOI plain-text bibliography: text output includes previews/hint and --strict fails
        plain_md = (
            "# References\n"
            "1. Ali F, Vyas J, Finlay AY. Counting the Burden. Acta Derm Venereol. 2020;100(12):adv00161.\n"
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "plain.md"
            p.write_text(plain_md, encoding="utf-8")
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = rv.main(["scan", str(p)])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("UNLINKED_BIBLIOGRAPHY_ENTRIES", out)
            self.assertIn("line 2:", out)
            self.assertIn("Hint:", out)
            with mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(rv.main(["scan", str(p), "--strict"]), 1)

        # DOCX: References at end of document.xml must NOT leak into footer1.xml
        doc_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            "<w:p><w:r><w:t>References</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>1. Ali F. Counting the Burden. Acta Derm Venereol. 2020;100(12):adv00161.</w:t></w:r></w:p>"
            "</w:body></w:document>"
        )
        footer_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:p><w:r><w:t>Confidential Thesis Protocol Copyright 2026</w:t></w:r></w:p>"
            "</w:ftr>"
        )
        with tempfile.TemporaryDirectory() as td:
            dp = Path(td) / "bib.docx"
            with zipfile.ZipFile(dp, "w") as zf:
                zf.writestr("word/document.xml", doc_xml)
                zf.writestr("word/footer1.xml", footer_xml)
            unlinked_docx = scan.extract_unlinked_bibliography_entries(scan.extract_text_segments(dp))
            self.assertEqual(len(unlinked_docx), 1)
            self.assertEqual(unlinked_docx[0].location, "document:p2")

        # PPTX: multi-line References slide detects unlinked line even when another line on the slide has a DOI
        slide_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:sp><p:txBody>'
            "<a:p><a:r><a:t>References</a:t></a:r></a:p>"
            "<a:p><a:r><a:t>1. Ali F. Counting the Burden. Acta Derm Venereol. 2020;100(12):adv00161.</a:t></a:r></a:p>"
            "<a:p><a:r><a:t>2. McMurray JJV. DAPA-HF. N Engl J Med. 2019. doi:10.1056/NEJMoa1911303</a:t></a:r></a:p>"
            "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
        )
        with tempfile.TemporaryDirectory() as td:
            pp = Path(td) / "deck.pptx"
            with zipfile.ZipFile(pp, "w") as zf:
                zf.writestr("ppt/slides/slide1.xml", slide_xml)
            unlinked_pptx = scan.extract_unlinked_bibliography_entries(scan.extract_text_segments(pp))
            self.assertEqual(len(unlinked_pptx), 1)
            self.assertEqual(unlinked_pptx[0].location, "Slide 1")
            self.assertIn("Ali F.", unlinked_pptx[0].text)


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
                    http_client.HttpResponse(200, json.dumps(DAPA_HF_CROSSREF), "cr"),
                ),
                (
                    "europepmc/webservices/rest/search",
                    http_client.HttpResponse(200, json.dumps({"resultList": {"result": []}}), "epmc"),
                ),
                ("esearch.fcgi", http_client.HttpResponse(200, json.dumps({"esearchresult": {"idlist": []}}), "es")),
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

    def test_batch_prefetch_uses_multi_id_endpoints_and_preserves_order(self) -> None:
        cr_batch_payload = {
            "message": {
                "items": [
                    DAPA_HF_CROSSREF["message"],
                    {
                        "DOI": "10.1056/NEJMoa1504720",
                        "title": ["Empagliflozin, Cardiovascular Outcomes, and Mortality in Type 2 Diabetes"],
                        "author": [{"family": "Zinman", "given": "Bernard"}],
                        "published-print": {"date-parts": [[2015, 11, 26]]},
                        "container-title": ["New England Journal of Medicine"],
                        "short-container-title": ["N Engl J Med"],
                    },
                ]
            }
        }
        pm_batch_xml = (
            '<?xml version="1.0" encoding="UTF-8"?><PubmedArticleSet>'
            + DAPA_HF_PUBMED_XML.replace(
                '<ArticleId IdType="doi">10.1056/NEJMoa1911303</ArticleId>',
                '<ArticleId IdType="doi">10.1056/NEJMoa1911303</ArticleId><ArticleId IdType="pmc">PMC7654321</ArticleId>',
            )
            .split("<PubmedArticleSet>", 1)[1]
            .split("</PubmedArticleSet>", 1)[0]
            + OTHER_PAPER_PUBMED_XML.replace(
                '<ArticleId IdType="doi">10.1056/NEJMoa1504720</ArticleId>',
                '<ArticleId IdType="doi">10.1056/NEJMoa1504720</ArticleId><ArticleId IdType="pmc">PMC9999999</ArticleId>',
            )
            .split("<PubmedArticleSet>", 1)[1]
            .split("</PubmedArticleSet>", 1)[0]
            + "</PubmedArticleSet>"
        )
        pmc_batch_xml = (
            "<pmc-articleset>"
            '<article><front><article-meta><article-id pub-id-type="pmc">7654321</article-id></article-meta></front>'
            "<body><p>In the diabetes subgroup of 1983 patients, the hazard ratio was 0.75 (95% CI, 0.63 to 0.90).</p></body>"
            "</article>"
            '<article><front><article-meta><article-id pub-id-type="pmc">PMC9999999</article-id></article-meta></front>'
            "<body><p>Empagliflozin reduced cardiovascular mortality in patients with type 2 diabetes.</p></body>"
            "</article>"
            "</pmc-articleset>"
        )
        http = FakeHttpClient(
            [
                (
                    "api.crossref.org/works?filter=",
                    http_client.HttpResponse(200, json.dumps(cr_batch_payload), "cr-batch"),
                ),
                (
                    "esearch.fcgi",
                    http_client.HttpResponse(
                        200, json.dumps({"esearchresult": {"idlist": ["31535829", "26378978"]}}), "es"
                    ),
                ),
                ("efetch.fcgi?db=pubmed&id=31535829,26378978", http_client.HttpResponse(200, pm_batch_xml, "pm-batch")),
                ("efetch.fcgi?db=pmc&id=PMC7654321", http_client.HttpResponse(200, pmc_batch_xml, "pmc-batch")),
            ]
        )
        cits = [
            models.CitationInput(
                doi="10.1056/NEJMoa1911303",
                author="McMurray",
                year="2019",
                claim="Subgroup N=1983 had HR 0.75 (95% CI 0.63-0.90).",
                label="first",
                fulltext=True,
            ),
            models.CitationInput(
                doi="10.1056/NEJMoa1504720",
                author="Zinman",
                year="2015",
                label="second",
                fulltext=False,
            ),
        ]
        results = verify.verify_citations_batch(cits, http, max_workers=4)
        self.assertEqual([r.input.label for r in results], ["first", "second"])
        self.assertEqual(results[0].status, "PASS")
        self.assertEqual(results[1].status, "PASS")
        assert results[0].claim_report is not None
        self.assertEqual(results[0].claim_report.missing_numbers, [])
        self.assertIn("N=1983 [fulltext]", results[0].claim_report.matched_numbers)
        # Europe PMC is skipped entirely when Crossref + PubMed already resolve everything!
        self.assertFalse(any("europepmc" in u for u in http.calls))
        # Total HTTP calls: Crossref batch + PubMed esearch batch + PubMed efetch batch + NCBI PMC fulltext (4 total)
        self.assertEqual(len(http.calls), 4)

        # Non-OA article with bodyless NCBI PMC XML skips Europe PMC fullTextXML cleanly
        non_oa_http = FakeHttpClient(
            [
                (
                    "efetch.fcgi?db=pmc&id=PMC9999999",
                    http_client.HttpResponse(
                        200,
                        "<pmc-articleset><article><front><article-meta/></front></article></pmc-articleset>",
                        "pmc",
                    ),
                ),
            ]
        )
        ft = fulltext.fetch_oa_fulltext_paragraphs(non_oa_http, "PMC9999999", is_open_access=False)
        self.assertEqual(ft.paragraphs, [])
        self.assertFalse(any("fullTextXML" in u for u in non_oa_http.calls))

    def test_batch_falls_back_gracefully_when_batch_endpoint_fails(self) -> None:
        http = FakeHttpClient(
            [
                ("api.crossref.org/works?filter=", http_client.HttpResponse(500, "", "cr-batch", "HTTP 500")),
                (
                    "api.crossref.org/works/10.1056/NEJMoa1911303",
                    http_client.HttpResponse(200, json.dumps(DAPA_HF_CROSSREF), "cr-1"),
                ),
                (
                    "api.crossref.org/works/10.1056/NEJMoa1504720",
                    http_client.HttpResponse(404, "", "cr-2", "HTTP 404 Not Found"),
                ),
                (
                    "europepmc/webservices/rest/search",
                    http_client.HttpResponse(200, json.dumps({"resultList": {"result": []}}), "epmc"),
                ),
                ("esearch.fcgi", http_client.HttpResponse(200, json.dumps({"esearchresult": {"idlist": []}}), "es")),
            ]
        )
        cits = [
            models.CitationInput(doi="10.1056/NEJMoa1911303", year="2019", label="ok"),
            models.CitationInput(doi="10.1056/NEJMoa1504720", year="2015", label="missing"),
        ]
        results = verify.verify_citations_batch(cits, http, max_workers=2)
        self.assertEqual(results[0].status, "PASS")
        self.assertEqual(results[1].status, "FAIL")
        self.assertIn("DOI_NOT_FOUND", [d.code for d in results[1].diagnostics])

    def test_multi_xml_parsers_and_decompression(self) -> None:
        import gzip
        import zlib

        sample = b"hello ref-verify"
        self.assertEqual(http_client._decompress_body(gzip.compress(sample), "gzip"), sample)
        self.assertEqual(http_client._decompress_body(zlib.compress(sample), "deflate"), sample)
        self.assertEqual(http_client._decompress_body(sample, ""), sample)

        many_pm = pubmed.parse_pubmed_xml_many(
            "<PubmedArticleSet>"
            + DAPA_HF_PUBMED_XML.split("<PubmedArticleSet>", 1)[1].split("</PubmedArticleSet>", 1)[0]
            + OTHER_PAPER_PUBMED_XML.split("<PubmedArticleSet>", 1)[1].split("</PubmedArticleSet>", 1)[0]
            + "</PubmedArticleSet>"
        )
        self.assertEqual(set(many_pm.keys()), {"31535829", "26378978"})

        many_pmc = fulltext.parse_pmc_xml_many(
            "<pmc-articleset>"
            '<article><front><article-meta><article-id pub-id-type="pmc">12345</article-id></article-meta></front>'
            "<body><p>Sufficiently long full-text body paragraph for parsing verification.</p></body></article>"
            "</pmc-articleset>"
        )
        self.assertIn("PMC12345", many_pmc)
        self.assertEqual(len(many_pmc["PMC12345"].paragraphs), 1)

    def test_workers_flag_parsed_on_batch_and_scan(self) -> None:
        parser = rv.build_parser()
        args_b = parser.parse_args(["batch", "refs.json", "--workers", "4"])
        self.assertEqual(args_b.workers, 4)
        args_s = parser.parse_args(["scan", "paper.md", "--workers", "1"])
        self.assertEqual(args_s.workers, 1)

    def test_doi_not_found_does_not_resolve_from_speculative_esearch(self) -> None:
        # Regression: parallel pubmed_esearch_by_doi must not yield a non-null resolved record when Crossref+EPMC miss
        http = FakeHttpClient(
            [
                (
                    "api.crossref.org/works/10.1056/FAKE99999",
                    http_client.HttpResponse(404, "", "cr", "HTTP 404 Not Found"),
                ),
                (
                    "europepmc/webservices/rest/search",
                    http_client.HttpResponse(200, json.dumps({"resultList": {"result": []}}), "epmc"),
                ),
                (
                    "esearch.fcgi",
                    http_client.HttpResponse(200, json.dumps({"esearchresult": {"idlist": ["31535829"]}}), "es"),
                ),
                ("efetch.fcgi?db=pubmed&id=31535829", http_client.HttpResponse(200, DAPA_HF_PUBMED_XML, "pm")),
            ]
        )
        res = verify.verify_citation(models.CitationInput(doi="10.1056/FAKE99999", year="2020"), http)
        self.assertEqual(res.status, "FAIL")
        self.assertIsNone(res.resolved)
        self.assertEqual([d.code for d in res.diagnostics], ["DOI_NOT_FOUND"])

    def test_corrupt_batch_pubmed_xml_falls_back_to_single_pmid(self) -> None:
        http = FakeHttpClient(
            [
                (
                    "efetch.fcgi?db=pubmed&id=31535829,26378978",
                    http_client.HttpResponse(200, "<PubmedArticleSet><broken", "pm"),
                ),
                ("efetch.fcgi?db=pubmed&id=31535829", http_client.HttpResponse(200, DAPA_HF_PUBMED_XML, "pm1")),
                ("efetch.fcgi?db=pubmed&id=26378978", http_client.HttpResponse(200, OTHER_PAPER_PUBMED_XML, "pm2")),
                (
                    "api.crossref.org/works/10.1056/NEJMoa1911303",
                    http_client.HttpResponse(200, json.dumps(DAPA_HF_CROSSREF), "cr1"),
                ),
                ("api.crossref.org/works/10.1056/NEJMoa1504720", http_client.HttpResponse(404, "", "cr2", "HTTP 404")),
                (
                    "europepmc/webservices/rest/search",
                    http_client.HttpResponse(200, json.dumps({"resultList": {"result": []}}), "epmc"),
                ),
            ]
        )
        results = verify.verify_citations_batch(
            [models.CitationInput(pmid="31535829", year="2019"), models.CitationInput(pmid="26378978", year="2015")],
            http,
            max_workers=2,
        )
        self.assertEqual([r.status for r in results], ["PASS", "PASS"])
        self.assertTrue(all(r.resolved is not None for r in results))

    def test_truncated_batch_esearch_marks_incomplete(self) -> None:
        http = FakeHttpClient(
            [
                (
                    "esearch.fcgi",
                    http_client.HttpResponse(
                        200,
                        json.dumps({"esearchresult": {"count": "99", "idlist": ["31535829", "26378978"]}}),
                        "es",
                    ),
                )
            ]
        )
        ids, ok = pubmed.pubmed_esearch_by_dois_batch(http, ["10.1056/NEJMoa1911303", "10.1056/NEJMoa1504720"])
        self.assertEqual(ids, ["31535829", "26378978"])
        self.assertFalse(ok)

    def test_fulltext_discovers_epmc_only_pmcid_when_pubmed_lacks_pmcid(self) -> None:
        # DAPA_HF_PUBMED_XML has no PMCID; Europe PMC provides PMC7654321
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
        ft_xml = (
            "<article><body><p>Subgroup analysis included 1983 patients with hazard ratio 0.75.</p></body></article>"
        )
        http = FakeHttpClient(
            [
                (
                    "api.crossref.org/works/10.1056/NEJMoa1911303",
                    http_client.HttpResponse(200, json.dumps(DAPA_HF_CROSSREF), "cr"),
                ),
                (
                    "esearch.fcgi",
                    http_client.HttpResponse(200, json.dumps({"esearchresult": {"idlist": ["31535829"]}}), "es"),
                ),
                ("efetch.fcgi?db=pubmed&id=31535829", http_client.HttpResponse(200, DAPA_HF_PUBMED_XML, "pm")),
                ("europepmc/webservices/rest/search", http_client.HttpResponse(200, json.dumps(epmc_json), "epmc")),
                ("efetch.fcgi?db=pmc&id=PMC7654321", http_client.HttpResponse(200, ft_xml, "ft")),
            ]
        )
        res = verify.verify_citation(
            models.CitationInput(
                doi="10.1056/NEJMoa1911303",
                claim="Subgroup N=1983 had HR 0.75.",
                fulltext=True,
            ),
            http,
        )
        self.assertEqual(res.status, "PASS")
        assert res.claim_report is not None
        self.assertEqual(res.claim_report.missing_numbers, [])

    def test_stale_pool_socket_retries_with_fresh_connection_and_handles_ssl_error(self) -> None:
        import ssl

        client = http_client.HttpClient(timeout=5.0, retries=0)
        borrow_calls: List[bool] = []

        class _BadConn:
            def __init__(self, exc: Exception) -> None:
                self._exc = exc
                self.closed = False

            def request(self, *args: object, **kwargs: object) -> None:
                raise self._exc

            def close(self) -> None:
                self.closed = True

        class _GoodResp:
            status = 200
            reason = "OK"
            will_close = True

            class _Headers:
                def get(self, k: str, default: Optional[str] = None) -> Optional[str]:
                    return default

                def get_content_charset(self) -> str:
                    return "utf-8"

            headers = _Headers()

            def read(self) -> bytes:
                return b"ok"

        class _GoodConn:
            def request(self, *args: object, **kwargs: object) -> None:
                pass

            def getresponse(self) -> _GoodResp:
                return _GoodResp()

            def close(self) -> None:
                pass

        bad = _BadConn(ssl.SSLError("EOF occurred in violation of protocol"))
        good = _GoodConn()

        def fake_borrow(scheme: str, host: str, port: int, timeout: float, force_fresh: bool = False):  # type: ignore[no-untyped-def]
            borrow_calls.append(force_fresh)
            if not force_fresh:
                return bad, True
            return good, False

        with mock.patch.object(client, "_borrow_conn", side_effect=fake_borrow):
            status, body, _, _, _ = client._single_request_pooled("https://example.org/test", {}, 5.0)
        self.assertEqual(status, 200)
        self.assertEqual(body, "ok")
        self.assertEqual(borrow_calls, [False, True])
        self.assertTrue(bad.closed)

    def test_http_client_get_fallback_policy(self) -> None:
        client = http_client.HttpClient(timeout=12.0, retries=2)
        seen: List[tuple[float, int]] = []

        def fake_impl(
            url: str, headers: Optional[Dict[str, str]], timeout: float, retries: int
        ) -> http_client.HttpResponse:
            seen.append((timeout, retries))
            return http_client.HttpResponse(200, "{}", url)

        with mock.patch.object(client, "_get_impl", side_effect=fake_impl):
            client.get_fallback("https://example.org/a", primary_failed_transient=False)
            client.get_fallback("https://example.org/b", primary_failed_transient=True)
        self.assertEqual(seen, [(client.FALLBACK_TIMEOUT, 0), (12.0, 2)])


if __name__ == "__main__":
    unittest.main()
