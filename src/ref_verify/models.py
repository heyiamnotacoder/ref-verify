"""Data records shared across the pipeline: inputs, resolved papers, diagnostics, and results."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SEVERITY_ORDER = {"CRITICAL": 0, "ERROR": 1, "WARNING": 2, "INFO": 3}


@dataclass
class Diagnostic:
    code: str
    severity: str  # CRITICAL | ERROR | WARNING | INFO
    message: str
    expected: str = ""
    actual: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    hint: str = ""
    fix: str = ""


@dataclass
class HttpResponse:
    """Outcome of one GET made by ``HttpClient``; ``status == 0`` means a network-level failure."""

    status: int
    body: str
    url: str
    error: str | None = None


@dataclass
class Author:
    family: str
    given: str = ""
    initials: str = ""
    collective: str = ""

    @property
    def display(self) -> str:
        if self.collective:
            return self.collective
        if self.initials:
            return f"{self.family} {self.initials}".strip()
        if self.given:
            inits = "".join(p[0] for p in re.split(r"[\s-]+", self.given) if p)
            return f"{self.family} {inits}".strip()
        return self.family


@dataclass
class PaperRecord:
    doi: str | None = None
    pmid: str | None = None
    pmcid: str | None = None
    title: str | None = None
    authors: list[Author] = field(default_factory=list)
    published_print: int | None = None
    published_online: int | None = None
    issued_year: int | None = None
    journal_full: str | None = None
    journal_abbrev: str | None = None
    abstract: str | None = None
    is_open_access: bool | None = None
    retracted: bool = False
    retraction_reasons: list[str] = field(default_factory=list)
    retraction_notice_ids: list[str] = field(default_factory=list)
    is_retraction_notice: bool = False
    expression_of_concern: list[str] = field(default_factory=list)
    correction_notices: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def all_years(self) -> list[int]:
        yrs: list[int] = []
        for y in (self.published_print, self.published_online, self.issued_year):
            if y is not None and y not in yrs:
                yrs.append(y)
        return yrs

    def best_year(self) -> int | None:
        return self.published_print or self.issued_year or self.published_online

    def journal_variants(self) -> list[str]:
        out: list[str] = []
        for j in (self.journal_full, self.journal_abbrev):
            if j and j not in out:
                out.append(j)
        return out


@dataclass
class CitationInput:
    doi: str | None = None
    pmid: str | None = None
    title: str | None = None
    author: str | None = None
    year: str | None = None
    journal: str | None = None
    claim: str | None = None
    label: str | None = None
    location: str | None = None
    fulltext: bool = False


@dataclass
class ClaimReport:
    claim: str
    extracted_numbers: list[str] = field(default_factory=list)
    matched_numbers: list[str] = field(default_factory=list)
    missing_numbers: list[str] = field(default_factory=list)
    extracted_keywords: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    abstract_numbers: list[str] = field(default_factory=list)
    matched_sentences: list[str] = field(default_factory=list)
    fulltext_excerpts: list[str] = field(default_factory=list)
    fulltext_status: str = "not_requested"
    abstract_available: bool = False


@dataclass
class VerificationResult:
    input: CitationInput
    status: str  # PASS | WARN | FAIL
    resolved: PaperRecord | None = None
    diagnostics: list[Diagnostic] = field(default_factory=list)
    claim_report: ClaimReport | None = None
    did_you_mean: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FulltextResult:
    paragraphs: list[str] = field(default_factory=list)
    source: str | None = None
    error: str | None = None
    is_transient: bool = False
    missing_body: bool = False


@dataclass
class UnlinkedEntry:
    """A bibliography entry found by `scan` that carries no DOI or PMID."""

    location: str
    text: str
