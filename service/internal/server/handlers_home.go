// Server-rendered public home page. Renders the latest material 8-K
// classifications with a filter-by-event-type nav. All templates are
// embedded into the binary via go:embed; no runtime asset directory.
//
// The page intentionally stays small in scope:
//
//   - One route (GET /).
//   - Server-side rendering with stdlib html/template.
//   - Pico.css via CDN for typography (one <link> in the layout).
//   - No JavaScript. Filter state is a query parameter; clicks navigate.
//   - Material classifications only (the non-material toggle, full search,
//     and the per-filing detail page are tracked as follow-ups).

package server

import (
	"embed"
	"fmt"
	"html/template"
	"net/http"
	"net/url"
	"strconv"
	"strings"

	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

const homePageLimit = 50

//go:embed templates/*.html.tmpl
var templateFS embed.FS

// homeTemplate is parsed once at process start. Template funcs handle
// taxonomy-value pretty-printing, optional-string dereferencing, the
// EDGAR URL construction, and confidence-percentage math so the
// templates stay declarative.
var homeTemplate = template.Must(template.New("layout.html.tmpl").Funcs(template.FuncMap{
	"eventLabel": eventLabel,
	"derefStr":   derefStr,
	"edgarURL":   edgarFilingURL,
	"mul":        func(a, b float64) float64 { return a * b },
}).ParseFS(templateFS, "templates/layout.html.tmpl", "templates/home.html.tmpl"))

type homePageData struct {
	ActiveEventType string
	EventTypeCounts []store.EventTypeCount
	TotalMaterial   int
	FilteredTotal   int
	Filings         []store.Classification
	// Pagination state. RangeStart/RangeEnd are 1-based inclusive bounds
	// of the slice currently shown ("17-32 of 299"). PrevURL/NextURL are
	// empty strings when no further page exists in that direction; the
	// template renders them as disabled controls in that case.
	RangeStart int
	RangeEnd   int
	PrevURL    string
	NextURL    string
}

func handleHome(s storer) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// Only handle the bare path; sub-routes (or asset paths a browser
		// might probe for) are 404s. stdlib net/http's pattern routing on
		// "GET /" matches every unmatched path that begins with /, so the
		// guard here keeps that behavior product-correct.
		if r.URL.Path != "/" {
			http.NotFound(w, r)
			return
		}

		eventType := strings.TrimSpace(r.URL.Query().Get("event"))
		offset := parseOffset(r.URL.Query().Get("offset"))

		counts, err := s.EventTypeCounts(r.Context())
		if err != nil {
			http.Error(w, "query failed", http.StatusInternalServerError)
			return
		}
		total := 0
		for _, c := range counts {
			total += c.Count
		}

		filings, filteredTotal, err := s.MaterialClassifications(r.Context(), eventType, homePageLimit, offset)
		if err != nil {
			http.Error(w, "query failed", http.StatusInternalServerError)
			return
		}

		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		if err := homeTemplate.ExecuteTemplate(w, "layout.html.tmpl", homePageData{
			ActiveEventType: eventType,
			EventTypeCounts: counts,
			TotalMaterial:   total,
			FilteredTotal:   filteredTotal,
			Filings:         filings,
			RangeStart:      pageRangeStart(offset, len(filings)),
			RangeEnd:        pageRangeEnd(offset, len(filings)),
			PrevURL:         pageURL(eventType, offset-homePageLimit, true),
			NextURL:         pageURL(eventType, offset+homePageLimit, offset+homePageLimit < filteredTotal),
		}); err != nil {
			// Headers already written; can't change status. Surface the
			// error in the connection's log if the framing layer wires it.
			_ = err
		}
	}
}

// parseOffset reads ?offset= as a non-negative integer, returning 0 for
// missing, malformed, or negative values. Bounds-checking against the
// filtered total happens implicitly: an offset past the end yields an
// empty filings list and disabled "Older" pagination.
func parseOffset(raw string) int {
	if raw == "" {
		return 0
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n < 0 {
		return 0
	}
	return n
}

// pageRangeStart returns the 1-based start index of the current page
// slice ("17-32 of 299" -> 17). Returns 0 when the slice is empty so
// the template can choose to show "0-0 of N" or hide the range.
func pageRangeStart(offset, pageLen int) int {
	if pageLen == 0 {
		return 0
	}
	return offset + 1
}

// pageRangeEnd returns the 1-based end index of the current page slice
// ("17-32 of 299" -> 32). Inclusive upper bound. Returns 0 when empty.
func pageRangeEnd(offset, pageLen int) int {
	if pageLen == 0 {
		return 0
	}
	return offset + pageLen
}

// pageURL constructs the URL for prev/next pagination links. Returns
// the empty string when `enabled` is false or the target offset is
// negative — the template treats an empty URL as "render as disabled."
// Preserves the active event-type filter so pagination clicks stay
// within the current category.
func pageURL(eventType string, targetOffset int, enabled bool) string {
	if !enabled || targetOffset < 0 {
		return ""
	}
	params := url.Values{}
	if eventType != "" {
		params.Set("event", eventType)
	}
	if targetOffset > 0 {
		params.Set("offset", fmt.Sprintf("%d", targetOffset))
	}
	if len(params) == 0 {
		return "/"
	}
	return "/?" + params.Encode()
}

// eventLabel turns taxonomy snake_case values (e.g., "ma_activity") into
// human-readable labels for chips and badges ("M&A activity"). The map
// keeps the common cases readable; unmapped values fall back to a
// title-cased replacement of underscores with spaces.
func eventLabel(eventType string) string {
	switch eventType {
	case "ma_activity":
		return "M&A activity"
	case "earnings_release":
		return "Earnings release"
	case "exec_appointment":
		return "Exec appointment"
	case "exec_departure":
		return "Exec departure"
	case "dilutive_issuance":
		return "Dilutive issuance"
	case "delisting_risk":
		return "Delisting risk"
	case "shareholder_vote_results":
		return "Shareholder vote"
	case "other_material":
		return "Other material"
	}
	return strings.Title(strings.ReplaceAll(eventType, "_", " ")) //nolint:staticcheck // strings.Title is fine for ASCII taxonomy values.
}

// derefStr safely renders a *string in a template; nil becomes empty.
func derefStr(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

// edgarFilingURL builds the canonical EDGAR filing-index URL from an
// accession number. The accession format is "<10-digit-CIK>-<2-digit-year>-
// <6-digit-sequence>"; EDGAR's archives URL uses the CIK with leading
// zeros stripped and the accession without dashes.
func edgarFilingURL(accession string) string {
	parts := strings.SplitN(accession, "-", 3)
	if len(parts) != 3 {
		return "https://www.sec.gov/"
	}
	cikStripped := strings.TrimLeft(parts[0], "0")
	if cikStripped == "" {
		cikStripped = "0"
	}
	noDashes := strings.ReplaceAll(accession, "-", "")
	return "https://www.sec.gov/Archives/edgar/data/" + cikStripped + "/" + noDashes + "/" + accession + "-index.htm"
}
