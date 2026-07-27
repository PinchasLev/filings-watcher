// Server-rendered Risk Radar feed at GET /radar. The cross-company front door for
// disclosure change-detection (ADR 0042/0043): recent 10-K filings whose Risk
// Factors materially shifted year over year, newest first, each a scannable headline
// verdict (direction + intensity + counts + thesis) linking through to the
// per-company page for the full themed evidence.
//
// This is the "pushable" surface the per-company page is not: you reach it without
// already knowing which company to look at. Filterable by intensity via query param;
// no JavaScript, pagination is query-param state like the home page.

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
	Nav             string
	ActiveIntensity string
	Counts          store.RiskRadarCounts
	FilteredTotal   int
	Rows            []store.RiskRadarRow
	RangeStart      int
	RangeEnd        int
	PrevURL         string
	NextURL         string
}

// validIntensity constrains the ?intensity= filter to the known values; anything
// else (including empty) means "all".
func validIntensity(raw string) string {
	switch raw {
	case "major", "moderate", "minor":
		return raw
	default:
		return ""
	}
}

func handleRadar(s storer) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		intensity := validIntensity(r.URL.Query().Get("intensity"))
		offset := parseOffset(r.URL.Query().Get("offset"))

		counts, err := s.RiskRadarIntensityCounts(r.Context())
		if err != nil {
			http.Error(w, "query failed", http.StatusInternalServerError)
			return
		}
		rows, total, err := s.RecentDisclosureChanges(r.Context(), intensity, radarPageLimit, offset)
		if err != nil {
			http.Error(w, "query failed", http.StatusInternalServerError)
			return
		}

		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		if err := radarTemplate.ExecuteTemplate(w, "layout.html.tmpl", radarPageData{
			Nav:             "radar",
			ActiveIntensity: intensity,
			Counts:          counts,
			FilteredTotal:   total,
			Rows:            rows,
			RangeStart:      pageRangeStart(offset, len(rows)),
			RangeEnd:        pageRangeEnd(offset, len(rows)),
			PrevURL:         radarPageURL(intensity, offset-radarPageLimit, true),
			NextURL:         radarPageURL(intensity, offset+radarPageLimit, offset+radarPageLimit < total),
		}); err != nil {
			// Headers already written; can't change status.
			_ = err
		}
	}
}

// radarPageURL builds a prev/next (and filter) link for the feed, preserving the
// active intensity filter. Returns "" when disabled or the offset is negative — the
// template renders an empty URL as a disabled control.
func radarPageURL(intensity string, targetOffset int, enabled bool) string {
	if !enabled || targetOffset < 0 {
		return ""
	}
	base := "/radar"
	sep := "?"
	if intensity != "" {
		base += "?intensity=" + intensity
		sep = "&"
	}
	if targetOffset > 0 {
		base += sep + "offset=" + strconv.Itoa(targetOffset)
	}
	return base
}
