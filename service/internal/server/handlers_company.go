// Server-rendered per-company view at GET /companies/{cik}. Lists a single
// company's material classifications, newest filing first — the same
// signal-dense framing as the home page, scoped to one stable CIK.
//
// CIK is the only grouping key (ADR 0025): tickers drift and company names
// both drift and collide, so neither can safely group an entity's filings.
// The ticker-search box on the home page resolves a symbol to its CIK and
// redirects here; this URL is the canonical, shareable company page.

package server

import (
	"errors"
	"fmt"
	"html/template"
	"math"
	"net/http"
	"strconv"

	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

const companyPageLimit = 50

// insiderTradesLimit caps the recent-insider-transactions table on the company page.
const insiderTradesLimit = 20

// companyTemplate is parsed once at process start, sharing the base layout
// and the common template funcs with the home and detail pages.
var companyTemplate = template.Must(template.New("layout.html.tmpl").Funcs(templateFuncs).ParseFS(
	templateFS, "templates/layout.html.tmpl", "templates/company.html.tmpl",
))

type companyPageData struct {
	// Nav is empty so neither top-bar section ("Latest" / "Live") is
	// highlighted on the company view — both remain plainly clickable.
	Nav         string
	Company     store.Company
	Events      []store.Event
	FilingTotal int
	// Insider (Form 4) surfacing. Supplementary: a query error here leaves
	// these zero/empty and the section renders its empty state, rather than
	// failing the whole company page.
	Pulse30       store.InsiderPulse
	Pulse90       store.InsiderPulse
	InsiderTrades []store.InsiderTrade
	// Pagination state, identical in meaning to the home page's.
	RangeStart int
	RangeEnd   int
	PrevURL    string
	NextURL    string
}

func handleCompany(s storer) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		cik := r.PathValue("cik")
		offset := parseOffset(r.URL.Query().Get("offset"))

		company, events, total, err := s.CompanyEvents(r.Context(), cik, companyPageLimit, offset)
		if errors.Is(err, store.ErrNotFound) {
			http.NotFound(w, r)
			return
		}
		if err != nil {
			http.Error(w, "query failed", http.StatusInternalServerError)
			return
		}

		// Insider surfacing is supplementary — ignore errors and render the
		// canonical cik (identity-resolved) rather than the requested one.
		canonicalCIK := company.CIK
		pulse30, _ := s.CompanyInsiderPulse(r.Context(), canonicalCIK, 30)
		pulse90, _ := s.CompanyInsiderPulse(r.Context(), canonicalCIK, 90)
		trades, _ := s.CompanyInsiderTrades(r.Context(), canonicalCIK, insiderTradesLimit)

		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		if err := companyTemplate.ExecuteTemplate(w, "layout.html.tmpl", companyPageData{
			Company:       *company,
			Events:        events,
			FilingTotal:   total,
			Pulse30:       pulse30,
			Pulse90:       pulse90,
			InsiderTrades: trades,
			RangeStart:    pageRangeStart(offset, len(events)),
			RangeEnd:      pageRangeEnd(offset, len(events)),
			PrevURL:       companyPageURL(cik, offset-companyPageLimit, true),
			NextURL:       companyPageURL(cik, offset+companyPageLimit, offset+companyPageLimit < total),
		}); err != nil {
			// Headers already written; can't change status.
			_ = err
		}
	}
}

// usdCompact renders a dollar amount as a short, human string ($2.1M, $340K).
func usdCompact(v float64) string {
	a := math.Abs(v)
	var s string
	switch {
	case a >= 1e9:
		s = fmt.Sprintf("$%.1fB", a/1e9)
	case a >= 1e6:
		s = fmt.Sprintf("$%.1fM", a/1e6)
	case a >= 1e3:
		s = fmt.Sprintf("$%.0fK", a/1e3)
	default:
		s = fmt.Sprintf("$%.0f", a)
	}
	if v < 0 {
		return "-" + s
	}
	return s
}

// usdCompactPtr renders a nullable dollar amount, showing an em dash for nil
// (e.g. a grant with no reported price).
func usdCompactPtr(v *float64) string {
	if v == nil {
		return "—"
	}
	return usdCompact(*v)
}

// insiderTxnLabel maps a Form 4 transaction code to a human label. Unknown
// codes fall back to the raw code so nothing is silently hidden.
func insiderTxnLabel(code string) string {
	switch code {
	case "P":
		return "Open-market buy"
	case "S":
		return "Open-market sell"
	case "A":
		return "Grant/award"
	case "M":
		return "Option exercise"
	case "F":
		return "Tax withholding"
	case "G":
		return "Gift"
	case "":
		return "—"
	default:
		return code
	}
}

// companyPageURL builds the prev/next pagination link for the company view.
// Returns the empty string when disabled or the target offset is negative —
// the template renders an empty URL as a disabled control.
func companyPageURL(cik string, targetOffset int, enabled bool) string {
	if !enabled || targetOffset < 0 {
		return ""
	}
	base := "/companies/" + cik
	if targetOffset == 0 {
		return base
	}
	return base + "?offset=" + strconv.Itoa(targetOffset)
}
