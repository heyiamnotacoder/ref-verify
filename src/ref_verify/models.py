"""Data records shared across the pipeline: inputs, resolved papers, diagnostics, and results."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

SEVERITY_ORDER = {"CRITICAL": 0, "ERROR": 1, "WARNING": 2, "INFO": 3}


@dataclass
class Diagnostic:
    code: str
    severity: str  # CRITICAL | ERROR | WARNING | INFO
    message: str
    expected: str = ""
    actual: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    hint: str = ""
    fix: str = ""


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
    doi: Optional[str] = None
    pmid: Optional[str] = None
    pmcid: Optional[str] = None
    title: Optional[str] = None
    authors: List[Author] = field(default_factory=list)
    published_print: Optional[int] = None
    published_online: Optional[int] = None
    issued_year: Optional[int] = None
    journal_full: Optional[str] = None
    journal_abbrev: Optional[str] = None
    abstract: Optional[str] = None
    is_open_access: Optional[bool] = None
    retracted: bool = False
    retraction_reasons: List[str] = field(default_factory=list)
    retraction_notice_ids: List[str] = field(default_factory=list)
    is_retraction_notice: bool = False
    expression_of_concern: List[str] = field(default_factory=list)
    correction_notices: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)

    def all_years(self) -> List[int]:
        yrs: List[int] = []
        for y in (self.published_print, self.published_online, self.issued_year):
            if y is not None and y not in yrs:
                yrs.append(y)
        return yrs

    def best_year(self) -> Optional[int]:
        return self.published_print or self.issued_year or self.published_online

    def journal_variants(self) -> List[str]:
        out: List[str] = []
        for j in (self.journal_full, self.journal_abbrev):
            if j and j not in out:
                out.append(j)
        return out


@dataclass
class CitationInput:
    doi: Optional[str] = None
    pmid: Optional[str] = None
    title: Optional[str] = None
    author: Optional[str] = None
    year: Optional[str] = None
    journal: Optional[str] = None
    claim: Optional[str] = None
    label: Optional[str] = None
    location: Optional[str] = None
    fulltext: bool = False


@dataclass
class ClaimReport:
    claim: str
    extracted_numbers: List[str] = field(default_factory=list)
    matched_numbers: List[str] = field(default_factory=list)
    missing_numbers: List[str] = field(default_factory=list)
    extracted_keywords: List[str] = field(default_factory=list)
    matched_keywords: List[str] = field(default_factory=list)
    abstract_numbers: List[str] = field(default_factory=list)
    matched_sentences: List[str] = field(default_factory=list)
    fulltext_excerpts: List[str] = field(default_factory=list)
    fulltext_status: str = "not_requested"
    abstract_available: bool = False


@dataclass
class VerificationResult:
    input: CitationInput
    status: str  # PASS | WARN | FAIL
    resolved: Optional[PaperRecord] = None
    diagnostics: List[Diagnostic] = field(default_factory=list)
    claim_report: Optional[ClaimReport] = None
    did_you_mean: List[Dict[str, Any]] = field(default_factory=list)

    def has_severity(self, *severities: str) -> bool:
        return any(d.severity in severities for d in self.diagnostics)


@dataclass
class FulltextResult:
    paragraphs: List[str] = field(default_factory=list)
    source: Optional[str] = None
    error: Optional[str] = None
    is_transient: bool = False
    missing_body: bool = False
