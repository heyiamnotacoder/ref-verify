"""Cohort and subgroup grounding: flags claims attributed to the wrong population or subgroup."""

from __future__ import annotations

import functools
import re
from collections.abc import Sequence
from dataclasses import dataclass

from ref_verify.lexicons import (
    COHORT_GENERIC_WORDS,
    COHORT_GROUP_PARENTS,
    COHORT_OPPOSITE_PAIRS,
    COHORT_SYNONYM_GROUPS,
    NEGATED_COHORT_ADJECTIVES,
    NON_COHORT_MODIFIER_WORDS,
    NON_COHORT_WITH_WORDS,
    PHARMA_STEMS,
    POSITIVE_COHORT_ADJECTIVES,
    TREATMENT_ARM_WORDS,
)
from ref_verify.models import Diagnostic, PaperRecord
from ref_verify.quant import extract_claim_numbers, normalize_for_quant, quant_token_in_norm_text, split_sentences
from ref_verify.text import normalize_text, truncate


def _alias_key(alias: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", alias.lower())).strip()


_ALIAS_TO_GROUP: dict[str, str] = {
    _alias_key(alias): grp for grp, aliases in COHORT_SYNONYM_GROUPS.items() for alias in aliases
}
_COHORT_PHRASE_TO_GROUP: dict[str, str] = {k: g for k, g in _ALIAS_TO_GROUP.items() if " " in k}
_COHORT_SINGLE_TO_GROUP: dict[str, str] = {k: g for k, g in _ALIAS_TO_GROUP.items() if " " not in k}


# Ordered longest-first so "type 2 diabetes" matches before "diabetes"
_COHORT_MULTI_PHRASES: tuple[tuple[str, str, re.Pattern[str]], ...] = tuple(
    (
        phrase,
        grp,
        re.compile(rf"\b{re.escape(phrase)}\b", re.IGNORECASE),
    )
    for phrase, grp in sorted(_COHORT_PHRASE_TO_GROUP.items(), key=lambda kv: -len(kv[0]))
)


def _symmetric_opposites(pairs: Sequence[tuple[str, str]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for a, b in pairs:
        out.setdefault(a, set()).add(b)
        out.setdefault(b, set()).add(a)
    return out


_COHORT_OPPOSITES_MAP = _symmetric_opposites(COHORT_OPPOSITE_PAIRS)


_OVERALL_ENDPOINT_FOLLOW = (
    r"(?!\s+(?:survival|mortality|death|response|remission|rate|rates|risk|incidence|"
    r"prevalence|score|scores|change|improvement|benefit|efficacy|safety|adverse|"
    r"toxicity|burden|mean|median|effect|hazard|odds|relative))"
)


OVERALL_SCOPE_RE = re.compile(
    rf"\b(?:"
    rf"overall{_OVERALL_ENDPOINT_FOLLOW}|"
    rf"(?:entire|full|whole|total|general|combined|pooled|primary|unselected|broad)\s+"
    rf"(?:cohort|population|trial|study|sample|patients|participants)|"
    rf"(?:across\s+all|all)\s+(?:randomized\s+|randomised\s+|enrolled\s+|study\s+|trial\s+)?"
    rf"(?:patients|participants|subjects|individuals)|"
    rf"intent(?:ion)?[- ]to[- ]treat|ITT\s+(?:cohort|population|analysis)"
    rf")\b",
    re.IGNORECASE,
)


SUBGROUP_CUE_RE = re.compile(
    r"\b(?:sub-?groups?|sub-?cohorts?|sub-?populations?|strat(?:um|a|ified)|post[- ]?hoc)\b",
    re.IGNORECASE,
)


SUBGROUP_INTRO_RE = re.compile(
    r"^(?:in|for)\s+(?:the\s+)?(?:prespecified\s+|pre-specified\s+|post[- ]?hoc\s+|exploratory\s+)?"
    r"sub-?group\s+analys(?:is|es)\b",
    re.IGNORECASE,
)


SECTION_HEADER_RE = re.compile(
    r"^(?:BACKGROUND|OBJECTIVES?|PURPOSE|METHODS?|PATIENTS?|DESIGN|SETTING|PARTICIPANTS?|"
    r"RESULTS?|FINDINGS?|CONCLUSIONS?|INTERPRETATION|TRIAL\s+REGISTRATION)\s*:\s*",
    re.IGNORECASE,
)


ENROLLMENT_SECTION_RE = re.compile(
    r"^(?:BACKGROUND|OBJECTIVES?|PURPOSE|METHODS?|PATIENTS?|DESIGN|SETTING|PARTICIPANTS?)\s*:",
    re.IGNORECASE,
)


RESULTS_SECTION_RE = re.compile(
    r"^(?:RESULTS?|FINDINGS?|CONCLUSIONS?|INTERPRETATION)\s*:",
    re.IGNORECASE,
)


ENROLLMENT_CUE_RE = re.compile(
    r"\b(?:randomly\s+assigned|random(?:ized|ised)\s+\d|we\s+(?:enrolled|recruited|assigned|included|studied|evaluated)|"
    r"eligible\s+patients|phase\s+[1-4]\b|double[- ]blind|placebo[- ]controlled)\b",
    re.IGNORECASE,
)


_POP_NOUN_RE = (
    r"(?:patients?|participants?|subjects?|individuals?|persons?|people|"
    r"adults?|children|infants?|neonates?|adolescents?|women|men|females?|males?|those)"
)


_COHORT_WITH_RE = re.compile(
    rf"\b(?:(?P<prep>in|among|for|within|of)\s+(?:the\s+)?)?"
    rf"(?:(?:prespecified|pre-specified|post[- ]?hoc|exploratory)\s+)?"
    rf"(?P<sg>sub-?group\s+of\s+)?"
    rf"{_POP_NOUN_RE}\s+(?P<pol>with|without|having|lacking)\s+"
    rf"(?P<tail>[^,;.()\[\]]+)",
    re.IGNORECASE,
)


_COHORT_TAIL_CUT_RE = re.compile(
    r"\s+(?:who\b|were\b|was\b|had\b|have\b|has\b|achieved\b|showed\b|experienced\b|"
    r"received\b|receiving\b|treated\b|assigned\b|randomized\b|randomised\b|enrolled\b|"
    r"to\s+receive\b|versus\b|vs\.?\b|compared\b|"
    r"the\s+(?:primary|secondary|hazard|odds|relative|risk|rate|mortality|incidence|median|mean|HR|OR|RR)\b)",
    re.IGNORECASE,
)


_COHORT_SUBGROUP_MOD_RE = re.compile(
    r"\b(?:(?:in|among|for|within)\s+(?:the\s+)?)?"
    r"([A-Za-z0-9≥≤<>%+-]+(?:\s+[A-Za-z0-9≥≤<>%+-]+){0,3})\s+"
    r"(?:sub-?group|sub-?cohort|sub-?population)\b",
    re.IGNORECASE,
)


_COHORT_STANDALONE_DEMO_RE = re.compile(
    r"\b(?:in|among|for|within)\s+(?:the\s+)?"
    r"(women|females?|men|males?|children|infants?|neonates?|adolescents?|adults?|"
    r"elderly|pediatric|paediatric|outpatients?|inpatients?|smokers?|non-?smokers?)"
    r"(?:\s+(?:patients?|participants?|subjects?|individuals?|cohort|population|group|sub-?group))?\b",
    re.IGNORECASE,
)


_ALL_COHORT_ADJECTIVES = sorted(
    set(NEGATED_COHORT_ADJECTIVES.keys()) | set(POSITIVE_COHORT_ADJECTIVES),
    key=len,
    reverse=True,
)


_COHORT_ADJ_POP_RE = re.compile(
    r"\b(?:(?:in|among|for|within)\s+(?:the\s+)?)?("
    + "|".join(re.escape(a) for a in _ALL_COHORT_ADJECTIVES)
    + r")\s+(patients?|participants?|subjects?|individuals?|adults?|children|women|men|cohort|population|sub-?group|group)\b",
    re.IGNORECASE,
)


_COHORT_PREP_MOD_POP_RE = re.compile(
    r"\b(?:in|among|for|within)\s+(?:the\s+)?([A-Za-z][A-Za-z-]{2,})\s+"
    r"(patients?|participants?|subjects?|individuals?|adults?|children|women|men)\b",
    re.IGNORECASE,
)


_COHORT_STAGE_CLASS_RE = re.compile(
    r"\b(?:in|among|for|within)\s+(?:the\s+)?(?:(?:patients?|participants?|subjects?|those)\s+(?:with|in)\s+)?"
    r"((?:nyha\s+class|child[- ]pugh\s+class|class|stage|grade)\s+"
    r"[ivx1-4a-c]+(?:\s*(?:/|or|to|-)\s*[ivx1-4a-c]+)*)\b",
    re.IGNORECASE,
)


_STAGE_CLASS_TOKEN_RE = re.compile(
    r"\b(?:(nyha)\s+)?(class|stage|grade)\s+([ivx1-4a-c]+(?:\s*(?:/|or|to|-)\s*[ivx1-4a-c]+)*)\b",
    re.IGNORECASE,
)


_STANDALONE_CONDITION_PHRASES = sorted(
    {
        p
        for grp in (
            "t2dm",
            "t1dm",
            "diabetes",
            "hfref",
            "hfpef",
            "heart_failure",
            "ckd",
            "esrd",
            "afib",
            "hypertension",
            "cad",
            "copd",
            "atopic_dermatitis",
            "ra",
            "ibd",
            "nsclc",
            "hcc",
            "aki",
            "vte",
            "cirrhosis",
        )
        for p in COHORT_SYNONYM_GROUPS[grp]
        if len(p) >= 3
    },
    key=len,
    reverse=True,
)


_COHORT_KNOWN_COND_RE = re.compile(
    r"\b(?:in|among|for|within)\s+(?:the\s+)?("
    + "|".join(re.escape(p) for p in _STANDALONE_CONDITION_PHRASES)
    + r")\b",
    re.IGNORECASE,
)


# "hospitalization for heart failure", "admission for COPD": an outcome named after a condition, not a population.
# CLAIM_COHORT_MISMATCH ``details["reason"]`` values. The first two mean the claim itself names a
# population the source contradicts; the last two mean the claim omits the source's subgroup.
REASON_COHORT_CONFLICT = "cohort_conflict"
REASON_COHORT_NOT_IN_SOURCE = "cohort_not_in_source_context"
REASON_SUBGROUP_AS_OVERALL = "subgroup_attributed_to_overall"
REASON_SUBGROUP_OMITTED = "subgroup_qualifier_omitted"
NAMED_POPULATION_REASONS = frozenset({REASON_COHORT_CONFLICT, REASON_COHORT_NOT_IN_SOURCE})

# Only outcome *nouns*: "patients hospitalized for HFpEF" or "approved for CKD" still name a population.
_OUTCOME_BEFORE_FOR_RE = re.compile(
    r"\b(?:hospitali[sz]ations?|(?:re)?admissions?|visits?|deaths?|mortality|events?)\s*$", re.IGNORECASE
)


# "a 35% reduction in heart failure hospitalization": the condition names an outcome, not a population.
_OUTCOME_AFTER_COND_RE = re.compile(
    r"\s*(?:-related\s+)?(?:hospitali[sz]ations?|(?:re)?admissions?|visits?|events?|deaths?|mortality|"
    r"exacerbations?|outcomes?)\b",
    re.IGNORECASE,
)


_AGE_LEFT_BOUND = r"(?<![A-Za-z0-9.])"


_AGE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "range",
        re.compile(r"\baged?\s+(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)\s*(?:years?|yrs?)?\b", re.IGNORECASE),
    ),
    (
        ">=",
        re.compile(
            rf"{_AGE_LEFT_BOUND}(?:aged?\s+)?(?:>=|≥)\s*(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\b"
            r"|\b(?:aged?\s+)?at\s+least\s+(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\b"
            r"|\baged?\s*(?:>=|≥)\s*(\d+(?:\.\d+)?)\b"
            r"|\b(?:aged?\s+)?(\d+(?:\.\d+)?)\s*(?:years?|yrs?)?\s*(?:or\s+older|and\s+older)\b",
            re.IGNORECASE,
        ),
    ),
    (
        ">",
        re.compile(
            rf"{_AGE_LEFT_BOUND}(?:aged?\s+)?(?<![<>])>\s*(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\b"
            r"|\b(?:aged?\s+)?(?:older\s+than|over)\s+(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\b"
            r"|\baged?\s*>\s*(\d+(?:\.\d+)?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "<=",
        re.compile(
            rf"{_AGE_LEFT_BOUND}(?:aged?\s+)?(?:<=|≤)\s*(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\b"
            r"|\baged?\s*(?:<=|≤)\s*(\d+(?:\.\d+)?)\b"
            r"|\b(?:aged?\s+)?(\d+(?:\.\d+)?)\s*(?:years?|yrs?)?\s*(?:or\s+younger|and\s+younger)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "<",
        re.compile(
            rf"{_AGE_LEFT_BOUND}(?:aged?\s+)?(?<![<>])<\s*(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\b"
            r"|\b(?:aged?\s+)?(?:younger\s+than|under|below|less\s+than)\s+(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\b"
            r"|\baged?\s*<\s*(\d+(?:\.\d+)?)\b",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True)
class AgeBound:
    op: str  # ">=" | ">" | "<=" | "<" | "range"
    lo: float
    hi: float | None = None

    def to_interval(self) -> tuple[float, bool, float, bool]:
        if self.op == "range" and self.hi is not None:
            return (self.lo, True, self.hi, True)
        if self.op == ">=":
            return (self.lo, True, float("inf"), False)
        if self.op == ">":
            return (self.lo, False, float("inf"), False)
        if self.op == "<=":
            return (float("-inf"), False, self.lo, True)
        return (float("-inf"), False, self.lo, False)

    def conflicts_with(self, other: AgeBound) -> bool:
        a_lo, a_lo_inc, a_hi, a_hi_inc = self.to_interval()
        b_lo, b_lo_inc, b_hi, b_hi_inc = other.to_interval()
        if a_hi < b_lo or (a_hi == b_lo and not (a_hi_inc and b_lo_inc)):
            return True
        if b_hi < a_lo or (b_hi == a_lo and not (b_hi_inc and a_lo_inc)):
            return True
        return False

    def matches_demographic_groups(self, groups: set[str]) -> bool:
        lo, _, hi, _ = self.to_interval()
        if lo >= 60 and hi == float("inf") and "elderly" in groups:
            return True
        if hi <= 18 and ("pediatric" in groups or "adolescents" in groups):
            return True
        return False


@dataclass(frozen=True)
class CohortTerms:
    groups: tuple[str, ...]
    core_terms: tuple[str, ...]

    def __bool__(self) -> bool:
        return bool(self.groups or self.core_terms)


@dataclass(frozen=True)
class CohortQualifier:
    raw: str
    polarity: str  # "with" | "without"
    terms: CohortTerms
    sub_conditions: tuple[CohortTerms, ...] = ()
    age_bounds: AgeBound | None = None

    @property
    def groups(self) -> tuple[str, ...]:
        return self.terms.groups

    @property
    def core_terms(self) -> tuple[str, ...]:
        return self.terms.core_terms


@dataclass(frozen=True)
class CohortContext:
    text: str
    qualifiers: tuple[CohortQualifier, ...]


@dataclass(frozen=True)
class RawClauseEntry:
    text: str
    is_enrollment: bool
    has_explicit_sg: bool
    from_fulltext: bool


@dataclass
class SourceClause:
    text: str
    norm_low: str
    norm_no_commas: str
    is_enrollment: bool
    is_subgroup: bool
    qualifiers: tuple[CohortQualifier, ...]
    from_fulltext: bool = False


@dataclass(frozen=True)
class SubgroupIssue:
    reason: str
    token: str
    source_cohorts: tuple[str, ...]
    source_clauses: tuple[str, ...]


def _is_drug_word(word: str) -> bool:
    low = word.lower()
    return len(low) >= 6 and any(low.endswith(stem) for stem in PHARMA_STEMS)


def _singularize_cohort_word(word: str) -> str:
    low = word.lower()
    if low in ("women", "men", "diabetes", "cirrhosis", "metastases", "metastasis"):
        return low
    if len(low) > 4 and low.endswith("s") and not low.endswith("ss"):
        return low[:-1]
    return low


def _groups_overlap_for_polarity(groups_a: Sequence[str], groups_b: Sequence[str]) -> bool:
    for ga in groups_a:
        pa = COHORT_GROUP_PARENTS.get(ga)
        for gb in groups_b:
            pb = COHORT_GROUP_PARENTS.get(gb)
            if ga == gb or pa == gb or pb == ga:
                return True
    return False


def _extract_stage_class_tokens(norm_text: str) -> list[str]:
    tokens: list[str] = []
    for m in _STAGE_CLASS_TOKEN_RE.finditer(norm_text):
        prefix = m.group(1).lower() + "_" if m.group(1) else ""
        kind = m.group(2).lower()
        levels = re.findall(r"[ivx1-4a-c]+", m.group(3).lower())
        for lvl in levels:
            tok = f"{prefix}{kind}_{lvl}"
            if tok not in tokens:
                tokens.append(tok)
    return tokens


@functools.lru_cache(maxsize=8192)
def _extract_groups_and_core_terms(phrase: str) -> CohortTerms:
    """Cached: the same clause and context texts are re-examined many times per claim."""
    return _extract_groups_and_core_terms_uncached(phrase)


def _extract_groups_and_core_terms_uncached(phrase: str) -> CohortTerms:
    """Map a cohort phrase into canonical synonym groups and remaining core tokens."""
    norm = re.sub(r"[^a-z0-9≥≤<>=%.+/\-]", " ", normalize_text(phrase).lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    groups: list[str] = []
    consumed_spans: list[tuple[int, int]] = []

    for _, grp, pat in _COHORT_MULTI_PHRASES:
        for m in pat.finditer(norm):
            if any(s <= m.start() and m.end() <= e for s, e in consumed_spans):
                continue
            consumed_spans.append((m.start(), m.end()))
            if grp not in groups:
                groups.append(grp)

    # Extract structured stage/class/grade tokens (e.g. "NYHA class III/IV" -> "nyha_class_iii", "nyha_class_iv")
    core_terms: list[str] = _extract_stage_class_tokens(norm)
    for m in _STAGE_CLASS_TOKEN_RE.finditer(norm):
        consumed_spans.append((m.start(), m.end()))

    # Mask consumed spans before single-word matching
    chars = list(norm)
    for s, e in consumed_spans:
        for idx in range(s, e):
            chars[idx] = " "
    rem = "".join(chars)

    for raw_tok in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", rem):
        low = raw_tok.lower()
        if low in NEGATED_COHORT_ADJECTIVES:
            target = NEGATED_COHORT_ADJECTIVES[low]
            if target in COHORT_SYNONYM_GROUPS:
                if target not in groups:
                    groups.append(target)
            elif target not in core_terms:
                core_terms.append(target)
            continue
        sing = _singularize_cohort_word(low)
        grp_hit = _COHORT_SINGLE_TO_GROUP.get(low) or _COHORT_SINGLE_TO_GROUP.get(sing)
        if grp_hit:
            if grp_hit not in groups:
                groups.append(grp_hit)
            continue
        if (
            low in COHORT_GENERIC_WORDS
            or sing in COHORT_GENERIC_WORDS
            or low in TREATMENT_ARM_WORDS
            or low in NON_COHORT_WITH_WORDS
            or _is_drug_word(low)
        ):
            continue
        if len(low) <= 1 and not low.isdigit():
            continue
        if sing not in core_terms:
            core_terms.append(sing)

    # Include numeric threshold qualifiers like ">=50%" or "<1%" if present (e.g. PD-L1 >=50%)
    for th in re.findall(r"(?:>=|<=|≥|≤|<|>)\s*\d+(?:\.\d+)?%?", norm):
        compact = th.replace(" ", "").replace("≥", ">=").replace("≤", "<=")
        if compact not in core_terms:
            core_terms.append(compact)

    return CohortTerms(tuple(groups), tuple(core_terms))


def _split_sub_conditions(phrase: str) -> list[str]:
    """Split compound 'X and Y' phrases unless the full phrase is a single known multi-word cohort."""
    norm = re.sub(r"[^a-z0-9\s]", " ", normalize_text(phrase).lower())
    norm = re.sub(r"\s+", " ", norm).strip()
    if norm in _COHORT_PHRASE_TO_GROUP:
        return [phrase]
    # Protect known multi-word phrases containing 'and' (e.g. 'heart failure and reduced ejection fraction')
    for mphrase, _, _ in _COHORT_MULTI_PHRASES:
        if " and " in mphrase and mphrase in norm:
            return [phrase]
    parts = [p.strip() for p in re.split(r"\band\b", phrase, flags=re.IGNORECASE) if p.strip()]
    return parts if len(parts) > 1 else [phrase]


@functools.lru_cache(maxsize=8192)
def _extract_age_bounds(text: str) -> tuple[tuple[str, AgeBound], ...]:
    """Cached (immutable result): the same clause texts are re-examined many times per claim."""
    return tuple(_extract_age_bounds_uncached(text))


def _extract_age_bounds_uncached(text: str) -> list[tuple[str, AgeBound]]:
    out: list[tuple[str, AgeBound]] = []
    norm = normalize_text(text)
    for op, pat in _AGE_PATTERNS:
        for m in pat.finditer(norm):
            if op == "range":
                lo, hi = float(m.group(1)), float(m.group(2))
                out.append((m.group(0).strip(), AgeBound("range", min(lo, hi), max(lo, hi))))
            else:
                val_str = next((g for g in m.groups() if g is not None), None)
                if val_str is not None:
                    out.append((m.group(0).strip(), AgeBound(op, float(val_str))))
    return out


def _make_cohort_qualifier(
    raw: str,
    polarity: str,
    phrase: str,
    age_bounds: AgeBound | None = None,
) -> CohortQualifier | None:
    terms = _extract_groups_and_core_terms(phrase)
    if not terms and age_bounds is None:
        return None
    sub_conds: list[CohortTerms] = []
    for sub in _split_sub_conditions(phrase):
        st = _extract_groups_and_core_terms(sub)
        if st:
            sub_conds.append(st)
    return CohortQualifier(
        raw=re.sub(r"\s+", " ", raw).strip(" ,.;:"),
        polarity=polarity,
        terms=terms,
        sub_conditions=tuple(sub_conds),
        age_bounds=age_bounds,
    )


@functools.lru_cache(maxsize=8192)
def extract_cohort_qualifiers(text: str) -> tuple[CohortQualifier, ...]:
    """Cached (immutable result): the same clause texts are re-examined many times per claim."""
    return tuple(_extract_cohort_qualifiers_uncached(text))


def _extract_cohort_qualifiers_uncached(text: str) -> list[CohortQualifier]:
    """Extract structured patient cohort/subgroup qualifiers from a claim or source clause."""
    norm = SECTION_HEADER_RE.sub("", normalize_text(text)).strip()
    if not norm:
        return []
    qualifiers: list[CohortQualifier] = []
    seen_keys: set[tuple[str, CohortTerms, AgeBound | None]] = set()

    def _add(q: CohortQualifier | None) -> None:
        if q is None:
            return
        key = (q.polarity, q.terms, q.age_bounds)
        if key not in seen_keys:
            seen_keys.add(key)
            qualifiers.append(q)

    # 1. "patients with/without <condition>" (only when cohort-scoped, not bare outcome counts like "42 patients with ...")
    for m in _COHORT_WITH_RE.finditer(norm):
        prep = (m.group("prep") or "").lower()
        has_sg = bool(m.group("sg"))
        before = norm[: m.start()].rstrip()
        if not prep and not has_sg:
            # Reject outcome-count patterns ("42 patients with serious adverse events") and mid-clause bare noun phrases
            if re.search(r"\d$", before):
                continue
            if before and not re.search(r"(?:^|[,;]\s*|\b(?:whereas|while|and|however))$", before, re.IGNORECASE):
                continue
        elif prep == "of" and not has_sg:
            continue

        pol_word = m.group("pol").lower()
        polarity = "without" if pol_word in ("without", "lacking") else "with"
        tail = _COHORT_TAIL_CUT_RE.split(m.group("tail"), maxsplit=1)[0].strip()
        if not tail:
            continue
        tail_words = [w.lower() for w in re.findall(r"[A-Za-z]+", tail)]
        if tail_words and all(
            w in COHORT_GENERIC_WORDS or w in NON_COHORT_WITH_WORDS or w in TREATMENT_ARM_WORDS or _is_drug_word(w)
            for w in tail_words
        ):
            continue
        raw = f"{m.group(0).split(m.group('tail'), 1)[0]}{tail}".strip()
        raw = re.sub(r"^(?:in|among|for|within|of)\s+(?:the\s+)?", "", raw, flags=re.IGNORECASE)
        _add(_make_cohort_qualifier(raw, polarity, tail))

    # 2. "<modifier> subgroup/subcohort/subpopulation"
    for m in _COHORT_SUBGROUP_MOD_RE.finditer(norm):
        mod = m.group(1).strip()
        mod_tokens = mod.split()
        while mod_tokens and mod_tokens[0].lower() in (
            COHORT_GENERIC_WORDS | TREATMENT_ARM_WORDS | {"in", "among", "for", "within", "the", "a", "an", "of"}
        ):
            mod_tokens.pop(0)
        cleaned_mod = " ".join(mod_tokens).strip()
        if not cleaned_mod:
            continue
        pol = "without" if any(t.lower() in NEGATED_COHORT_ADJECTIVES for t in mod_tokens) else "with"
        _add(_make_cohort_qualifier(f"{cleaned_mod} subgroup", pol, cleaned_mod))

    # 3. "<adj> patients/adults/women/..." (known clinical/demographic adjectives)
    for m in _COHORT_ADJ_POP_RE.finditer(norm):
        adj = m.group(1).lower()
        noun = m.group(2)
        pol = "without" if adj in NEGATED_COHORT_ADJECTIVES else "with"
        phrase = adj if pol == "without" else f"{adj} {noun}"
        _add(_make_cohort_qualifier(f"{adj} {noun}", pol, phrase))

    # 4. General "in/among <modifier> patients/adults/women/..." for unlisted cohorts (e.g. "In Asian patients")
    for m in _COHORT_PREP_MOD_POP_RE.finditer(norm):
        mod = m.group(1)
        mod_low = mod.lower()
        noun = m.group(2)
        if (
            mod_low in COHORT_GENERIC_WORDS
            or mod_low in TREATMENT_ARM_WORDS
            or mod_low in NON_COHORT_WITH_WORDS
            or mod_low in NON_COHORT_MODIFIER_WORDS
            or mod_low.endswith("-treated")
            or _is_drug_word(mod_low)
        ):
            continue
        pol = "without" if mod_low in NEGATED_COHORT_ADJECTIVES else "with"
        phrase = mod_low if pol == "without" else f"{mod} {noun}"
        _add(_make_cohort_qualifier(f"{mod} {noun}", pol, phrase))

    # 5. Standalone demographic ("in women", "among children", "in elderly")
    for m in _COHORT_STANDALONE_DEMO_RE.finditer(norm):
        demo = m.group(1).lower()
        pol = "without" if demo in NEGATED_COHORT_ADJECTIVES else "with"
        raw = re.sub(r"^(?:in|among|for|within)\s+(?:the\s+)?", "", m.group(0), flags=re.IGNORECASE).strip()
        _add(_make_cohort_qualifier(raw, pol, demo))

    # 6. Clinical class/stage/grade ("In NYHA class III/IV", "In stage IV patients")
    for m in _COHORT_STAGE_CLASS_RE.finditer(norm):
        stage_phrase = m.group(1).strip()
        _add(_make_cohort_qualifier(stage_phrase, "with", stage_phrase))

    # 7. Shorthand "in <known condition>" (e.g. "in HFpEF", "in CKD", "in type 2 diabetes")
    for m in _COHORT_KNOWN_COND_RE.finditer(norm):
        if m.group(0).lower().startswith("for") and _OUTCOME_BEFORE_FOR_RE.search(norm[: m.start()]):
            continue
        if _OUTCOME_AFTER_COND_RE.match(norm, m.end()):  # "in heart failure hospitalization"
            continue
        cond = m.group(1).strip()
        q = _make_cohort_qualifier(cond, "with", cond)
        if q is not None and not any(existing.terms == q.terms for existing in qualifiers):
            _add(q)

    # 8. Age thresholds ("patients >=65 years", "patients <65 years", "aged 12 to 17 years")
    for raw_age, bounds in _extract_age_bounds(norm):
        _add(_make_cohort_qualifier(raw_age, "with", "", age_bounds=bounds))

    return qualifiers


def _split_top_level(text: str, sep_re: re.Pattern[str]) -> list[str]:
    """Split text at regex matches occurring outside parentheses/brackets."""
    parts: list[str] = []
    depth = 0
    cut_positions: list[tuple[int, int]] = []
    matches = list(sep_re.finditer(text))
    if not matches:
        return [text]

    match_idx = 0
    for i, ch in enumerate(text):
        while match_idx < len(matches) and matches[match_idx].start() < i:
            match_idx += 1
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        elif depth == 0 and match_idx < len(matches) and matches[match_idx].start() == i:
            cut_positions.append((matches[match_idx].start(), matches[match_idx].end()))
            match_idx += 1

    if not cut_positions:
        return [text]

    prev = 0
    for start, end in cut_positions:
        seg = text[prev:start].strip()
        if seg:
            parts.append(seg)
        # Keep leading contrastive word (e.g. "whereas") attached to the right clause if matched
        kept = text[start:end].lstrip(" ,;")
        prev = end - len(kept)
    tail = text[prev:].strip()
    if tail:
        parts.append(tail)
    return parts


_CLAUSE_SEP_RE = re.compile(
    r";\s+"
    r"|,\s*(?=whereas\b|while\b)"
    r"|,\s*and\s+(?=(?:in|among|for|within)\s+(?:the\s+)?(?:sub-?group|patients?|participants?|subjects?|those|women|men|adults?|children|adolescents?|elderly|pediatric|paediatric))"
    r"|,\s*compared\s+with\s+(?=(?:in|among|for)\b|\d)"
    r"|,?\s+(?:vs\.?|versus)\s+(?=(?:in|among|for)\s+(?:the\s+)?(?:sub-?group|patients?|participants?|subjects?|those|women|men|adults?|children|adolescents?|elderly|pediatric|paediatric))",
    re.IGNORECASE,
)


_COMPOUND_COHORT_SEPS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:,\s*and\s+|\s+and\s+)", re.IGNORECASE),
    re.compile(r",?\s+(?:vs\.?|versus)\s+", re.IGNORECASE),
    re.compile(r",\s+"),
)


def _split_compound_cohort_segment(seg: str) -> list[str]:
    """Split a multi-cohort segment on top-level conjunctions when every part carries both cohort & number."""
    parts = [seg]
    for sep_re in _COMPOUND_COHORT_SEPS:
        next_parts: list[str] = []
        for piece in parts:
            if len(extract_cohort_qualifiers(piece)) < 2:
                next_parts.append(piece)
                continue
            cand = _split_top_level(piece, sep_re)
            if len(cand) >= 2 and all(extract_cohort_qualifiers(p) and extract_claim_numbers(p) for p in cand):
                next_parts.extend(cand)
            else:
                next_parts.append(piece)
        parts = next_parts
    return parts


def _split_clauses(sentence: str) -> list[str]:
    """Split a sentence into top-level clauses without breaking parenthetical statistics."""
    initial = _split_top_level(sentence, _CLAUSE_SEP_RE)
    out: list[str] = []
    for seg in initial:
        out.extend(_split_compound_cohort_segment(seg))
    return out


def _context_has_negated_concept(text: str, terms: CohortTerms) -> bool:
    norm_low = normalize_text(text).lower()
    for adj, target in NEGATED_COHORT_ADJECTIVES.items():
        if (_groups_overlap_for_polarity((target,), terms.groups) or target in terms.core_terms) and re.search(
            rf"\b{re.escape(adj)}\b", norm_low
        ):
            return True
    for g in terms.groups:
        related_groups = {g}
        parent = COHORT_GROUP_PARENTS.get(g)
        if parent:
            related_groups.add(parent)
        for child, par in COHORT_GROUP_PARENTS.items():
            if par == g:
                related_groups.add(child)
        for rg in related_groups:
            for alias in COHORT_SYNONYM_GROUPS.get(rg, ()):
                if re.search(rf"\b(?:without|no|lacking)\s+{re.escape(alias)}\b", norm_low):
                    return True
    for t in terms.core_terms:
        if re.search(rf"\b(?:without|no|lacking)\s+{re.escape(t)}\b", norm_low):
            return True
    return False


def _context_contains_unit(
    ctx_groups: set[str],
    ctx_words: set[str],
    ctx_norm_low: str,
    unit: CohortTerms,
) -> bool:
    """Check whether a single cohort sub-condition's groups & core terms appear in a context."""
    for g in unit.groups:
        if g in ctx_groups:
            continue
        # Allow broader parent concept (e.g. 'diabetes') to match narrower child ('t2dm'/'t1dm')
        if any(COHORT_GROUP_PARENTS.get(cg) == g for cg in ctx_groups):
            continue
        return False
    for t in unit.core_terms:
        if t.startswith((">=", "<=", ">", "<")):
            compact_ctx = re.sub(r"\s+", "", ctx_norm_low.replace("≥", ">=").replace("≤", "<="))
            if t not in compact_ctx:
                return False
        elif t not in ctx_words and _singularize_cohort_word(t) not in ctx_words:
            return False
    return bool(unit)


def _qualifiers_conflict(claim_q: CohortQualifier, source_qs: Sequence[CohortQualifier]) -> bool:
    """Return True if claim_q directly contradicts source_qs (polarity, opposite cohort, or age)."""
    for sq in source_qs:
        # 1. Age threshold conflict
        if claim_q.age_bounds is not None and sq.age_bounds is not None:
            if claim_q.age_bounds.conflicts_with(sq.age_bounds):
                return True

        # 2. Same or parent/child concept, opposite polarity ("without diabetes" vs "with type 2 diabetes")
        same_concept = _groups_overlap_for_polarity(claim_q.groups, sq.groups) or bool(
            set(claim_q.core_terms) & set(sq.core_terms)
        )
        if same_concept and claim_q.polarity != sq.polarity:
            has_same_pol = any(
                (
                    _groups_overlap_for_polarity(claim_q.groups, other.groups)
                    or bool(set(claim_q.core_terms) & set(other.core_terms))
                )
                and other.polarity == claim_q.polarity
                for other in source_qs
            )
            if not has_same_pol:
                return True

        # 3. Mutually exclusive cohort pair (e.g. women vs men, HFpEF vs HFrEF, T1DM vs T2DM)
        if claim_q.polarity == "with" and sq.polarity == "with":
            for cg in claim_q.groups:
                opposites = _COHORT_OPPOSITES_MAP.get(cg, set())
                if opposites & set(sq.groups):
                    has_claim_grp = any(cg in other.groups and other.polarity == "with" for other in source_qs)
                    if not has_claim_grp:
                        return True
    return False


def _qualifier_supported_by_context(q: CohortQualifier, ctx: CohortContext) -> bool:
    """Return True if cohort qualifier q is supported by ctx without conflict."""
    if _qualifiers_conflict(q, ctx.qualifiers):
        return False

    ctx_terms = _extract_groups_and_core_terms(ctx.text)
    ctx_groups = set(ctx_terms.groups)
    ctx_norm_low = normalize_text(ctx.text).lower()
    ctx_words = {_singularize_cohort_word(w) for w in re.findall(r"[a-z0-9]+", ctx_norm_low)}
    ctx_words.update(re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)+", ctx_norm_low))
    ctx_words.update(ctx_terms.core_terms)

    # Check opposite-group conflict against raw context text too (e.g. Title has "Reduced Ejection Fraction")
    if q.polarity == "with":
        for cg in q.groups:
            opposites = _COHORT_OPPOSITES_MAP.get(cg, set())
            if (opposites & ctx_groups) and cg not in ctx_groups:
                return False

    # Age-bound check
    if q.age_bounds is not None:
        ctx_ages = [b for _, b in _extract_age_bounds(ctx.text)]
        if ctx_ages:
            return not all(q.age_bounds.conflicts_with(cb) for cb in ctx_ages)
        return q.age_bounds.matches_demographic_groups(ctx_groups)

    # Negated qualifier ("without diabetes" / "nondiabetic") must find negation in context
    if q.polarity == "without":
        has_neg_q = any(
            sq.polarity == "without"
            and (_groups_overlap_for_polarity(q.groups, sq.groups) or bool(set(q.core_terms) & set(sq.core_terms)))
            for sq in ctx.qualifiers
        )
        return has_neg_q or _context_has_negated_concept(ctx.text, q.terms)

    # Positive qualifier: every sub-condition must be present in context
    units = q.sub_conditions or (q.terms,)
    for unit in units:
        if not _context_contains_unit(ctx_groups, ctx_words, ctx_norm_low, unit):
            return False
    return True


def _collect_raw_clauses(
    blocks: Sequence[str],
    *,
    from_fulltext: bool,
    track_sections: bool,
) -> list[RawClauseEntry]:
    """Split text blocks into raw clauses while preserving structured section context and per-clause subgroup scope."""
    entries: list[RawClauseEntry] = []
    in_enrollment_section = False

    for block in blocks:
        for line in block.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if track_sections:
                if ENROLLMENT_SECTION_RE.match(stripped):
                    in_enrollment_section = True
                elif RESULTS_SECTION_RE.match(stripped):
                    in_enrollment_section = False

            for sent in split_sentences(stripped):
                if track_sections:
                    if ENROLLMENT_SECTION_RE.match(sent):
                        in_enrollment_section = True
                    elif RESULTS_SECTION_RE.match(sent):
                        in_enrollment_section = False
                sent_is_enroll = in_enrollment_section or bool(ENROLLMENT_CUE_RE.search(sent))
                sub_clauses = _split_clauses(sent)
                # Only inherit subgroup cue across sibling clauses when the sentence starts with "In subgroup analyses..."
                first_clean = SECTION_HEADER_RE.sub("", sub_clauses[0]).strip() if sub_clauses else ""
                inherit_sg = bool(SUBGROUP_INTRO_RE.match(first_clean))
                for clause in sub_clauses:
                    c_has_sg = bool(SUBGROUP_CUE_RE.search(clause)) or (
                        inherit_sg and not OVERALL_SCOPE_RE.search(clause)
                    )
                    entries.append(
                        RawClauseEntry(
                            text=clause,
                            is_enrollment=sent_is_enroll,
                            has_explicit_sg=c_has_sg,
                            from_fulltext=from_fulltext,
                        )
                    )
    return entries


def _build_source_clauses(
    rec: PaperRecord,
    fulltext_paragraphs: Sequence[str] = (),
) -> tuple[list[SourceClause], CohortContext]:
    """Parse abstract (+ optional OA fulltext) into clauses and detect subgroup scoping."""
    raw_entries: list[RawClauseEntry] = []
    if rec.abstract:
        raw_entries.extend(_collect_raw_clauses([rec.abstract], from_fulltext=False, track_sections=True))
    if fulltext_paragraphs:
        raw_entries.extend(_collect_raw_clauses(fulltext_paragraphs, from_fulltext=True, track_sections=False))

    # Build baseline overall-population context (title + enrollment clauses)
    overall_parts: list[str] = []
    if rec.title:
        overall_parts.append(rec.title)
    for entry in raw_entries:
        if not entry.from_fulltext and entry.is_enrollment and not entry.has_explicit_sg:
            overall_parts.append(entry.text)
    baseline_text = " ".join(overall_parts)
    baseline_ctx = CohortContext(
        text=baseline_text,
        qualifiers=tuple(extract_cohort_qualifiers(baseline_text)) if baseline_text else (),
    )

    total_abstract_clauses = sum(1 for e in raw_entries if not e.from_fulltext)
    clauses: list[SourceClause] = []

    for entry in raw_entries:
        c_quals = tuple(extract_cohort_qualifiers(entry.text))
        is_subgroup = False
        if entry.has_explicit_sg:
            is_subgroup = True
        elif (
            not entry.is_enrollment
            and c_quals
            and (bool(overall_parts) or total_abstract_clauses > 1 or entry.from_fulltext)
        ):
            if not baseline_text:
                other_clauses = [e.text for e in raw_entries if not e.from_fulltext and e.text != entry.text]
                if any(
                    not all(
                        _qualifier_supported_by_context(cq, CohortContext(oc, tuple(extract_cohort_qualifiers(oc))))
                        for cq in c_quals
                    )
                    for oc in other_clauses
                ):
                    is_subgroup = True
            else:
                if any(not _qualifier_supported_by_context(cq, baseline_ctx) for cq in c_quals):
                    is_subgroup = True

        norm_low, norm_no_commas = normalize_for_quant(entry.text)
        clauses.append(
            SourceClause(
                text=entry.text,
                norm_low=norm_low,
                norm_no_commas=norm_no_commas,
                is_enrollment=entry.is_enrollment,
                is_subgroup=is_subgroup,
                qualifiers=c_quals,
                from_fulltext=entry.from_fulltext,
            )
        )

    # Expand overall context with non-subgroup abstract clauses for general context checks
    main_parts = list(overall_parts)
    for c in clauses:
        if not c.from_fulltext and not c.is_subgroup and c.text not in main_parts:
            main_parts.append(c.text)
    overall_text = " ".join(main_parts)
    overall_ctx = CohortContext(
        text=overall_text,
        qualifiers=tuple(extract_cohort_qualifiers(overall_text)) if overall_text else (),
    )
    return clauses, overall_ctx


def _clause_context(clause: SourceClause, overall_ctx: CohortContext) -> CohortContext:
    return CohortContext(
        text=f"{clause.text} {overall_ctx.text}".strip(),
        qualifiers=clause.qualifiers + overall_ctx.qualifiers,
    )


def check_claim_cohort(
    claim: str,
    rec: PaperRecord,
    matched_numbers: Sequence[str],
    fulltext_paragraphs: Sequence[str] = (),
) -> Diagnostic | None:
    """Verify that matched quantitative tokens belong to the cohort/subgroup stated in the claim."""
    if not matched_numbers or (not rec.abstract and not fulltext_paragraphs):
        return None

    clauses, overall_ctx = _build_source_clauses(rec, fulltext_paragraphs=fulltext_paragraphs)
    if not clauses:
        return None

    norm_claim = normalize_text(claim)
    claim_qualifiers = tuple(extract_cohort_qualifiers(claim))
    claim_ctx = CohortContext(text=claim, qualifiers=claim_qualifiers)
    claim_asserts_overall = bool(OVERALL_SCOPE_RE.search(norm_claim))
    claim_mentions_subgroup = bool(SUBGROUP_CUE_RE.search(norm_claim))

    # Locate source clauses matching each matched quantitative token
    token_clauses: dict[str, list[SourceClause]] = {}
    for raw_tok in matched_numbers:
        clean_tok = re.sub(r"\s+\[fulltext\]$", "", raw_tok)
        hits = [c for c in clauses if quant_token_in_norm_text(clean_tok, c.norm_low, c.norm_no_commas)]
        if hits:
            token_clauses[clean_tok] = hits

    if not token_clauses:
        return None

    # Check 1: Tokens that ONLY appear in subgroup-scoped clauses
    subgroup_only_issues: list[SubgroupIssue] = []
    for tok, hits in token_clauses.items():
        if not all(c.is_subgroup for c in hits):
            continue

        any_clause_aligned = False
        has_explicit_conflict = False
        for c in hits:
            if claim_qualifiers and any(_qualifiers_conflict(cq, c.qualifiers) for cq in claim_qualifiers):
                has_explicit_conflict = True
                continue
            if claim_asserts_overall and not claim_mentions_subgroup:
                continue

            if c.qualifiers:
                claim_matches_sg = any(_qualifier_supported_by_context(sq, claim_ctx) for sq in c.qualifiers)
                if claim_qualifiers:
                    # Claim named explicit cohort(s): must match this subgroup's qualifier(s) directly
                    # (never allow generic "subgroup" wording to bypass a wrong subgroup like "HFrEF subgroup")
                    c_ctx = _clause_context(c, overall_ctx)
                    all_claim_q_ok = all(
                        _qualifier_supported_by_context(cq, c_ctx) and not _qualifiers_conflict(cq, c.qualifiers)
                        for cq in claim_qualifiers
                    )
                    if all_claim_q_ok and claim_matches_sg:
                        any_clause_aligned = True
                        break
                elif claim_matches_sg or claim_mentions_subgroup:
                    any_clause_aligned = True
                    break
            elif claim_mentions_subgroup and not claim_asserts_overall:
                any_clause_aligned = True
                break

        if not any_clause_aligned:
            sg_labels = tuple(dict.fromkeys(sq.raw for c in hits for sq in c.qualifiers)) or ("subgroup",)
            sg_texts = tuple(dict.fromkeys(c.text for c in hits))
            if has_explicit_conflict or claim_qualifiers:
                reason = REASON_COHORT_CONFLICT
            elif claim_asserts_overall and not claim_mentions_subgroup:
                reason = REASON_SUBGROUP_AS_OVERALL
            else:
                reason = REASON_SUBGROUP_OMITTED
            subgroup_only_issues.append(SubgroupIssue(reason, tok, sg_labels, sg_texts))

    if subgroup_only_issues:
        reason_priority = {
            REASON_COHORT_CONFLICT: 0,
            REASON_SUBGROUP_AS_OVERALL: 1,
            REASON_SUBGROUP_OMITTED: 2,
        }
        subgroup_only_issues.sort(key=lambda issue: reason_priority.get(issue.reason, 9))
        top_reason = subgroup_only_issues[0].reason
        bad_tokens = list(dict.fromkeys(issue.token for issue in subgroup_only_issues))
        source_cohorts = list(dict.fromkeys(lbl for issue in subgroup_only_issues for lbl in issue.source_cohorts))
        source_clauses = list(dict.fromkeys(txt for issue in subgroup_only_issues for txt in issue.source_clauses))
        claim_cohort_labels = [q.raw for q in claim_qualifiers] or (
            ["overall"] if claim_asserts_overall else ["unqualified/overall"]
        )

        if top_reason == REASON_COHORT_CONFLICT:
            msg = (
                f"Matched number(s) [{', '.join(bad_tokens)}] appear in a different cohort/subgroup "
                f"in the source text ({', '.join(source_cohorts)}) than claimed ({', '.join(claim_cohort_labels)})."
            )
        elif top_reason == REASON_SUBGROUP_AS_OVERALL:
            msg = (
                f"Matched number(s) [{', '.join(bad_tokens)}] only appear in a subgroup "
                f"({', '.join(source_cohorts)}) in the source text, not the claimed overall cohort."
            )
        else:
            msg = (
                f"Matched number(s) [{', '.join(bad_tokens)}] only appear in a subgroup "
                f"({', '.join(source_cohorts)}) in the source text, but the claim omits the subgroup qualifier."
            )

        return Diagnostic(
            code="CLAIM_COHORT_MISMATCH",
            severity="WARNING",
            message=msg,
            expected=f"claim_cohort={claim_cohort_labels}",
            actual=f'source_cohort={source_cohorts}; clause="{truncate(source_clauses[0], 260)}"',
            details={
                "reason": top_reason,
                "matched_numbers": bad_tokens,
                "claim_cohorts": claim_cohort_labels,
                "source_cohorts": source_cohorts,
                "matched_clauses": source_clauses[:3],
            },
            hint=(
                "A plain numeric match does not prove the claim applies to the stated population — "
                "the matched value(s) are reported for a specific subgroup/cohort in the paper."
            ),
            fix=(
                "Compare the claim's cohort/population against the printed OFFICIAL ABSTRACT below "
                "(or OA full text) and qualify or correct the cohort/numbers before marking SUPPORTED."
            ),
        )

    # Check 2: Claim explicitly names cohort(s) that are unsupported by the source context of the matched numbers.
    # Verify per non-enrollment (results/outcome) token so mixing a subgroup sample size with an overall HR is caught.
    if claim_qualifiers:
        results_tokens = {tok: hits for tok, hits in token_clauses.items() if any(not c.is_enrollment for c in hits)}
        tokens_to_check = results_tokens if results_tokens else token_clauses

        unsupported_cohorts: list[str] = []
        mismatched_tokens: list[str] = []
        mismatched_clauses: list[str] = []

        for tok, hits in tokens_to_check.items():
            tok_unsupported: list[str] = []
            for cq in claim_qualifiers:
                supported = any(
                    not _qualifiers_conflict(cq, c.qualifiers)
                    and _qualifier_supported_by_context(cq, _clause_context(c, overall_ctx))
                    for c in hits
                )
                if not supported:
                    tok_unsupported.append(cq.raw)
            if tok_unsupported:
                mismatched_tokens.append(tok)
                for u in tok_unsupported:
                    if u not in unsupported_cohorts:
                        unsupported_cohorts.append(u)
                for c in hits:
                    if c.text not in mismatched_clauses:
                        mismatched_clauses.append(c.text)

        if unsupported_cohorts:
            sample_clause = mismatched_clauses[0]
            return Diagnostic(
                code="CLAIM_COHORT_MISMATCH",
                severity="WARNING",
                message=(
                    f"Claim attributes matched number(s) [{', '.join(mismatched_tokens)}] to cohort(s) "
                    f"[{', '.join(unsupported_cohorts)}] not supported by the source context for those values."
                ),
                expected=f"claim_cohort={[q.raw for q in claim_qualifiers]}",
                actual=f'unsupported_cohorts={unsupported_cohorts}; clause="{truncate(sample_clause, 260)}"',
                details={
                    "reason": REASON_COHORT_NOT_IN_SOURCE,
                    "matched_numbers": mismatched_tokens,
                    "claim_cohorts": [q.raw for q in claim_qualifiers],
                    "unsupported_cohorts": unsupported_cohorts,
                    "matched_clauses": mismatched_clauses[:3],
                },
                hint=(
                    "A plain numeric match does not prove the claim applies to the stated population — "
                    "the claimed cohort does not appear in the source context for those numbers."
                ),
                fix=(
                    "Compare the claim's cohort/population against the printed OFFICIAL ABSTRACT below "
                    "(or OA full text) and qualify or correct the cohort/numbers before marking SUPPORTED."
                ),
            )

    return None
