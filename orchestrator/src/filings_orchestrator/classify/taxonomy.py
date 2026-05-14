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
    MA_ACTIVITY = "ma_activity"
    RESTATEMENT = "restatement"
    AUDITOR_CHANGE = "auditor_change"
    GOING_CONCERN = "going_concern"
    MATERIAL_IMPAIRMENT = "material_impairment"
    SHAREHOLDER_VOTE_RESULTS = "shareholder_vote_results"
    DELISTING_RISK = "delisting_risk"
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
