/* Live tape auto-update for /live. Replaces the older banner-based
 * approach: new filings appear at the top of #live-tape automatically,
 * timestamps are localized to the viewer's timezone, no manual refresh
 * required.
 *
 * Reads its starting baseline from data-since on its own <script> tag
 * — keeps the page CSP at script-src 'self' (no inline scripts). The
 * baseline advances after every successful poll, anchored to the
 * newest fetched card's datetime attribute.
 */
(function () {
  "use strict";

  var script = document.currentScript;
  if (!script || !script.dataset || !script.dataset.since) {
    return;
  }
  var since = script.dataset.since;
  var POLL_INTERVAL_MS = 30000;

  /* localizeTimes converts every <time class="submitted-at"> inside
   * `scope` from its UTC fallback text to the viewer's local timezone.
   * The original UTC ISO is preserved in the datetime attribute, so
   * this is idempotent — re-running the function reads from the
   * attribute, not from the displayed text.
   */
  function localizeTimes(scope) {
    var nodes = scope.querySelectorAll("time.submitted-at");
    for (var i = 0; i < nodes.length; i++) {
      var t = nodes[i];
      var iso = t.getAttribute("datetime");
      if (!iso) continue;
      var d = new Date(iso);
      if (isNaN(d.getTime())) continue;
      t.textContent = d.toLocaleString([], {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      });
    }
  }

  /* removeEmptyPlaceholder removes the "No filings in this window yet"
   * paragraph once we successfully insert any cards. Otherwise it
   * lingers above the freshly-prepended content.
   */
  function removeEmptyPlaceholder() {
    var empty = document.getElementById("live-tape-empty");
    if (empty) empty.remove();
  }

  /* check polls the endpoint with the current since baseline, prepends
   * any returned card HTML to #live-tape, localizes the new times,
   * and advances since to the newest card's datetime attribute. */
  async function check() {
    try {
      var resp = await fetch(
        "/api/live-events?since=" + encodeURIComponent(since),
        { cache: "no-store" }
      );
      if (!resp.ok) return;
      var html = (await resp.text()).trim();
      if (!html) return;

      var container = document.getElementById("live-tape");
      if (!container) return;

      // Endpoint returns cards in DESC order; insertAdjacentHTML with
      // 'afterbegin' places the whole block at the top, preserving
      // newest-on-top semantics inside the prepended group.
      container.insertAdjacentHTML("afterbegin", html);
      removeEmptyPlaceholder();
      localizeTimes(container);

      // Advance the baseline to the newest card we just inserted. The
      // first <time.submitted-at> in the container is now the newest;
      // its datetime attribute is the verbatim ISO from the server.
      var newest = container.querySelector("time.submitted-at");
      if (newest) {
        var iso = newest.getAttribute("datetime");
        if (iso) since = iso;
      }
    } catch (e) {
      // Silent — a transient network blip shouldn't pollute the console.
    }
  }

  function start() {
    localizeTimes(document);
    check();
    setInterval(check, POLL_INTERVAL_MS);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
