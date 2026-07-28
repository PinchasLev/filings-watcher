// Server-side page-view logging (visit tracking). A middleware records a row per
// page view so /ops can show engagement — the honest demand signal before any
// monetization. Deliberately privacy-light: no IP, no cookie, no cross-day
// identifier; just the path, the referrer's host, a client-kind read of the
// User-Agent, and a day-rotating visitor hash.
//
// Best-effort: the row is written off the request path in a goroutine and its error
// is ignored, so analytics can never slow or fail a page render.

package server

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// Client kinds recorded per view.
const (
	clientHuman     = "human"
	clientCrawler   = "crawler"
	clientAutomated = "automated"
)

// viewSalt is a per-process random salt folded into the visitor hash so stored
// hashes are not reproducible from (day, IP, UA) alone. Regenerated on restart —
// which at worst mildly fragments a day's unique count across a deploy.
var viewSalt = newSalt()

func newSalt() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		return "filings-watcher-fallback-salt"
	}
	return hex.EncodeToString(b)
}

// pageViewLogger is the narrow slice of the store the middleware needs.
type pageViewLogger interface {
	LogPageView(ctx context.Context, path, referrerHost, clientKind, visitorHash, viewedAt string) error
}

// logPageViews wraps a handler, recording a view for each GET to a tracked page.
func logPageViews(s pageViewLogger, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		next.ServeHTTP(w, r)
		if r.Method != http.MethodGet || !isTrackedPage(r.URL.Path) {
			return
		}
		path := r.URL.Path
		host := referrerHost(r.Referer())
		kind := classifyClient(r.UserAgent())
		hash := visitorDayHash(clientIP(r), r.UserAgent())
		viewedAt := time.Now().UTC().Format(time.RFC3339)
		go func() {
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			_ = s.LogPageView(ctx, path, host, kind, hash, viewedAt)
		}()
	})
}

// isTrackedPage allowlists the human-facing HTML pages. An allowlist (not an
// exclude-list) keeps bot probes of random paths (/wp-admin, …) out of the log, and
// excludes /health, /static, /api, and /ops (the dashboard itself).
func isTrackedPage(p string) bool {
	switch p {
	case "/", "/live", "/radar", "/insiders", "/filings":
		return true
	}
	return strings.HasPrefix(p, "/companies/") || strings.HasPrefix(p, "/filings/")
}

// referrerHost extracts just the host from a Referer header — enough to see where
// traffic comes from, without logging the full URL or its query string.
func referrerHost(ref string) string {
	if ref == "" {
		return ""
	}
	u, err := url.Parse(ref)
	if err != nil {
		return ""
	}
	return u.Host
}

// clientIP returns the originating client IP, reading X-Forwarded-For (set by the
// Caddy reverse proxy) first, then falling back to RemoteAddr. Used only to compute
// the day-rotating visitor hash — never stored.
func clientIP(r *http.Request) string {
	if xff := r.Header.Get("X-Forwarded-For"); xff != "" {
		if i := strings.IndexByte(xff, ','); i >= 0 {
			return strings.TrimSpace(xff[:i])
		}
		return strings.TrimSpace(xff)
	}
	if host, _, err := net.SplitHostPort(r.RemoteAddr); err == nil {
		return host
	}
	return r.RemoteAddr
}

// visitorDayHash is a salted hash of (day, IP, UA) stored instead of the IP. The day
// component makes it rotate every 24h, so it counts unique visitors within a day
// without following anyone across days. Empty IP yields an empty hash (uncounted).
func visitorDayHash(ip, ua string) string {
	if ip == "" {
		return ""
	}
	day := time.Now().UTC().Format("2006-01-02")
	sum := sha256.Sum256([]byte(viewSalt + "|" + day + "|" + ip + "|" + ua))
	return hex.EncodeToString(sum[:8])
}

// crawlerMarkers are substrings of known indexers, link-preview, and uptime bots.
var crawlerMarkers = []string{
	"bot", "crawl", "spider", "slurp", "bingpreview", "facebookexternalhit",
	"embedly", "quora link preview", "pingdom", "uptimerobot", "statuscake",
	"googlebot", "bingbot", "duckduckbot", "yandex", "baiduspider", "ahrefs", "semrush",
}

// toolMarkers are substrings of programmatic tools / libraries and headless browsers
// — automation, i.e. would-be API consumers.
var toolMarkers = []string{
	"curl", "wget", "python-requests", "python-urllib", "go-http-client", "httpx",
	"okhttp", "java/", "libwww", "postman", "insomnia", "axios", "node-fetch",
	"scrapy", "headless", "phantomjs", "puppeteer", "playwright", "restsharp",
}

// classifyClient buckets a User-Agent into human / crawler / automated. Order
// matters: known crawlers first, then tools, then real browsers; an unknown
// non-browser UA is treated as automated, and an empty UA as automated (a script,
// not a crawler that would identify itself).
func classifyClient(ua string) string {
	u := strings.ToLower(strings.TrimSpace(ua))
	if u == "" {
		return clientAutomated
	}
	for _, m := range crawlerMarkers {
		if strings.Contains(u, m) {
			return clientCrawler
		}
	}
	for _, m := range toolMarkers {
		if strings.Contains(u, m) {
			return clientAutomated
		}
	}
	if strings.Contains(u, "mozilla") || strings.Contains(u, "applewebkit") || strings.Contains(u, "gecko") {
		return clientHuman
	}
	return clientAutomated
}
