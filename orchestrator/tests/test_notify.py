"""Tests for the notification transport seam (ADR 0031).

Covers the Discord rendering (transport-neutral Notification -> webhook JSON)
and that DiscordNotifier POSTs the payload and surfaces non-2xx as an error the
drainer can retry on. Uses httpx.MockTransport so no network is touched.
"""

from __future__ import annotations

import json

import httpx
import pytest

from filings_orchestrator.alerting import ALERT, INFO
from filings_orchestrator.alerting.notify import (
    DiscordNotifier,
    Notification,
    _render_discord,
)
from filings_orchestrator.alerting.outbox import Episode


def test_render_includes_title_body_and_fields() -> None:
    payload = _render_discord(
        Notification(
            ALERT, "Handler panic recovered", "GET /live panicked: boom", {"path": "/live"}
        )
    )
    embed = payload["embeds"][0]
    assert embed["title"] == "Handler panic recovered"
    assert embed["description"] == "GET /live panicked: boom"
    assert embed["fields"] == [{"name": "path", "value": "/live", "inline": True}]


def test_render_omits_description_and_fields_when_empty() -> None:
    embed = _render_discord(Notification(INFO, "Just a headline", None, {}))["embeds"][0]
    assert "description" not in embed
    assert "fields" not in embed


def test_render_colors_differ_by_severity() -> None:
    alert_color = _render_discord(Notification(ALERT, "a", None, {}))["embeds"][0]["color"]
    info_color = _render_discord(Notification(INFO, "i", None, {}))["embeds"][0]["color"]
    assert alert_color != info_color


def test_render_stringifies_non_string_field_values() -> None:
    embed = _render_discord(Notification(ALERT, "t", None, {"attempts": 3}))["embeds"][0]
    assert embed["fields"] == [{"name": "attempts", "value": "3", "inline": True}]


def test_discord_notifier_posts_payload_to_webhook() -> None:
    captured: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append((str(request.url), json.loads(request.content)))
        return httpx.Response(204)

    notifier = DiscordNotifier(httpx.Client(transport=httpx.MockTransport(handler)))
    notifier.send("https://discord.test/webhook/abc", Notification(ALERT, "t", "b", {}))

    assert len(captured) == 1
    url, body = captured[0]
    assert url == "https://discord.test/webhook/abc"
    assert body["embeds"][0]["title"] == "t"


def test_discord_notifier_raises_on_non_2xx() -> None:
    notifier = DiscordNotifier(
        httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(500)))
    )
    with pytest.raises(httpx.HTTPStatusError):
        notifier.send("https://discord.test/webhook/abc", Notification(INFO, "t", None, {}))


# --- Status and episode: the message says whether it is new, ongoing, or cleared ---

_EPISODE = Episode(
    first_seen="2026-10-26T11:15:00+00:00",
    last_seen="2026-11-25T11:01:00+00:00",
    occurrences=37464,
)


def _note(status: str | None, episode: Episode | None = _EPISODE) -> dict[str, object]:
    payload = _render_discord(
        Notification(
            severity=ALERT,
            title="Anthropic credit exhausted",
            body=None,
            fields={"error_class": "BadRequestError"},
            status=status,
            episode=episode,
        )
    )
    embeds = payload["embeds"]
    assert isinstance(embeds, list)
    embed = embeds[0]
    assert isinstance(embed, dict)
    return embed


def _fields(embed: dict[str, object]) -> dict[str, str]:
    grid = embed["fields"]
    assert isinstance(grid, list)
    return {f["name"]: f["value"] for f in grid}


def test_ongoing_reminder_says_so_and_carries_its_history() -> None:
    embed = _note("ongoing")
    assert embed["title"] == "Still failing: Anthropic credit exhausted"
    fields = _fields(embed)
    assert fields["Status"] == "ongoing"
    assert fields["Occurrences"] == "37464"
    assert fields["First seen"] == "2026-10-26 11:15 UTC"
    assert fields["Lasted"] == "29d 23h"
    assert fields["error_class"] == "BadRequestError"  # the producer's own fields still follow


def test_cleared_reads_green_with_its_span() -> None:
    embed = _note("cleared")
    assert embed["title"] == "Cleared: Anthropic credit exhausted"
    assert embed["color"] == 0x22C55E
    assert _fields(embed)["Status"] == "cleared"


def test_new_alert_keeps_its_plain_title_and_alert_color() -> None:
    one = Episode(
        first_seen="2026-11-26T10:00:00+00:00", last_seen="2026-11-26T10:00:00+00:00", occurrences=1
    )
    embed = _note("new", one)
    assert embed["title"] == "Anthropic credit exhausted"
    assert embed["color"] == 0xE01E2B
    fields = _fields(embed)
    assert fields["Occurrences"] == "1"
    assert "Lasted" not in fields  # a single occurrence has no span


def test_statuses_are_distinguishable_by_color() -> None:
    colors = {_note(s)["color"] for s in ("new", "ongoing", "cleared")}
    assert len(colors) == 3
