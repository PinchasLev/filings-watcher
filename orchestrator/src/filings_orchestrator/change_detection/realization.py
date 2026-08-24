"""Risk materialization judge — the Risk Radar's first "track update" (Phase 2b).

The radar detects a company-specific risk in a 10-K (change_specificity). This stage tracks
that risk forward: for each flagged specific risk, given the material 8-K/6-K events the
company filed AFTER the 10-K, judge whether any 8-K DIRECTLY realizes THIS risk — uplifting it
from declared (hypothetical) to materialized.

The bar is strict, and deliberately so: a realization must draw a direct line from a specific
8-K disclosure to the specific flagged risk, binary and evidenced. Speculation, a generic
quarterly earnings release, and a merely-related topic all resolve to NOT realized — that is
exactly where an offline validation's weak cases failed. Each verdict cites the 8-K disclosure
and states how it realizes the risk.

Reuses the judge's structured-output discipline (forced single tool call, temperature 0,
cached system prompt).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from typing import Any, NamedTuple

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, field_validator

from filings_orchestrator.cost import emit_llm_call

DEFAULT_REALIZATION_MODEL = "claude-sonnet-4-6"


class RealizationEvent(NamedTuple):
    """One subsequent 8-K/6-K material event offered to the realization judge as a
    candidate for realizing the flagged risk.

    `source_text` is the event's actual disclosure text (the anchored 8-K Item body /
    exhibit excerpt) the judge must quote from to ground its verdict; it defaults empty
    for callers (unit tests) that only have the one-line `summary`, in which case the
    summary is the only material the judge sees and quotes."""

    filing_date: str
    event_type: str
    item: str
    summary: str
    source_text: str = ""


class RealizationVerdict(BaseModel):
    """The judge's verdict for one flagged risk. Matches the bound tool schema — field
    order and descriptions are what the model reads."""

    is_realized: bool = Field(
        description=(
            "True ONLY if a specific 8-K event DIRECTLY realizes this flagged risk (the "
            "hypothetical has materialized or is concretely advancing); false otherwise."
        )
    )
    event_index: int | None = Field(
        default=None,
        description=(
            "The 1-based index of the 8-K event that realizes the risk; null when not realized."
        ),
    )
    quote: str = Field(
        default="",
        description=(
            "When is_realized is true: a SHORT span (one sentence or clause) copied VERBATIM, "
            "character-for-character, from the realizing event's DISCLOSURE TEXT — the exact "
            "words that state the adverse consequence. Do not paraphrase, summarize, or combine "
            "spans. Empty when not realized."
        ),
    )
    evidence: str = Field(
        default="",
        description=(
            "One sentence stating how the quoted disclosure realizes THIS risk. Assert ONLY what "
            "the quote and the risk factor already say — introduce no name, title, role, number, "
            "or fact that is not present verbatim in the quote or the risk-factor text."
        ),
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="Confidence in the realized/not-realized judgment, 0..1."
    )

    @field_validator("event_index", mode="before")
    @classmethod
    def _coerce_index(cls, value: object) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, (float, str)):
            try:
                return int(float(value))
            except ValueError:
                return None
        return None

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, value: object) -> float:
        if isinstance(value, bool):
            return 0.5
        if isinstance(value, (int, float)):
            return min(1.0, max(0.0, float(value)))
        if isinstance(value, str):
            try:
                return min(1.0, max(0.0, float(value)))
            except ValueError:
                return 0.5
        return 0.5


_SYSTEM_PROMPT = (
    "You are given the material 8-K/6-K events a company filed AFTER its 10-K, and then ONE "
    "company-specific risk from that 10-K — the RISK FACTOR text plus a one-line note on this "
    "year's CHANGE. Decide whether any single event shows THIS risk MATERIALIZING.\n\n"
    "A risk materializes when the ADVERSE CONSEQUENCE it warns about actually befalls the company "
    "and a subsequent event discloses that consequence: a strike or shutdown for a labor risk; the "
    "key person actually departing for a retention risk; a cyber breach for a security risk; a "
    "downgrade, covenant breach, default, or forced asset sale for a debt risk; an announced "
    "impairment charge; a lawsuit filed against the company; the loss of a major customer; a "
    "regulator's enforcement action. The harm has LANDED, and the event says so. Set is_realized="
    "true and name that one event.\n\n"
    "Materialization is about CONSEQUENCES, not activity. Set is_realized=false when the company "
    "merely does more of the risky thing or manages the exposure without harm having occurred: "
    "taking on, issuing, or refinancing debt; amending a credit facility; a leadership transition "
    "where the executive REMAINS with the company or on its board (moving from an executive to a "
    "non-executive or chairman role is a role change, not a departure — only an actual exit, such "
    "as resigning, being terminated, or leaving for a competitor, realizes a key-person or "
    "retention risk); adjusting severance or retention terms; raising capital. These change the "
    "exposure; they are NOT the consequence the risk warned about. Also reject: a merely related "
    "topic; a peripheral facet the factor mentions in passing (a labor-costs risk is NOT realized "
    "by a CEO appointment); a generic quarterly earnings release; a big salient event (a merger, a "
    "large acquisition) matched to a loosely-related risk merely because it is important; "
    "speculation that results 'would' or 'could' reflect the risk; and any link that requires "
    "INFERENCE rather than being explicitly disclosed.\n\n"
    "Anchor on the CORE subject of the RISK FACTOR. Point to the one event whose disclosure states "
    "the consequence. When in doubt, choose false.\n\n"
    "GROUND YOUR VERDICT IN THE SOURCE. Each event is given with its actual DISCLOSURE TEXT. When "
    "you set is_realized=true you MUST copy into `quote` a short span, VERBATIM and "
    "character-for-character, from THAT event's disclosure text — the exact words stating the "
    "adverse consequence. If no span of the disclosure text states the consequence plainly, the "
    "risk is not realized: choose false. Never quote from the risk factor, from another event, or "
    "from your own words. Then in `evidence` state in one sentence how that quoted disclosure "
    "realizes THIS risk — asserting ONLY what the quote and the risk factor already say. Do NOT "
    "add any name, title, role, number, or fact not written verbatim in the quote or the risk "
    "factor; inventing an unstated detail (for example a job title the filing does not give) is a "
    "serious error even if it seems plausible. This is checked in code: every role or title "
    "phrase, every figure, and every span you put in quotation marks must appear in the filing "
    "text, and a verdict whose sentence fails that check is discarded entirely. Describe a "
    "person only as the filing describes them. Submit your verdict with the tool, exactly once."
)


def prompt_fingerprint() -> str:
    """Hash of everything that determines what the judge reads: the system prompt and the user
    turn's layout. Reordering the turn changes the judgment as surely as rewording the prompt,
    so both feed the version — otherwise a layout change would be silently reinterpreted under
    the old tag."""
    material = "\x00".join((_SYSTEM_PROMPT, _EVENTS_HEADER, _RISK_HEADER))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]


def realization_version(model_name: str = DEFAULT_REALIZATION_MODEL) -> str:
    """A reproducibility tag = model + a hash of the prompt and its layout (mirrors
    judge_version). Changing the model, the prompt, or the turn order yields a new version, so
    verdicts re-derive rather than being silently reinterpreted."""
    return f"{model_name}+realization-{prompt_fingerprint()}"


def build_realization_judge(model_name: str = DEFAULT_REALIZATION_MODEL) -> Any:
    """A Claude model bound to the realization tool, forced to call it once."""
    model = ChatAnthropic(model_name=model_name, timeout=60, stop=None, temperature=0)
    tool_spec = {
        "name": "submit_realization",
        "description": "Submit the risk-realization verdict. Call exactly once.",
        "input_schema": RealizationVerdict.model_json_schema(),
    }
    return model.bind_tools([tool_spec], tool_choice={"type": "tool", "name": "submit_realization"})


# Prompt layout. The events block leads and the risk trails, because caching is a PREFIX match:
# every risk flagged on one 10-K is judged against the same events (they are loaded by cik +
# the 10-K's filing date, not per risk), and those risks are processed back-to-back by the
# selection's ORDER BY. Leading with the shared block lets each subsequent call read it from
# cache instead of re-sending it. Putting the per-risk text first — the original layout — made
# the volatile part the prefix, so nothing after it could ever be reused.
_EVENTS_HEADER = "SUBSEQUENT 8-K EVENTS:\n"
_RISK_HEADER = "FLAGGED RISK (declared in the 10-K):\n"


def _build_events_block(events: Sequence[RealizationEvent]) -> str:
    """The shared, cacheable half: every candidate event with its disclosure text. Identical
    for all risks flagged on one 10-K."""
    blocks: list[str] = []
    for i, e in enumerate(events, start=1):
        head = (
            f"{i}. [{e.filing_date}] {e.event_type}"
            f"{(' (item ' + e.item + ')') if e.item else ''}: {e.summary}"
        )
        # The disclosure text is the ONLY material `quote` may be copied from. Present it
        # only when supplied (unit callers pass just the summary, which stands as the source).
        if e.source_text.strip():
            head += f"\n   DISCLOSURE TEXT (quote only from here):\n   {e.source_text.strip()}"
        blocks.append(head)
    return _EVENTS_HEADER + "\n".join(blocks)


def _build_risk_block(risk_text: str, risk: str) -> str:
    """The volatile half: the one risk under judgment. Must trail the events block."""
    factor = risk_text.strip() or "(text unavailable)"
    return _RISK_HEADER + f"Risk factor: {factor}\nWhat changed this year: {risk}"


def _build_user_prompt(risk_text: str, risk: str, events: Sequence[RealizationEvent]) -> str:
    """The user turn as one string — what the model reads, in order. The offline eval uses this
    so its prompt can never drift from production's."""
    return _build_events_block(events) + "\n\n" + _build_risk_block(risk_text, risk)


def build_user_content(
    risk_text: str,
    risk: str,
    events: Sequence[RealizationEvent],
    *,
    cache_shared_prefix: bool = True,
) -> list[str | dict[Any, Any]]:
    """The same turn split into two content blocks with a cache breakpoint after the shared
    events block. Text is identical to `_build_user_prompt`; only the block boundary differs.

    `cache_shared_prefix=False` sends the turn as one unbroken block. A breakpoint pays a 1.25x
    write premium on the events block to make it reusable, which is a loss when this 10-K
    contributes only one risk to the run and nothing will ever read the entry — and two thirds
    of the 10-Ks in a run contribute exactly one."""
    events_block = _build_events_block(events)
    risk_block = _build_risk_block(risk_text, risk)
    if not cache_shared_prefix:
        return [{"type": "text", "text": events_block + "\n\n" + risk_block}]
    return [
        {"type": "text", "text": events_block, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": risk_block},
    ]


_TOC_NOISE = re.compile(r"\d+\s*table of contents", re.IGNORECASE)


# EDGAR sets its prose with typographic punctuation — curly quotes, en and em dashes, soft
# hyphens — while a model transcribing a span from it emits the ASCII equivalents. None of that
# carries meaning the citation check is about, and demanding byte-identical typography rejected
# faithful quotes wholesale: of eight the gate refused in one production run, SEVEN differed from
# their filing by nothing else, each diverging at a possessive or a defined term in quotes. The
# eighth invented a dollar figure and still fails after folding, which is the point — this narrows
# the check to typography and leaves the anti-fabrication guard doing its job.
_TYPOGRAPHIC_FOLD = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
        "\u2032": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2033": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2015": "-",
        "\u2212": "-",
        "\u2026": "...",
        "\u00ad": "",
        "\u200b": "",
        "\ufeff": "",
    }
)


def _normalize_for_quote(value: str) -> str:
    """Fold text for verbatim-quote checking: map typographic punctuation to its ASCII
    equivalent, strip interspersed 'N Table of Contents' page-break artifacts (EDGAR injects
    these mid-sentence), then collapse all whitespace to single spaces and lowercase — so a
    genuine quote is not rejected over cosmetic source noise. What survives the fold is the
    wording, which is what the citation check is actually about."""
    return " ".join(_TOC_NOISE.sub(" ", value.translate(_TYPOGRAPHIC_FOLD)).split()).lower()


def quote_is_grounded(quote: str, source_text: str, *, min_chars: int = 12) -> bool:
    """True when `quote` is a verbatim span of `source_text` (normalized). A too-short or empty
    quote is not grounding — the judge must cite a real, substantive span of the disclosure."""
    q = _normalize_for_quote(quote)
    if len(q) < min_chars:
        return False
    return q in _normalize_for_quote(source_text)


def realization_is_grounded(
    verdict: RealizationVerdict, events: Sequence[RealizationEvent]
) -> bool:
    """Code check on a REALIZED verdict (the bounded-operator gate): the cited event must exist
    and the verdict's quote must be a verbatim span of THAT event's disclosure text (falling back
    to its summary when no separate source text was supplied). Not-realized verdicts pass through
    (nothing to ground). A realized verdict that fails this is ungrounded and is not surfaced."""
    if not verdict.is_realized:
        return True
    idx = verdict.event_index
    if idx is None or not (1 <= idx <= len(events)):
        return False
    event = events[idx - 1]
    source = event.source_text.strip() or event.summary
    return quote_is_grounded(verdict.quote, source)


# --- Evidence grounding -------------------------------------------------------------------
#
# The quote gate above proves the CITATION is real. It says nothing about the sentence the
# page actually renders: `evidence`. That field is free text, and a model asked to explain a
# connection will fuse accurate fragments into an attribution the filing never makes — the
# original bug pinned "designated future CEO of Global Coffee Co." on an executive the 8-K
# described only as the head of the Coffee Operating Unit. Prompt wording alone did not hold
# it, so the claim-bearing parts of the sentence are checked here in code, against the filing
# text the sentence is allowed to draw on.

# Dropped before comparing, so a paraphrase that only swaps articles or possessives still
# matches its source ("head of THE Coffee Operating Unit" vs "head of ITS Coffee Operating
# Unit"). Everything else is treated as a content claim.
_EVIDENCE_FUNCTION_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "s",
        "this",
        "that",
        "these",
        "those",
        "its",
        "it",
        "his",
        "her",
        "their",
        "our",
        "your",
        "of",
        "for",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "as",
        "from",
        "into",
        "and",
        "or",
        "but",
        "which",
        "who",
        "whose",
        "whom",
        "is",
        "was",
        "are",
        "were",
        "be",
        "been",
        "being",
        "has",
        "have",
        "had",
        "will",
        "would",
        "may",
        "might",
        "can",
        "could",
        "shall",
    }
)

# Words that grade a role's importance rather than name an office. On a bare role word they
# form a CATEGORY reference ("a key executive", "a senior executive") — the sentence is
# describing what kind of person this is, not asserting a title, so there is nothing for the
# filing to corroborate. None of them appears in an SEC officer title on its own.
_GENERIC_ROLE_MODIFIERS = frozenset(
    {
        "key",
        "senior",
        "top",
        "certain",
        "other",
        "another",
        "several",
        "various",
        "important",
        "critical",
        "significant",
        "major",
    }
)

# A role word anchors a person-attribution — precisely the class of claim the judge invented.
_ROLE_KEYWORDS = frozenset(
    {
        "ceo",
        "cfo",
        "coo",
        "cto",
        "cio",
        "chair",
        "chairman",
        "chairwoman",
        "president",
        "officer",
        "officers",
        "director",
        "directors",
        "head",
        "chief",
        "evp",
        "svp",
        "vp",
        "controller",
        "founder",
        "treasurer",
        "secretary",
        "principal",
        "executive",
        "executives",
    }
)

_TOKEN_RE = re.compile(r"[A-Za-z]+|\d+(?:[.,]\d+)*")
# "8-K", "10-Q", "20-F" — the NAME of a document, not a quantity. The tokenizer splits
# hyphenated compounds, so "The 8-K discloses..." yields a bare "8" that the invented-figure
# check then hunts for in the sources and cannot find. Two of three evidence rejections in one
# production run were this, refusing sound verdicts over the phrase every evidence sentence
# naturally opens with. Stripped before the figure scan only; the letters stay readable to every
# other check.
_SEC_FORM_RE = re.compile(r"\b\d{1,2}-[A-Za-z]{1,2}\b")

# Punctuation a quotation carries at its edges belongs to the sentence doing the quoting, not to
# the source. A filing ending "...or not completed." quoted mid-sentence becomes
# "...or not completed," — the convention, not a misquote. Compared literally that one character
# sinks the whole span, so the edges are trimmed before the check. Anything internal still has to
# match.
_QUOTE_EDGE_PUNCT = " .,;:!?"
_QUOTED_RE = re.compile(r"[\"\u201c]([^\"\u201d]{6,})[\"\u201d]")


def _raw_tokens(text: str) -> list[str]:
    """Word and number tokens with their original case, which `_role_phrases` needs to tell a
    proper-noun complement ("head of the Coffee Operating Unit") from the verb that follows it."""
    return [t.replace(",", "") for t in _TOKEN_RE.findall(text)]


def _tokens(text: str) -> list[str]:
    """Lowercased word and number tokens. Possessives split to a bare `s` and hyphenated
    compounds split to their parts, both of which fold away as function words or match
    part-wise."""
    return [t.lower() for t in _raw_tokens(text)]


def _content(tokens: Sequence[str]) -> list[str]:
    return [t for t in tokens if t not in _EVIDENCE_FUNCTION_WORDS]


def _contains_run(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    """True when `needle` appears as a CONTIGUOUS run in `haystack`. Contiguity is the whole
    point: the invented title reused only words the filing does contain, just never adjacent."""
    n = len(needle)
    if n == 0:
        return True
    return any(list(haystack[i : i + n]) == list(needle) for i in range(len(haystack) - n + 1))


def _role_phrases(evidence: str) -> list[list[str]]:
    """The role-anchored noun phrases asserted by `evidence`: each role word, the modifiers
    immediately before it, and any `of ...` complement after it. A bare role word carries no
    attribution and is skipped, as is one qualified only by a significance adjective."""
    raw = _raw_tokens(evidence)
    toks = [t.lower() for t in raw]
    phrases: list[list[str]] = []
    for i, tok in enumerate(toks):
        if tok not in _ROLE_KEYWORDS:
            continue
        # Modifiers immediately before the role word. This is where an invented title lives:
        # "prospective CEO", "designated future CEO".
        start = i
        while start > 0 and toks[start - 1] not in _EVIDENCE_FUNCTION_WORDS:
            start -= 1
        end = i + 1
        # An "of ..." complement extends the phrase, but only over a proper-noun run, so the
        # phrase ends at the unit named rather than running on into the sentence's verb.
        if end < len(toks) and toks[end] == "of":
            cursor = end + 1
            while cursor < len(toks) and toks[cursor] in _EVIDENCE_FUNCTION_WORDS:
                cursor += 1
            complement = cursor
            while complement < len(raw) and raw[complement][:1].isupper():
                complement += 1
            if complement > cursor:
                end = complement
        phrase = _content(toks[start:end])
        # A significance adjective on a bare role word names a category of person, not an
        # office: "a key executive" is how the sentence refers to the risk's subject, and the
        # filing has no such phrase to match because there is no title being claimed. A longer
        # phrase is a composite title ("senior vice president") and stays checked in full, so
        # inflating a real title still fails.
        if len(phrase) == 2 and phrase[0] in _GENERIC_ROLE_MODIFIERS:
            continue
        if len(phrase) >= 2:
            phrases.append(phrase)
    return phrases


def evidence_is_grounded(evidence: str, *, sources: Sequence[str]) -> tuple[bool, list[str]]:
    """Code check on the rendered evidence sentence. Returns (grounded, unsupported claims).

    Three classes of claim must trace to one of the `sources` (the cited quote, the risk-factor
    text, and the realizing disclosure) — each checked against a single source, so a claim
    stitched together from two of them does not pass:

      * role-anchored noun phrases (an invented title or attribution),
      * numbers (an invented figure),
      * spans the sentence itself puts in quotation marks.

    An empty sentence or an absent source is vacuously grounded; there is nothing to check."""
    if not evidence.strip():
        return True, []
    live = [s for s in sources if s and s.strip()]
    if not live:
        return True, []

    src_tokens = [_tokens(s) for s in live]
    src_content = [_content(t) for t in src_tokens]
    src_normalized = [_normalize_for_quote(s) for s in live]
    unsupported: list[str] = []

    for phrase in _role_phrases(evidence):
        if not any(_contains_run(sc, phrase) for sc in src_content):
            unsupported.append(" ".join(phrase))

    for token in _tokens(_SEC_FORM_RE.sub(" ", evidence)):
        if any(c.isdigit() for c in token) and not any(token in t for t in src_tokens):
            unsupported.append(token)

    for span in _QUOTED_RE.findall(evidence):
        trimmed = span.strip(_QUOTE_EDGE_PUNCT)
        folded = _normalize_for_quote(trimmed)
        if folded and not any(folded in s for s in src_normalized):
            unsupported.append(trimmed)

    # Preserve order, drop repeats.
    return (not unsupported), list(dict.fromkeys(unsupported))


def realization_evidence_is_grounded(
    verdict: RealizationVerdict, events: Sequence[RealizationEvent], *, risk_text: str
) -> tuple[bool, list[str]]:
    """Evidence gate for a REALIZED verdict, mirroring `realization_is_grounded`. Not-realized
    verdicts carry no rendered sentence and pass through."""
    if not verdict.is_realized:
        return True, []
    idx = verdict.event_index
    event = events[idx - 1] if idx is not None and 1 <= idx <= len(events) else None
    disclosure = (event.source_text.strip() or event.summary) if event else ""
    return evidence_is_grounded(verdict.evidence, sources=[verdict.quote, risk_text, disclosure])


def judge_realization(
    model: Any,
    *,
    risk_text: str,
    risk: str,
    events: Sequence[RealizationEvent],
    model_name: str,
    accession_number: str | None = None,
    cache_shared_prefix: bool = True,
) -> RealizationVerdict:
    """Judge whether any subsequent 8-K realizes the flagged risk via the bound `model`.
    The risk factor's text anchors the judgment to the core risk (rather than a poorly-parsed
    heading). Records the call for cost accounting even if parsing fails.

    `cache_shared_prefix` is False when this 10-K contributes a single risk to the run, so the
    events block is sent without a breakpoint nothing would read."""
    system_blocks: list[str | dict[Any, Any]] = [
        {"type": "text", "text": _SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
    ]
    # Two breakpoints: the system prompt — shared by every call the pipeline makes, so always
    # worth caching — then the events block shared by every risk on this 10-K, which is only
    # worth caching when more than one risk will read it. The trailing risk block is the only
    # part re-sent at full price per call.
    user_content = build_user_content(
        risk_text, risk, events, cache_shared_prefix=cache_shared_prefix
    )
    response = model.invoke(
        [SystemMessage(content=system_blocks), HumanMessage(content=user_content)]
    )
    emit_llm_call(
        model=model_name,
        stage="realization",
        response=response,
        accession_number=accession_number,
    )
    tool_calls = getattr(response, "tool_calls", None) or []
    if not tool_calls:
        raise RuntimeError("model did not return a tool call; cannot extract realization verdict")
    return RealizationVerdict.model_validate(tool_calls[0]["args"])
