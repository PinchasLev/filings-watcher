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

from enum import StrEnum


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
        "A material event that does not fit any of the more specific "
        "categories above. Use this rather than guessing."
    ),
}


# Items that are pure scaffolding for other disclosures; skip when iterating
# through a filing's items, since they convey no event of their own.
NON_SUBSTANTIVE_ITEMS: frozenset[str] = frozenset(
    {
        "9.01",  # Financial Statements and Exhibits — references attached exhibits
    }
)
