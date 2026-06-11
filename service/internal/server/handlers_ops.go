// Operator dashboard at GET /ops/. Tailnet-only — the public Caddyfile
// blocks /ops/* with a 404 (ADR 0024); operators reach the Go service
// directly at 127.0.0.1:8080/ops/ via the mesh VPN (ADR 0014).
//
// First cut: two panels. Cost trajectory against today's cap, and
// the atom-ingest snapshot freshness. Both read from existing tables;
// no new schema. The richer panels (per-tick history, timer status,
// publication signals, OTel collector export rate) stack on top later.

package server

import (
	"html/template"
	"net/http"
	"time"
)

// dailyCostCapUSD mirrors the orchestrator wrapper's
// ANTHROPIC_DAILY_COST_CAP_USD default. The pre-tick gate enforces this
// value in the orchestrator; the dashboard surfaces how close we are.
// If the wrapper diverges from this constant in the future, plumb both
// through a shared config source instead.
const dailyCostCapUSD = 5.00

var opsTemplate = template.Must(template.New("layout.html.tmpl").Funcs(templateFuncs).ParseFS(
	templateFS, "templates/layout.html.tmpl", "templates/ops.html.tmpl",
))

type opsPageData struct {
	Nav string

	SpendTotalUSD     float64
	SpendCallCount    int
	SpendCapUSD       float64
	SpendPercentOfCap float64

	// FreshnessTimestamp is the latest submitted_at across all filings,
	// verbatim. Nil when the corpus has no atom-ingested rows yet.
	FreshnessTimestamp *string

	// RenderedAt is the page's render time (UTC). Surfaces page staleness
	// while there's no auto-refresh.
	RenderedAt string
}

func handleOps(s storer) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		spend, err := s.TodaySpend(r.Context())
		if err != nil {
			http.Error(w, "query failed", http.StatusInternalServerError)
			return
		}
		freshness, err := s.AtomSnapshotFreshness(r.Context())
		if err != nil {
			http.Error(w, "query failed", http.StatusInternalServerError)
			return
		}

		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		if err := opsTemplate.ExecuteTemplate(w, "layout.html.tmpl", opsPageData{
			Nav:                "",
			SpendTotalUSD:      spend.TotalUSD,
			SpendCallCount:     spend.CallCount,
			SpendCapUSD:        dailyCostCapUSD,
			SpendPercentOfCap:  100.0 * spend.TotalUSD / dailyCostCapUSD,
			FreshnessTimestamp: freshness,
			RenderedAt:         time.Now().UTC().Format(time.RFC3339),
		}); err != nil {
			_ = err
		}
	}
}
