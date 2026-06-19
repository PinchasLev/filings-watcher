"""Classification taxonomy for 8-K material events.

The labels are chosen to align with the SEC's 8-K Item structure where the
mapping is clean, and to expand only where one Item covers materially
different events (notably Item 5.02 covers both executive departures and
appointments — distinct signals).

Item references in each label's description identify the SEC Item that
typically triggers the event; classification considers the prose, not the
Item number alone, so a filing with Item 5.02 may still be classified as
`exec_departure` or `exec_appointment` depending on what actually occurred.

Items that are purely supporting (e.g., Item 9.01 Financial Statements
and Exhibits, Item 7.01 Regulation FD Disclosure when used as a wrapper)
are skipped at the caller, not represented in the taxonomy.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import NamedTuple

# Bump this every time the EventType enum, EVENT_TYPE_DESCRIPTIONS, or
# EVENT_TO_DOMAIN mapping changes (major.minor: additive change → minor, breaking
# rename/split/merge → major; ADR 0032). Persisted classifications carry this
# value so historical rows remain interpretable under their original taxonomy
# (ADR 0011), and a content hash binds it to the choice-set it names (ADR 0032).
# v1.1: added the per-domain `*_other` catch-all leaves (additive).
TAXONOMY_VERSION = "v1.1"


class EventType(StrEnum):
    """Material event types classified from 8-K Item content.

    String enum so the value flows cleanly through JSON Schema and tool-use
    arguments without custom serialization.
    """

    EARNINGS_RELEASE = "earnings_release"
    EXEC_DEPARTURE = "exec_departure"
    EXEC_APPOINTMENT = "exec_appointment"
    EXEC_COMPENSATION = "exec_compensation"
    MA_ACTIVITY = "ma_activity"
    RESTATEMENT = "restatement"
    AUDITOR_CHANGE = "auditor_change"
    GOING_CONCERN = "going_concern"
    MATERIAL_IMPAIRMENT = "material_impairment"
    SHAREHOLDER_VOTE_RESULTS = "shareholder_vote_results"
    DELISTING_RISK = "delisting_risk"
    BANKRUPTCY_FILING = "bankruptcy_filing"
    COVENANT_BREACH = "covenant_breach"
    CYBERSECURITY_INCIDENT = "cybersecurity_incident"
    DILUTIVE_ISSUANCE = "dilutive_issuance"
    MATERIAL_LITIGATION = "material_litigation"
    OTHER_MATERIAL = "other_material"
    # Per-domain catch-alls (ADR 0032, v1.1): a graceful "I know the domain but
    # not the specific type" home, so a known-domain event is not forced into a
    # wrong specific leaf or the global `other_material`. The global catch-all
    # stays for events whose domain itself is unclear.
    GOVERNANCE_OTHER = "governance_other"
    FINANCIAL_OTHER = "financial_other"
    OPERATIONAL_OTHER = "operational_other"
    LEGAL_OTHER = "legal_other"
    TERMINAL_OTHER = "terminal_other"


EVENT_TYPE_DESCRIPTIONS: dict[EventType, str] = {
    EventType.EARNINGS_RELEASE: (
        "Disclosure of quarterly or annual financial results, typically as a "
        "press release attached as an exhibit (commonly Item 2.02)."
    ),
    EventType.EXEC_DEPARTURE: (
        "Departure of a director, officer, or named executive — resignation, "
        "removal, retirement, or death (commonly Item 5.02). Use this when "
        "the principal disclosed action is a person leaving."
    ),
    EventType.EXEC_APPOINTMENT: (
        "Appointment or election of a director, officer, or named executive "
        "(commonly Item 5.02). Use this when the principal disclosed action "
        "is a person taking a role, even if a departure also occurred."
    ),
    EventType.EXEC_COMPENSATION: (
        "Disclosure of compensatory arrangements for a director, officer, "
        "or named executive — equity grants, performance awards, "
        "compensation plan amendments, severance modifications, or "
        "shareholder approval of such arrangements (typically Item 5.02(e)). "
        "Distinct from `exec_departure` (a person leaving) and "
        "`exec_appointment` (a person taking a role)."
    ),
    EventType.MA_ACTIVITY: (
        "Entry into, completion, or termination of a material acquisition, "
        "disposition, merger, or change of control (Items 1.01, 1.02, 2.01, "
        "5.01)."
    ),
    EventType.RESTATEMENT: (
        "Non-reliance on previously issued financial statements or audit "
        "report — i.e., a financial restatement (Item 4.02). Strong signal "
        "of accounting trouble."
    ),
    EventType.AUDITOR_CHANGE: (
        "Change in the registrant's independent accountant — resignation, "
        "dismissal, or appointment of a new auditor (Item 4.01)."
    ),
    EventType.GOING_CONCERN: (
        "Disclosure of substantial doubt about the registrant's ability to "
        "continue as a going concern. Often appears under Item 8.01 (Other "
        "Events) but the language is unmistakable."
    ),
    EventType.MATERIAL_IMPAIRMENT: (
        "Material impairment, write-down, or charge — assets, goodwill, "
        "intangibles, or other (Item 2.06)."
    ),
    EventType.SHAREHOLDER_VOTE_RESULTS: (
        "Results of a vote at an annual or special meeting of security holders (Item 5.07)."
    ),
    EventType.DELISTING_RISK: (
        "Notice of delisting, failure to satisfy a continued listing rule, "
        "or transfer of listing (Item 3.01)."
    ),
    EventType.BANKRUPTCY_FILING: (
        "Voluntary or involuntary bankruptcy or receivership filing — "
        "Chapter 7, Chapter 11, or analogous proceedings (Item 1.03). "
        "Terminal signal."
    ),
    EventType.COVENANT_BREACH: (
        "Triggering event that accelerates or increases a direct financial "
        "obligation — debt covenant violation, cross-default, or technical "
        "default (Item 2.04). Often the earliest visible indicator of "
        "financial stress."
    ),
    EventType.CYBERSECURITY_INCIDENT: (
        "Material cybersecurity incident — breach, ransomware, data "
        "exfiltration, or other compromise of information systems "
        "(Item 1.05, required since 2023)."
    ),
    EventType.DILUTIVE_ISSUANCE: (
        "Unregistered sale of equity securities — private placements, "
        "PIPEs, convertible notes, ATM offerings (Item 3.02). Strong "
        "signal at small- and mid-cap issuers raising cash."
    ),
    EventType.MATERIAL_LITIGATION: (
        "Material litigation, regulatory investigation, or settlement — "
        "lawsuits filed against the registrant, government investigations "
        "or charges, large settlements or judgments. Typically disclosed "
        "under Item 8.01 (Other Events); no dedicated 8-K Item."
    ),
    EventType.OTHER_MATERIAL: (
        "A material event whose kind you cannot place in any domain above — you "
        "cannot tell whether it is governance, financial, operational, legal, or "
        "existential. Use this only when the domain itself is unclear; when the "
        "domain is clear but no specific type fits, use that domain's "
        "`*_other` category instead."
    ),
    EventType.GOVERNANCE_OTHER: (
        "A governance event — leadership, board, auditor, or shareholder-vote "
        "matter — that does not fit a specific governance category above. Use "
        "when the event is clearly governance-related but not a named type; "
        "prefer this over `other_material` whenever the domain is clear."
    ),
    EventType.FINANCIAL_OTHER: (
        "A financial event — results, obligations, capital, or accounting — that "
        "does not fit a specific financial category above (for example an asset "
        "sale or divestiture, a debt or project financing, or a buyback or "
        "dividend action). Use when the event is clearly financial but not a "
        "named type; prefer this over `other_material` whenever the domain is clear."
    ),
    EventType.OPERATIONAL_OTHER: (
        "An operational or strategic business event that does not fit a specific "
        "operational category above (for example a material contract, "
        "partnership, product or regulatory milestone, or restructuring). Use "
        "when the event is clearly operational but not a named type; prefer this "
        "over `other_material` whenever the domain is clear."
    ),
    EventType.LEGAL_OTHER: (
        "A legal or regulatory event that does not fit a specific legal category "
        "above. Use when the event is clearly legal or regulatory but not a named "
        "type; prefer this over `other_material` whenever the domain is clear."
    ),
    EventType.TERMINAL_OTHER: (
        "An event materially threatening the registrant's continued existence "
        "that does not fit a specific terminal category above. Use when the event "
        "is clearly existential but not a named type; prefer this over "
        "`other_material` whenever the domain is clear."
    ),
}


# Items that are pure scaffolding for other disclosures; skip when iterating
# through a filing's items, since they convey no event of their own.
NON_SUBSTANTIVE_ITEMS: frozenset[str] = frozenset(
    {
        "9.01",  # Financial Statements and Exhibits — references attached exhibits
    }
)


class EventDomain(StrEnum):
    """High-level grouping of event types.

    Each `EventType` maps to exactly one `EventDomain` via `EVENT_TO_DOMAIN`.
    Domains group fine-grained event types into coarser categories useful for
    dashboard organization, watchlist alerting at the group level, and
    cross-filing pattern detection where the specific event type is less
    important than the kind of event.

    This is a post-hoc mapping derived from the leaf classification, not a
    hierarchical classifier — the model still picks one `EventType` per
    section, and the domain follows mechanically. See ADR 0010 for the
    trade-off against a full hierarchical classifier.
    """

    GOVERNANCE = "governance"
    FINANCIAL = "financial"
    OPERATIONAL = "operational"
    LEGAL = "legal"
    TERMINAL = "terminal"
    CATCHALL = "catchall"


EVENT_TO_DOMAIN: dict[EventType, EventDomain] = {
    # Governance: who runs the company, who audits it, what shareholders vote on.
    EventType.EXEC_DEPARTURE: EventDomain.GOVERNANCE,
    EventType.EXEC_APPOINTMENT: EventDomain.GOVERNANCE,
    EventType.EXEC_COMPENSATION: EventDomain.GOVERNANCE,
    EventType.AUDITOR_CHANGE: EventDomain.GOVERNANCE,
    EventType.SHAREHOLDER_VOTE_RESULTS: EventDomain.GOVERNANCE,
    EventType.GOVERNANCE_OTHER: EventDomain.GOVERNANCE,
    # Financial: the numbers, the obligations, the equity.
    EventType.EARNINGS_RELEASE: EventDomain.FINANCIAL,
    EventType.RESTATEMENT: EventDomain.FINANCIAL,
    EventType.MATERIAL_IMPAIRMENT: EventDomain.FINANCIAL,
    EventType.COVENANT_BREACH: EventDomain.FINANCIAL,
    EventType.DILUTIVE_ISSUANCE: EventDomain.FINANCIAL,
    EventType.FINANCIAL_OTHER: EventDomain.FINANCIAL,
    # Operational: structural business changes.
    EventType.MA_ACTIVITY: EventDomain.OPERATIONAL,
    EventType.OPERATIONAL_OTHER: EventDomain.OPERATIONAL,
    # Legal: external pressure or risk from courts, regulators, or attackers.
    EventType.MATERIAL_LITIGATION: EventDomain.LEGAL,
    EventType.CYBERSECURITY_INCIDENT: EventDomain.LEGAL,
    EventType.LEGAL_OTHER: EventDomain.LEGAL,
    # Terminal: events that materially threaten the registrant's continuing existence.
    EventType.GOING_CONCERN: EventDomain.TERMINAL,
    EventType.DELISTING_RISK: EventDomain.TERMINAL,
    EventType.BANKRUPTCY_FILING: EventDomain.TERMINAL,
    EventType.TERMINAL_OTHER: EventDomain.TERMINAL,
    # Catch-all (domain itself unclear).
    EventType.OTHER_MATERIAL: EventDomain.CATCHALL,
}


def domain_for(event_type: EventType) -> EventDomain:
    """Return the EventDomain for a given EventType.

    Raises KeyError if `EVENT_TO_DOMAIN` and `EventType` ever drift — guarded
    against by the taxonomy-coverage test.
    """
    return EVENT_TO_DOMAIN[event_type]


# --- Taxonomy snapshot + content hash (ADR 0032) --------------------------
#
# The full definition of the taxonomy at a point in time, used both to populate
# the per-version snapshot tables and to compute the content hash that binds a
# `taxonomy_version` to its choice-set (so a content change cannot ship under an
# unchanged label). The hash is over the content only — never the version
# string — so two versions with identical content hash identically.


class TaxonomyDomainDef(NamedTuple):
    """One tier-1 domain in the taxonomy definition."""

    domain: str
    description: str | None


class TaxonomyLeafDef(NamedTuple):
    """One tier-2 leaf, with its description and the domain it rolls up to."""

    leaf: str
    description: str
    domain: str


class TaxonomyDefinition(NamedTuple):
    """The full taxonomy definition for the current in-code `TAXONOMY_VERSION`."""

    version: str
    domains: list[TaxonomyDomainDef]
    leaves: list[TaxonomyLeafDef]


def taxonomy_definition() -> TaxonomyDefinition:
    """Return the current in-code taxonomy as a structured definition.

    Domains carry no descriptions today (the `EventDomain` enum has none); the
    field is present so descriptions can be added later as an additive change.
    """
    domains = [TaxonomyDomainDef(domain=d.value, description=None) for d in EventDomain]
    leaves = [
        TaxonomyLeafDef(
            leaf=event_type.value,
            description=EVENT_TYPE_DESCRIPTIONS[event_type],
            domain=domain_for(event_type).value,
        )
        for event_type in EventType
    ]
    return TaxonomyDefinition(version=TAXONOMY_VERSION, domains=domains, leaves=leaves)


def hash_taxonomy_content(
    domains: list[tuple[str, str | None]],
    leaves: list[tuple[str, str, str]],
) -> str:
    """SHA-256 of a taxonomy's content from raw domain/leaf tuples.

    Canonical and order-independent: domains and leaves are sorted before
    hashing, so the hash depends only on the *content*, not on declaration or row
    order. The version string is deliberately excluded — the hash answers "is this
    the same choice-set?", which the version label is then bound to. Shared by the
    in-code hash and the stored-snapshot verification so both canonicalize
    identically (ADR 0032).
    """
    canonical = {
        "domains": sorted([domain, description or ""] for domain, description in domains),
        "leaves": sorted([leaf, description, domain] for leaf, description, domain in leaves),
    }
    payload = json.dumps(canonical, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def taxonomy_content_hash() -> str:
    """SHA-256 of the current in-code taxonomy's content."""
    definition = taxonomy_definition()
    return hash_taxonomy_content(
        [(d.domain, d.description) for d in definition.domains],
        [(leaf.leaf, leaf.description, leaf.domain) for leaf in definition.leaves],
    )
