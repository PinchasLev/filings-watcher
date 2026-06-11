// Operator dashboard at GET /ops/. Tailnet-only — the public Caddyfile
// blocks /ops/* with a 404 (ADR 0024); operators reach the Go service
// directly at 127.0.0.1:8080/ops/ via the mesh VPN (ADR 0014).
//
// Five panels, all read-only against existing tables:
//   - Trailing 30 days spend (budget-depletion signal)
//   - Trailing 24 hours spend (recent-behavior total)
//   - Atom-ingest freshness (pipeline-health signal)
//   - Hourly spend chart, last 24 hours (intra-day shape)
//   - Daily spend chart, last 30 days (intra-month shape)
//
// The page is standalone HTML — it does not extend layout.html.tmpl
// because the operator dashboard has a different audience (tailnet
// operators, not public visitors) and shouldn't carry public-content
// chrome like the ticker-search form or the Latest/Live nav.
//
// No JavaScript. Both charts are server-rendered inline SVG; bar
// geometry and Y-axis ticks are computed Go-side so the template stays
// declarative. The two charts share the same SVG machinery: hourly and
// daily buckets each map to a chartSource ({Label, TotalUSD}) and
// flow through the same builder.

package server

import (
	"fmt"
	"html/template"
	"net/http"
	"time"

	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

const (
	trailingBudgetHours   = 24 * 30 // 30 days
	trailingBehaviorHours = 24
	trailingChartDays     = 30
	chartViewBoxWidth     = 600
	chartViewBoxHeight    = 120
	chartLeftPadPx        = 56 // room for Y-axis labels (e.g., "$0.0688")
	chartBarGapPx         = 2.0
	chartFloorBarHeightPx = 1.5 // visible "no data" bar so the axis reads as N bars
)

var opsTemplate = template.Must(template.New("ops.html.tmpl").Funcs(templateFuncs).ParseFS(
	templateFS, "templates/ops.html.tmpl",
))

type opsPageData struct {
	// 30-day budget signal.
	Trailing30dTotalUSD float64
	Trailing30dCalls    int

	// 24h behavior signal — total + the hourly chart.
	Trailing24hTotalUSD float64
	Trailing24hCalls    int

	// Pipeline freshness — verbatim ISO timestamp or nil.
	FreshnessTimestamp *string

	// Two charts, same SVG machinery. The 24h hourly chart answers
	// "shape of the day"; the 30d daily chart answers "shape of the
	// month" — different time scales of the same question.
	HourlyChart chartView
	DailyChart  chartView

	// SVG viewBox + label-positioning are shared across both charts
	// (the panels are visually consistent).
	ChartViewBoxWidth  int
	ChartViewBoxHeight int
	ChartLeftPad       int
	ChartLabelX        int // X coordinate for right-aligned Y-axis label text (left pad minus a small gap)

	// Surface page staleness while there's no auto-refresh.
	RenderedAt string
}

// chartView is everything the template needs to render one bar chart
// panel: the bar geometry, the Y-axis ticks, and the peak value the
// ticks were derived from (used as a secondary label, when wanted).
type chartView struct {
	Bars       []chartBar
	YAxisTicks []chartAxisTick
	PeakUSD    float64
}

// chartSource is the input to the chart-building helpers. It carries
// only the data they need — a label (for the tooltip) and a value (for
// the bar height). Both the hourly and daily buckets convert to this
// type so the chart code is fully shared.
type chartSource struct {
	Label    string
	TotalUSD float64
}

// chartBar is the geometry for one <rect> in the inline SVG. Computed
// server-side so the template can be declarative — no math in the
// template language.
type chartBar struct {
	X           float64
	Y           float64
	Width       float64
	Height      float64
	BucketLabel string  // for the title-attribute tooltip (hour or day boundary)
	TotalUSD    float64 // for the title-attribute tooltip
}

// chartAxisTick is one Y-axis label + gridline coordinate. Y is the
// SVG-space pixel where the label baseline and the gridline both sit.
// Label is pre-formatted ($0.0000 style) so the template doesn't carry
// formatting noise.
type chartAxisTick struct {
	Y     float64
	Label string
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
		hourlyBuckets, err := s.HourlySpendBuckets(r.Context(), trailingBehaviorHours)
		if err != nil {
			http.Error(w, "query failed", http.StatusInternalServerError)
			return
		}
		dailyBuckets, err := s.DailySpendBuckets(r.Context(), trailingChartDays)
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
		if err := opsTemplate.ExecuteTemplate(w, "ops.html.tmpl", opsPageData{
			Trailing30dTotalUSD: spend30d.TotalUSD,
			Trailing30dCalls:    spend30d.CallCount,
			Trailing24hTotalUSD: spend24h.TotalUSD,
			Trailing24hCalls:    spend24h.CallCount,
			HourlyChart:         buildChartView(hourlySources(hourlyBuckets)),
			DailyChart:          buildChartView(dailySources(dailyBuckets)),
			ChartViewBoxWidth:   chartViewBoxWidth,
			ChartViewBoxHeight:  chartViewBoxHeight,
			ChartLeftPad:        chartLeftPadPx,
			ChartLabelX:         chartLeftPadPx - 4,
			FreshnessTimestamp:  freshness,
			RenderedAt:          time.Now().UTC().Format(time.RFC3339),
		}); err != nil {
			_ = err
		}
	}
}

// hourlySources formats hourly buckets for the chart. The tooltip label
// reads "YYYY-MM-DD HH:00 UTC" so the operator can see the absolute hour
// at a glance.
func hourlySources(buckets []store.HourlyBucket) []chartSource {
	out := make([]chartSource, len(buckets))
	for i, b := range buckets {
		out[i] = chartSource{Label: formatHourLabel(b.HourStart), TotalUSD: b.TotalUSD}
	}
	return out
}

// dailySources formats daily buckets for the chart. The tooltip is
// date-only — the 00:00:00 UTC suffix adds nothing.
func dailySources(buckets []store.DailyBucket) []chartSource {
	out := make([]chartSource, len(buckets))
	for i, b := range buckets {
		out[i] = chartSource{Label: formatDayLabel(b.DayStart), TotalUSD: b.TotalUSD}
	}
	return out
}

func formatHourLabel(iso string) string {
	t, err := time.Parse(time.RFC3339, iso)
	if err != nil {
		return iso
	}
	return t.UTC().Format("2006-01-02 15:00 UTC")
}

func formatDayLabel(iso string) string {
	t, err := time.Parse(time.RFC3339, iso)
	if err != nil {
		return iso
	}
	return t.UTC().Format("2006-01-02")
}

// buildChartView packages the bars + Y-axis ticks for one chart panel.
// Single entry point so the handler doesn't carry the two-call rhythm.
func buildChartView(src []chartSource) chartView {
	bars, peak := buildChartBars(src)
	ticks := buildYAxisTicks(peak)
	return chartView{Bars: bars, YAxisTicks: ticks, PeakUSD: peak}
}

// (No formatUSD helper here — templates render currency with the
// existing printf "%.2f" pattern, matching the rest of the site.)

// buildChartBars maps an ordered list of chart sources (label + value)
// to SVG-coordinate bar geometry plus the peak value used for the Y-axis
// labels. Bars are normalized so the largest source fills the chart
// height. If every source is zero, every bar gets the small "floor"
// height so the x-axis still reads as N evenly-spaced markers rather
// than nothing.
//
// Bars start at X = chartLeftPadPx to leave room on the left for the
// Y-axis labels.
func buildChartBars(src []chartSource) ([]chartBar, float64) {
	n := len(src)
	if n == 0 {
		return nil, 0
	}

	peak := 0.0
	for _, s := range src {
		if s.TotalUSD > peak {
			peak = s.TotalUSD
		}
	}

	plotWidth := float64(chartViewBoxWidth - chartLeftPadPx)
	barWidth := (plotWidth - chartBarGapPx*float64(n-1)) / float64(n)
	bars := make([]chartBar, 0, n)
	for i, s := range src {
		var h float64
		if peak > 0 {
			h = (s.TotalUSD / peak) * float64(chartViewBoxHeight)
		}
		if h < chartFloorBarHeightPx {
			h = chartFloorBarHeightPx
		}
		bars = append(bars, chartBar{
			X:           float64(chartLeftPadPx) + float64(i)*(barWidth+chartBarGapPx),
			Y:           float64(chartViewBoxHeight) - h,
			Width:       barWidth,
			Height:      h,
			BucketLabel: s.Label,
			TotalUSD:    s.TotalUSD,
		})
	}
	return bars, peak
}

// buildYAxisTicks returns three labeled Y-axis ticks: $0 at the
// baseline, peak/2 at mid-chart, and the peak at the top. When the
// peak is zero (no data window), only the $0 baseline tick is
// returned — the mid/peak labels would both read "$0.0000" and add
// no information.
func buildYAxisTicks(peak float64) []chartAxisTick {
	if peak <= 0 {
		return []chartAxisTick{
			{Y: float64(chartViewBoxHeight), Label: "$0.0000"},
		}
	}
	return []chartAxisTick{
		{Y: 0, Label: fmt.Sprintf("$%.4f", peak)},
		{Y: float64(chartViewBoxHeight) / 2, Label: fmt.Sprintf("$%.4f", peak/2)},
		{Y: float64(chartViewBoxHeight), Label: "$0.0000"},
	}
}
