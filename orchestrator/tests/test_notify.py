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
