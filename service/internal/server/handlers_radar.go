// Server-rendered Risk Radar feed at GET /radar. The cross-company front door for
// disclosure change-detection (ADR 0042/0043): recent 10-K filings whose Risk Factors
// surfaced company-specific changes year over year, newest first, each listing its top
// company-specific changes and linking through to the per-company page.
//
// This is the "pushable" surface the per-company page is not: you reach it without
// already knowing which company to look at. No JavaScript; pagination is query-param
// state like the home page.

package server

import (
	"html/template"
	"net/http"
	"strconv"

	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

const radarPageLimit = 40

var radarTemplate = template.Must(template.New("layout.html.tmpl").Funcs(templateFuncs).ParseFS(
	templateFS, "templates/layout.html.tmpl", "templates/radar.html.tmpl",
))

type radarPageData struct {
	Nav           string
	FilteredTotal int
	Tracked       int
	WithSpecific  int
	Rows          []store.RiskRadarRow
	RangeStart    int
	RangeEnd      int
	PrevURL       string
	NextURL       string
}

func handleRadar(s storer) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		offset := parseOffset(r.URL.Query().Get("offset"))

		rows, total, err := s.RecentDisclosureChanges(r.Context(), radarPageLimit, offset)
		if err != nil {
			http.Error(w, "query failed", http.StatusInternalServerError)
			return
		}

		// Coverage is a header stat; a failure here shouldn't take down the feed, so
		// degrade to hiding the line (Tracked == 0) rather than 500ing.
		tracked, withSpecific, err := s.RiskRadarCoverage(r.Context())
		if err != nil {
			tracked, withSpecific = 0, 0
		}

		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		if err := radarTemplate.ExecuteTemplate(w, "layout.html.tmpl", radarPageData{
			Nav:           "radar",
			FilteredTotal: total,
			Tracked:       tracked,
			WithSpecific:  withSpecific,
			Rows:          rows,
			RangeStart:    pageRangeStart(offset, len(rows)),
			RangeEnd:      pageRangeEnd(offset, len(rows)),
			PrevURL:       radarPageURL(offset-radarPageLimit, true),
			NextURL:       radarPageURL(offset+radarPageLimit, offset+radarPageLimit < total),
		}); err != nil {
			// Headers already written; can't change status.
			_ = err
		}
	}
}

// radarPageURL builds a prev/next link for the feed. Returns "" when disabled or the
// offset is negative — the template renders an empty URL as a disabled control.
func radarPageURL(targetOffset int, enabled bool) string {
	if !enabled || targetOffset < 0 {
		return ""
	}
	if targetOffset == 0 {
		return "/radar"
	}
	return "/radar?offset=" + strconv.Itoa(targetOffset)
}
