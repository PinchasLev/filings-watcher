"""Notification transport seam (ADR 0031).

The drainer holds a transport-neutral `Notification` value — severity, title,
body, structured fields — and hands it to a `Notifier`, the only component that
knows a wire format. `DiscordNotifier` renders it to Discord's webhook JSON and
POSTs it. A later move to Slack is one new `Notifier` implementation plus a
different webhook URL in Parameter Store; nothing upstream (the outbox, the
emit() call sites, the drainer's routing) changes.

The split is deliberate: a producer states a severity and never names a channel
or a provider; the drainer maps severity -> webhook URL and owns delivery
policy; the `Notifier` owns only rendering. Three concerns, three seams.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import httpx

from filings_orchestrator.alerting.emit import ALERT
from filings_orchestrator.alerting.outbox import Episode

# Discord embed accent colors (decimal RGB). Red draws the eye for things that
# need action; a calm blue for situational awareness. Severity, not the
# provider, picks the intent; the provider picks how to express it. An alert's
# status then shades it: amber while a condition is still going, green once it
# clears, so the channel reads as a timeline at a glance.
_COLOR_ALERT = 0xE01E2B
_COLOR_INFO = 0x3B82F6
_COLOR_ONGOING = 0xF59E0B
_COLOR_CLEARED = 0x22C55E

# Where a keyed alert stands in its episode. A keyless alert has no episode and no status.
NEW = "new"
ONGOING = "ongoing"
CLEARED = "cleared"

_TITLE_PREFIX = {ONGOING: "Still failing: ", CLEARED: "Cleared: "}


@dataclass(frozen=True)
class Notification:
    """A delivery-ready alert, independent of any provider's wire format.

    `status` and `episode` are set by the drainer for a keyed alert, so the message itself says
    whether this is the first trip, a reminder that it is still happening, or the all-clear — and
    over what span and how many occurrences. Without them a month-old condition's reminder reads
    exactly like the first page.
    """

    severity: str
    title: str
    body: str | None
    fields: dict[str, object]
    status: str | None = None
    episode: Episode | None = None


class Notifier(Protocol):
    """Renders a `Notification` to a provider and POSTs it to `webhook_url`.

    Implementations raise on a non-2xx response (or transport error) so the
    drainer can record the failure and retry the row on the next pass.
    """

    def send(self, webhook_url: str, notification: Notification) -> None: ...


class DiscordNotifier:
    """Renders notifications as Discord embeds and delivers via incoming webhook.

    Holds an injected `httpx.Client` so tests can supply a transport and the
    process can reuse one connection pool across a drain pass.
    """

    def __init__(self, client: httpx.Client, *, timeout: float = 10.0) -> None:
        self._client = client
        self._timeout = timeout

    def send(self, webhook_url: str, notification: Notification) -> None:
        response = self._client.post(
            webhook_url, json=_render_discord(notification), timeout=self._timeout
        )
        # 204 No Content is Discord's success for a webhook POST; any non-2xx
        # (rate limit, bad URL, outage) raises and the drainer retries the row.
        response.raise_for_status()


def _render_discord(notification: Notification) -> dict[str, object]:
    """Build the Discord webhook payload for one notification.

    Uses an embed (not a plain `content` string) so the accent color carries
    severity at a glance and structured fields render as a tidy grid. Empty
    `fields` simply omits the grid; a None `body` omits the description.
    """
    embed: dict[str, object] = {
        "title": _TITLE_PREFIX.get(notification.status or "", "") + notification.title,
        "color": _color(notification),
    }
    if notification.body:
        embed["description"] = notification.body
    grid = [*_episode_fields(notification), *notification.fields.items()]
    if grid:
        embed["fields"] = [
            {"name": key, "value": str(value), "inline": True} for key, value in grid
        ]
    return {"embeds": [embed]}


def _color(notification: Notification) -> int:
    if notification.status == CLEARED:
        return _COLOR_CLEARED
    if notification.severity != ALERT:
        return _COLOR_INFO
    return _COLOR_ONGOING if notification.status == ONGOING else _COLOR_ALERT


def _episode_fields(notification: Notification) -> list[tuple[str, object]]:
    """The episode as leading grid fields — what separates a one-off from an ongoing condition."""
    ep = notification.episode
    if ep is None:
        return []
    fields: list[tuple[str, object]] = [
        ("Status", notification.status or NEW),
        ("Occurrences", ep.occurrences),
        ("First seen", _stamp(ep.first_seen)),
    ]
    if ep.occurrences > 1 or notification.status == CLEARED:
        fields.append(("Last seen", _stamp(ep.last_seen)))
        fields.append(("Lasted", _span(ep.first_seen, ep.last_seen)))
    return fields


def _stamp(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M UTC")


def _span(first: str, last: str) -> str:
    """A human duration: "3d 4h", "2h 15m", "under a minute"."""
    seconds = int((datetime.fromisoformat(last) - datetime.fromisoformat(first)).total_seconds())
    days, rem = divmod(seconds, 86_400)
    hours, rem = divmod(rem, 3_600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m" if minutes else "under a minute"
