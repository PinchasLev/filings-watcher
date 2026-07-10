"""Business-day helpers shared by the daily-index reconciler and its freshness alarm.

Two independent call sites need the same notion of "is this a day EDGAR
publishes a daily index, and how many such days separate two dates":

- `scan_daily_index` uses `is_business_day` to route a missing today's-index
  publication as alarm-eligible (a weekday) vs informational (a weekend).
- `check_ingest_freshness` uses `business_days_between` to measure how far the
  ingest cursor has fallen behind today, in the unit that matters — published
  index days, not wall-clock days (a cursor sitting at Friday all weekend is
  not stale).

`is_business_day` is weekday-only. The ~9 NYSE/SEC holidays per year register
as false business days: on a holiday the daily index legitimately does not
publish, so a lag count can over-count by one per holiday in the gap. Callers
absorb this with slack in their thresholds rather than encoding a calendar
here; wiring in a real NYSE calendar (e.g. the `holidays` package or
`pandas_market_calendars`) is the tracked follow-up. The largest routine US
market closure is two consecutive weekdays (Thanksgiving Thu + the following
half-day Fri is still a publish day; Christmas/New-Year gaps are one weekday),
so a threshold with two business days of slack is holiday-safe.
"""

from __future__ import annotations

from datetime import date


def parse_filed_at_to_date(value: str) -> date:
    """Parse a filing date into a `date`.

    Accepts the daily index's `YYYYMMDD` form and an ISO `YYYY-MM-DD` form,
    matching the two shapes `last_filed_at` is written with across ingest paths.
    """
    s = value.strip()
    if len(s) == 8 and s.isdigit():
        return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))
    return date.fromisoformat(s)


def is_business_day(d: date) -> bool:
    """Return True for Mon-Fri (weekday-only; holidays are a known false positive).

    Deliberately weekday-only — the ~9 NYSE/SEC holidays per year read as
    business days here. See the module docstring for why callers absorb this
    with threshold slack rather than a calendar.
    """
    return d.weekday() < 5


def business_days_between(start: date, end: date) -> int:
    """Count business days strictly between `start` and `end` (both exclusive).

    This is the "how many published index days has the cursor missed" measure:
    with `start` = the cursor's date and `end` = today, the count excludes both
    the already-processed cursor date and today (whose index may not have
    published yet), leaving the fully-elapsed business days the reconciler
    should have advanced through. Returns 0 when `end <= start`.
    """
    if end <= start:
        return 0
    count = 0
    current = date.fromordinal(start.toordinal() + 1)
    while current < end:
        if is_business_day(current):
            count += 1
        current = date.fromordinal(current.toordinal() + 1)
    return count
