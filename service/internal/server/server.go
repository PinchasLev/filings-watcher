// Package server wires HTTP handlers over the store. Uses stdlib net/http
// with the Go 1.22+ pattern-routing syntax per ADR 0009 (no third-party
// HTTP framework).
package server

import (
	"context"
	"net/http"
	"time"

	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

// storer is the narrow interface this package needs from the store. Defined
// here (at the point of use) rather than in the store package, so handlers
// depend on the subset they actually call, and tests can supply a small fake.
// Idiomatic Go: "accept interfaces, return structs."
type storer interface {
	// JSON list endpoint (/filings): raw per-Item classifications.
	LatestClassifications(ctx context.Context, limit, offset int) ([]store.Classification, int, error)
	// Filing detail: the filing plus its per-Item classifications (JSON back-compat).
	FilingByAccession(ctx context.Context, accession string) (*store.FilingDetail, error)
	LookupCIKByTicker(ctx context.Context, ticker string) (string, error)
	// Events layer (ADR 0027/0028): the HTML home and per-company lists, their
	// filter-chip counts, and the detail page's per-event drill-down.
	MaterialEvents(ctx context.Context, eventType string, limit, offset int) ([]store.Event, int, error)
	CompanyEvents(ctx context.Context, cik string, limit, offset int) (*store.Company, []store.Event, int, error)
	// Insider (Form 4) surfacing on the company page.
	CompanyInsiderPulse(ctx context.Context, cik string, windowDays int) (store.InsiderPulse, error)
	CompanyInsiderTrades(ctx context.Context, cik string, limit int) ([]store.InsiderTrade, error)
	// NotableInsiderActivity backs the /insiders feed of recent cluster buys.
	NotableInsiderActivity(ctx context.Context, windowDays, limit int) ([]store.InsiderCluster, error)
	// LiveEvents backs the /live tape: near-real-time material events sorted
	// by precise EDGAR-side submission time within a rolling window. Implicit
	// atom-feed-only via the submitted_at IS NOT NULL filter in the query.
	LiveEvents(ctx context.Context, since time.Time, limit, offset int) ([]store.Event, int, error)
	// ListLiveEventsSince backs /api/live-events: the AJAX path that
	// the live tape's JS uses to fetch and prepend new cards.
	ListLiveEventsSince(ctx context.Context, since time.Time, limit int) ([]store.Event, error)
	MaterialEventTypeCounts(ctx context.Context) ([]store.EventTypeCount, error)
	EventsByAccession(ctx context.Context, accession string) ([]store.EventWithItems, error)
	// Operator dashboard at /ops/. Tailnet-only via Caddy's public 404
	// on /ops/* (ADR 0024). Cost trajectory and ingest freshness over
	// rolling windows (not calendar-aligned).
	TrailingHoursSpend(ctx context.Context, hours int) (store.SpendSnapshot, error)
	HourlySpendBuckets(ctx context.Context, hours int) ([]store.HourlyBucket, error)
	DailySpendBuckets(ctx context.Context, days int) ([]store.DailyBucket, error)
	SpendDataStartDate(ctx context.Context) (string, error)
	AtomSnapshotFreshness(ctx context.Context) (*string, error)
}

// New returns an http.Handler with all routes registered.
func New(s storer) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", handleHealth)
	mux.HandleFunc("GET /filings", handleListFilings(s))
	mux.HandleFunc("GET /filings/{accession}", handleFilingDetail(s))
	mux.HandleFunc("GET /companies/{cik}", handleCompany(s))
	mux.HandleFunc("GET /insiders", handleInsiders(s))
	mux.HandleFunc("GET /live", handleLive(s))
	mux.HandleFunc("GET /api/live-events", handleLiveEvents(s))
	mux.HandleFunc("GET /static/live.js", handleLiveScript())
	mux.HandleFunc("GET /ops/", handleOps(s))
	mux.HandleFunc("GET /", handleHome(s))
	return mux
}
