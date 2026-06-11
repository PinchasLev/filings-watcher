// Operator dashboard at GET /ops/. Tailnet-only — the public Caddyfile
// blocks /ops/* with a 404 (ADR 0024); operators reach the Go service
// directly at 127.0.0.1:8080/ops/ via the mesh VPN (ADR 0014).
//
// Three panels, all read-only against existing tables:
//   - Trailing 30 days spend (budget-depletion signal)
//   - Trailing 24 hours: total + inline SVG hourly bar chart (behavior shape)
//   - Atom-ingest freshness (pipeline-health signal)
//
// The page is standalone HTML — it does not extend layout.html.tmpl
// because the operator dashboard has a different audience (tailnet
// operators, not public visitors) and shouldn't carry public-content
// chrome like the ticker-search form or the Latest/Live nav.
//
// No JavaScript. The 24h chart is server-rendered inline SVG; bar
// geometry is computed Go-side so the template stays declarative.
// A future "auto-refresh" affordance would be a `<meta http-equiv=
// "refresh">` (no JS, just a full reload) — not in this PR.

package server

import (
	"html/template"
	"net/http"
	"time"

	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

const (
	trailingBudgetHours   = 24 * 30 // 30 days
	trailingBehaviorHours = 24
	chartViewBoxWidth     = 600
	chartViewBoxHeight    = 120
	chartBarGapPx         = 2.0
	chartFloorBarHeightPx = 1.5 // visible "no data this hour" bar so the axis reads as 24 bars
)

var opsTemplate = template.Must(template.New("ops.html.tmpl").Funcs(templateFuncs).ParseFS(
	templateFS, "templates/ops.html.tmpl",
))

type opsPageData struct {
	// 30-day budget signal.
	Trailing30dTotalUSD float64
	Trailing30dCalls    int

	// 24h behavior signal — total + the bars for the SVG chart.
	Trailing24hTotalUSD float64
	Trailing24hCalls    int
	ChartBars           []chartBar
	ChartPeakUSD        float64
	ChartViewBoxWidth   int
	ChartViewBoxHeight  int

	// Pipeline freshness — verbatim ISO timestamp or nil.
	FreshnessTimestamp *string

	// Surface page staleness while there's no auto-refresh.
	RenderedAt string
}

// chartBar is the geometry for one <rect> in the inline SVG. Computed
// server-side so the template can be declarative — no math in the
// template language.
type chartBar struct {
	X         float64
	Y         float64
	Width     float64
	Height    float64
	HourStart string  // for the title-attribute tooltip
	TotalUSD  float64 // for the title-attribute tooltip
}

func handleOps(s storer) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		spend30d, err := s.TrailingHoursSpend(r.Context(), trailingBudgetHours)
		if err != nil {
			http.Error(w, "query failed", http.StatusInternalServerError)
			return
		}
		spend24h, err := s.TrailingHoursSpend(r.Context(), trailingBehaviorHours)
		if err != nil {
			http.Error(w, "query failed", http.StatusInternalServerError)
			return
		}
		buckets, err := s.HourlySpendBuckets(r.Context(), trailingBehaviorHours)
		if err != nil {
			http.Error(w, "query failed", http.StatusInternalServerError)
			return
		}
		freshness, err := s.AtomSnapshotFreshness(r.Context())
		if err != nil {
			http.Error(w, "query failed", http.StatusInternalServerError)
			return
		}

		bars, peak := buildChartBars(buckets)

		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		if err := opsTemplate.ExecuteTemplate(w, "ops.html.tmpl", opsPageData{
			Trailing30dTotalUSD: spend30d.TotalUSD,
			Trailing30dCalls:    spend30d.CallCount,
			Trailing24hTotalUSD: spend24h.TotalUSD,
			Trailing24hCalls:    spend24h.CallCount,
			ChartBars:           bars,
			ChartPeakUSD:        peak,
			ChartViewBoxWidth:   chartViewBoxWidth,
			ChartViewBoxHeight:  chartViewBoxHeight,
			FreshnessTimestamp:  freshness,
			RenderedAt:          time.Now().UTC().Format(time.RFC3339),
		}); err != nil {
			_ = err
		}
	}
}

// (No formatUSD helper here — templates render currency with the
// existing printf "%.2f" pattern, matching the rest of the site.)

// buildChartBars maps an ordered list of buckets to SVG-coordinate bar
// geometry plus the peak value (used for the y-axis label). Bars are
// normalized so the largest bucket fills the chart height. If every
// bucket is zero, every bar gets the small "floor" height so the
// x-axis still reads as 24 evenly-spaced markers rather than nothing.
func buildChartBars(buckets []store.HourlyBucket) ([]chartBar, float64) {
	n := len(buckets)
	if n == 0 {
		return nil, 0
	}

	peak := 0.0
	for _, b := range buckets {
		if b.TotalUSD > peak {
			peak = b.TotalUSD
		}
	}

	barWidth := (float64(chartViewBoxWidth) - chartBarGapPx*float64(n-1)) / float64(n)
	bars := make([]chartBar, 0, n)
	for i, b := range buckets {
		var h float64
		if peak > 0 {
			h = (b.TotalUSD / peak) * float64(chartViewBoxHeight)
		}
		if h < chartFloorBarHeightPx {
			h = chartFloorBarHeightPx
		}
		bars = append(bars, chartBar{
			X:         float64(i) * (barWidth + chartBarGapPx),
			Y:         float64(chartViewBoxHeight) - h,
			Width:     barWidth,
			Height:    h,
			HourStart: b.HourStart,
			TotalUSD:  b.TotalUSD,
		})
	}
	return bars, peak
}
