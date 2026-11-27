#!/usr/bin/env python3
"""Offline tests for finding a DOI/PMID by search when a reference cites neither."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from test_ref_verify import DAPA_HF_CROSSREF, DAPA_HF_PUBMED_XML, FakeHttpClient

from ref_verify import checklist, models, resolve, scan, verify
from ref_verify import cli as rv

DAPA_TITLE = "Dapagliflozin in Patients with Heart Failure and Reduced Ejection Fraction"
DAPA_NO_ID_REF = (
    "1. McMurray JJV, Solomon SD, Inzucchi SE, et al. Dapagliflozin in Patients with Heart Failure and "
    "Reduced Ejection Fraction. N Engl J Med. 2019;381(21):1995-2008."
)
# Shaped so `refparse` cannot split author/title (no initials), so it stays an UNVERIFIED row.
UNPARSEABLE_REF = "2. World Health Organization. Global report on diabetes. Geneva: WHO; 2016."


def _candidate(**overrides: Any) -> dict[str, Any]:
    cand: dict[str, Any] = {
        "source": "crossref",
        "doi": "10.1056/NEJMoa1911303",
        "pmid": None,
        "title": DAPA_TITLE,
        "first_author": "McMurray JJV",
        "year": 2019,
        "journal": "N Engl J Med",
        "title_similarity": 1.0,
        "cited_by_count": 6500,
    }
    cand.update(overrides)
    return cand


def _cited(**overrides: Any) -> models.CitationInput:
    cit = models.CitationInput(title=DAPA_TITLE, author="McMurray JJV", year="2019", journal="N Engl J Med")
    for key, value in overrides.items():
        setattr(cit, key, value)
    return cit


def _search_routes(*, crossref_items: list[dict[str, Any]], pubmed_ids: list[str]) -> list[tuple[str, Any]]:
    summary = {
        "result": {
            "31535829": {
                "title": f"{DAPA_TITLE}.",
                "articleids": [{"idtype": "doi", "value": "10.1056/NEJMoa1911303"}],
                "authors": [{"name": "McMurray JJV"}],
                "pubdate": "2019 Nov 21",
                "source": "N Engl J Med",
            }
        }
    }
    return [
        (
            "api.crossref.org/works?query",
            models.HttpResponse(200, json.dumps({"message": {"items": crossref_items}}), "cr-search"),
        ),
        (
            "esearch.fcgi",
            models.HttpResponse(200, json.dumps({"esearchresult": {"idlist": pubmed_ids}}), "pm-search"),
        ),
        ("esummary.fcgi", models.HttpResponse(200, json.dumps(summary), "pm-summary")),
    ]


def _verify_routes() -> list[tuple[str, Any]]:
    return [
        ("api.crossref.org/works/10.1056/NEJMoa1911303", models.HttpResponse(200, json.dumps(DAPA_HF_CROSSREF), "cr")),
        (
            "europepmc/webservices/rest/search",
            models.HttpResponse(200, json.dumps({"resultList": {"result": []}}), "epmc"),
        ),
        ("efetch.fcgi?db=pubmed&id=31535829", models.HttpResponse(200, DAPA_HF_PUBMED_XML, "pm")),
    ]


def _dapa_found_http() -> FakeHttpClient:
    primary = dict(DAPA_HF_CROSSREF["message"], **{"is-referenced-by-count": 6500})
    return FakeHttpClient(_search_routes(crossref_items=[primary], pubmed_ids=["31535829"]) + _verify_routes())


class TestPickingAConfidentMatch(unittest.TestCase):
    def test_title_author_and_year_agreeing_is_a_match(self) -> None:
        match = resolve.pick_confident_match(_cited(), [_candidate()])
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match["doi"], "10.1056/NEJMoa1911303")

    def test_any_contradiction_rejects_the_candidate(self) -> None:
        for label, cand in (
            ("different first author", _candidate(first_author="Zinman B")),
            ("year two off", _candidate(year=2021)),
            ("title too different", _candidate(title="Dapagliflozin in heart failure", title_similarity=0.6)),
            ("no author to confirm the cited one", _candidate(first_author=None)),
        ):
            with self.subTest(label):
                self.assertIsNone(resolve.pick_confident_match(_cited(), [cand]))

    def test_title_alone_is_not_enough(self) -> None:
        self.assertIsNone(resolve.pick_confident_match(_cited(author=None, year=None), [_candidate()]))

    def test_first_author_and_year_are_both_required(self) -> None:
        self.assertIsNone(resolve.pick_confident_match(_cited(author=None), [_candidate()]))
        self.assertIsNone(resolve.pick_confident_match(_cited(year=None), [_candidate()]))
        self.assertIsNone(resolve.pick_confident_match(_cited(), [_candidate(year=None)]))

    def test_a_retracted_original_is_matched_so_its_retraction_is_reported(self) -> None:
        retracted = _candidate(title=f"RETRACTED: {DAPA_TITLE}", title_similarity=0.93)
        match = resolve.pick_confident_match(_cited(), resolve.rank_candidates([retracted]))
        assert match is not None
        self.assertEqual(match["doi"], "10.1056/NEJMoa1911303")
        for notice in (f"Retraction: {DAPA_TITLE}", f"Retraction Note: {DAPA_TITLE}", f"{DAPA_TITLE}. Reply"):
            with self.subTest(notice):
                self.assertTrue(resolve.is_notice_title(notice))
        self.assertFalse(resolve.is_notice_title(f"RETRACTED: {DAPA_TITLE}"))

    def test_distinct_pubmed_records_sharing_a_title_stay_separate(self) -> None:
        a = _candidate(source="pubmed", doi=None, pmid="111", first_author="Zinman B")
        b = _candidate(source="pubmed", doi=None, pmid="222", first_author="McMurray JJV")
        self.assertEqual(len(resolve.rank_candidates([a, b])), 2)

    def test_a_title_cited_without_its_subtitle_needs_author_and_year(self) -> None:
        full = _candidate(
            title="Counting the Burden: Atopic Dermatitis and Health-related Quality of Life",
            first_author="Ali F",
            year=2020,
            journal="Acta Derm Venereol",
            title_similarity=0.418,
        )
        cited = _cited(title="Counting the Burden", author="Ali F", year="2020", journal="Acta Derm Venereol")
        match = resolve.pick_confident_match(cited, [full])
        assert match is not None
        self.assertEqual(match["doi"], full["doi"])
        cited.year = None
        self.assertIsNone(resolve.pick_confident_match(cited, [full]))

    def test_two_different_papers_that_both_fit_are_ambiguous(self) -> None:
        twin = _candidate(doi="10.1101/2019.01.01.000001", journal="medRxiv")
        self.assertIsNone(resolve.pick_confident_match(_cited(journal=None), [_candidate(), twin]))
        # The cited journal breaks the tie between a preprint and the published version.
        match = resolve.pick_confident_match(_cited(), [twin, _candidate()])
        assert match is not None
        self.assertEqual(match["doi"], "10.1056/NEJMoa1911303")

    def test_next_years_letter_by_the_same_authors_loses_to_the_paper(self) -> None:
        letter = _candidate(doi="10.1056/NEJMc1917241", year=2020)  # Crossref lists it under the bare title
        match = resolve.pick_confident_match(_cited(), [letter, _candidate()])
        assert match is not None
        self.assertEqual(match["doi"], "10.1056/NEJMoa1911303")

    def test_a_reply_letter_sharing_title_and_authors_is_never_the_match(self) -> None:
        # Crossref lists the reply under the trial's own title; only PubMed's copy ends in "Reply".
        reply_crossref = _candidate(doi="10.1056/NEJMc1917241", first_author=None, year=2020, cited_by_count=4)
        reply_pubmed = _candidate(
            source="pubmed", doi="10.1056/NEJMc1917241", pmid="32130823", title=f"{DAPA_TITLE}. Reply", year=2020
        )
        candidates = resolve.rank_candidates([reply_crossref, reply_pubmed])
        self.assertIsNone(resolve.pick_confident_match(_cited(), candidates))
        match = resolve.pick_confident_match(
            _cited(), resolve.rank_candidates([reply_crossref, reply_pubmed, _candidate()])
        )
        assert match is not None
        self.assertEqual(match["doi"], "10.1056/NEJMoa1911303")

    def test_the_exact_year_beats_a_one_year_offset(self) -> None:
        next_year = _candidate(doi="10.1/other", year=2020)
        match = resolve.pick_confident_match(_cited(), [next_year, _candidate()])
        assert match is not None
        self.assertEqual(match["doi"], "10.1056/NEJMoa1911303")

    def test_pubmed_and_crossref_records_of_one_paper_combine(self) -> None:
        pubmed_only = _candidate(source="pubmed", doi=None, pmid="31535829")
        match = resolve.pick_confident_match(_cited(), [_candidate(), pubmed_only])
        assert match is not None
        self.assertEqual((match["doi"], match["pmid"]), ("10.1056/NEJMoa1911303", "31535829"))


class TestResolvingCitations(unittest.TestCase):
    def test_title_only_citation_is_verified_against_the_found_paper(self) -> None:
        http = _dapa_found_http()
        res = verify.verify_citation(_cited(), http)
        self.assertEqual(res.status, "PASS")
        self.assertEqual((res.input.doi, res.input.pmid), ("10.1056/NEJMoa1911303", "31535829"))
        self.assertIsNotNone(res.resolved)
        (note,) = [d for d in res.diagnostics if d.code == "IDENTIFIER_RESOLVED_BY_SEARCH"]
        self.assertEqual(note.severity, "INFO")
        self.assertIn("10.1056/NEJMoa1911303", note.fix)
        row = checklist.build_row(1, res)
        self.assertEqual(row.cells["identifier"].verdict, checklist.INFO)

    def test_no_confident_match_keeps_the_did_you_mean_warning(self) -> None:
        other = dict(DAPA_HF_CROSSREF["message"], author=[{"family": "Zinman", "given": "Bernard"}])
        http = FakeHttpClient(_search_routes(crossref_items=[other], pubmed_ids=[]))
        res = verify.verify_citation(_cited(), http)
        self.assertEqual(res.status, "WARN")
        self.assertIsNone(res.input.doi)
        self.assertEqual([d.code for d in res.diagnostics], ["NO_IDENTIFIER_PROVIDED"])
        self.assertEqual(res.did_you_mean[0]["doi"], "10.1056/NEJMoa1911303")

    def test_a_search_outage_is_a_lookup_error_not_a_missing_paper(self) -> None:
        down = models.HttpResponse(0, "", "cr-search", "timed out")
        http = FakeHttpClient(
            [("api.crossref.org/works?query", down)] + _search_routes(crossref_items=[], pubmed_ids=[])
        )
        res = verify.verify_citation(_cited(), http)
        self.assertEqual(res.status, "FAIL")
        self.assertEqual([d.code for d in res.diagnostics], ["LOOKUP_ERROR"])
        self.assertIsNotNone(res.input.resolution)
        assert res.input.resolution is not None
        self.assertTrue(res.input.resolution.lookup_failed)

    def test_a_malformed_cited_identifier_is_never_replaced_by_search(self) -> None:
        http = _dapa_found_http()
        res = verify.verify_citation(_cited(doi="10.bad"), http)
        self.assertIn("DOI_SYNTAX_INVALID", [d.code for d in res.diagnostics])
        self.assertNotIn("IDENTIFIER_RESOLVED_BY_SEARCH", [d.code for d in res.diagnostics])
        (no_id,) = [d for d in res.diagnostics if d.code == "NO_IDENTIFIER_PROVIDED"]
        self.assertNotIn("first author", no_id.message)  # no match was attempted, so none is claimed

    def test_batch_resolves_before_prefetching(self) -> None:
        http = _dapa_found_http()
        cited_id = models.CitationInput(doi="10.1056/NEJMoa1911303", label="with-doi")
        results = verify.verify_citations_batch([cited_id, _cited(label="title-only")], http, max_workers=2)
        self.assertEqual([r.status for r in results], ["PASS", "PASS"])
        self.assertIn("IDENTIFIER_RESOLVED_BY_SEARCH", [d.code for d in results[1].diagnostics])

    def test_pubmed_title_search_ranks_by_relevance_not_recency(self) -> None:
        http = _dapa_found_http()
        resolve.resolve_citation(_cited(), http)
        (search_url,) = [u for u in http.calls if "esearch.fcgi" in u]
        self.assertIn("sort=relevance", search_url)


class TestScanningReferencesWithoutIdentifiers(unittest.TestCase):
    def test_entry_with_a_parsed_title_becomes_a_searchable_citation(self) -> None:
        entries = [
            models.UnlinkedEntry("line 3", DAPA_NO_ID_REF),
            models.UnlinkedEntry("line 4", UNPARSEABLE_REF),
        ]
        searchable, unlinked = scan.split_searchable_entries(entries)
        (cit,) = searchable
        self.assertEqual((cit.title, cit.author, cit.year), (DAPA_TITLE, "McMurray JJV", "2019"))
        self.assertEqual((cit.location, cit.reference_text), ("line 3", DAPA_NO_ID_REF))
        self.assertEqual([u.location for u in unlinked], ["line 4"])

    def test_searched_entries_keep_their_place_in_the_reference_list(self) -> None:
        segments = [
            ("line 1", "## References"),
            (
                "line 2",
                "1. Zinman B. Empagliflozin outcomes in diabetes. N Engl J Med. 2015. doi:10.1056/NEJMoa1504720",
            ),
            ("line 3", DAPA_NO_ID_REF),
            ("line 4", "3. Packer M. Empagliflozin in heart failure. N Engl J Med. 2020. doi:10.1056/NEJMoa2022190"),
        ]
        cited = scan.extract_citations_from_segments(segments)
        searched, _ = scan.split_searchable_entries(scan.extract_unlinked_bibliography_entries(segments))
        merged = scan.merge_in_document_order(segments, cited, searched)
        self.assertEqual([c.location for c in merged], ["line 2", "line 3", "line 4"])

    def test_scan_verifies_a_vancouver_list_without_dois(self) -> None:
        md = (
            "Dapagliflozin reduced the primary outcome to 16.3% [1].\n\n"
            f"## References\n{DAPA_NO_ID_REF}\n{UNPARSEABLE_REF}\n"
        )
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "manuscript.md"
            p.write_text(md, encoding="utf-8")
            buf = io.StringIO()
            with mock.patch.object(rv, "HttpClient", return_value=_dapa_found_http()), mock.patch("sys.stdout", buf):
                rv.main(["scan", str(p), "--json"])
        out = json.loads(buf.getvalue())
        self.assertEqual(out["resolved_by_search_count"], 1)
        self.assertEqual(out["unlinked_bibliography_count"], 1)
        (res,) = out["results"]
        self.assertEqual(res["input"]["doi"], "10.1056/NEJMoa1911303")
        self.assertIn("IDENTIFIER_RESOLVED_BY_SEARCH", [d["code"] for d in res["diagnostics"]])
        (sentence,) = res["input"]["citing_sentences"]
        self.assertEqual(sentence["cited_keys"], ["10.1056/nejmoa1911303"])
        self.assertEqual(len(res["sentence_checks"]), 1)


if __name__ == "__main__":
    unittest.main()
