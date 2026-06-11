/* Freshness banner for the /live tape. Polls /api/live-since on a short
 * interval; when the server reports new material atom-ingested events
 * filed after the page rendered, surfaces a sticky banner inviting the
 * operator to refresh.
 *
 * Reads its baseline timestamp from data-since on its own <script> tag
 * — keeps the page CSP at script-src 'self' (no inline scripts) and
 * lets the server pin "since" to the exact moment the page rendered.
 */
(function () {
  "use strict";

  var script = document.currentScript;
  if (!script || !script.dataset || !script.dataset.since) {
    return;
  }
  var since = script.dataset.since;
  var POLL_INTERVAL_MS = 30000;

  var banner = document.createElement("div");
  banner.className = "live-banner";
  banner.hidden = true;
  banner.setAttribute("role", "status");
  banner.setAttribute("aria-live", "polite");

  var link = document.createElement("a");
  link.href = "/live";
  link.className = "live-banner-link";
  banner.appendChild(link);

  // Insert at the top of the main container so it sits above the tape
  // but inside the visible content area.
  function mountBanner() {
    var container = document.querySelector("main.container");
    if (container && container.firstChild) {
      container.insertBefore(banner, container.firstChild);
    } else {
      document.body.insertBefore(banner, document.body.firstChild);
    }
  }

  function renderCount(n) {
    if (n <= 0) {
      banner.hidden = true;
      return;
    }
    link.textContent =
      n + " new filing" + (n === 1 ? "" : "s") + " since you loaded — refresh";
    banner.hidden = false;
  }

  async function check() {
    try {
      var resp = await fetch(
        "/api/live-since?since=" + encodeURIComponent(since),
        { cache: "no-store" }
      );
      if (!resp.ok) {
        return;
      }
      var data = await resp.json();
      if (typeof data.new_count === "number") {
        renderCount(data.new_count);
      }
    } catch (e) {
      // Silent — a transient network blip shouldn't pollute the console.
    }
  }

  // Wait for DOM ready before mounting the banner, then start polling.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      mountBanner();
      check();
    });
  } else {
    mountBanner();
    check();
  }
  setInterval(check, POLL_INTERVAL_MS);
})();
