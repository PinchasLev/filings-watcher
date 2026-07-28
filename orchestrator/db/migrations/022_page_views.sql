-- 022: page_views — lightweight server-side page-view log for engagement metrics.
--
-- Portable SQL — must compile and run identically on SQLite and Postgres. Append-only
-- with no surrogate key (an analytics log needs none). The application (the Go web
-- server) supplies viewed_at.
--
-- client_kind is the server's best-effort read of the User-Agent, in three buckets:
--   'human'     — a real browser
--   'crawler'   — a known indexer / link-preview / uptime bot (noise)
--   'automated' — a script or tool (curl, python, go-http, …) or an unknown
--                 non-browser client — the signal for API / automation appetite
-- It is a heuristic, not ground truth (a scraper can spoof a browser UA), but it lets
-- /ops separate human readers from bots from would-be automation consumers.
--
-- Privacy-light by design: we record the PATH, the Referer's HOST only (not the full
-- URL, so no query strings), the client_kind, and a visitor_day_hash — a salted hash
-- of (day, client IP, User-Agent) the server stores INSTEAD of the IP. The day is in
-- the hash, so it rotates every 24h: we can count unique visitors per day and views
-- per visitor without a cookie, without storing an IP, and with no identifier that
-- follows a person across days.
CREATE TABLE page_views (
    path             TEXT NOT NULL,
    referrer_host    TEXT NOT NULL DEFAULT '',
    client_kind      TEXT NOT NULL DEFAULT 'human',
    visitor_day_hash TEXT NOT NULL DEFAULT '',
    viewed_at        TEXT NOT NULL
);

CREATE INDEX idx_page_views_viewed_at ON page_views (viewed_at);
