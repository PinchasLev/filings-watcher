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
	"html/template"
	"net/http"
	"strconv"

	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

const companyPageLimit = 50

// companyTemplate is parsed once at process start, sharing the base layout
// and the common template funcs with the home and detail pages.
var companyTemplate = template.Must(template.New("layout.html.tmpl").Funcs(templateFuncs).ParseFS(
	templateFS, "templates/layout.html.tmpl", "templates/company.html.tmpl",
))

type companyPageData struct {
	Company     store.Company
	Filings     []store.Classification
	FilingTotal int
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

		company, filings, total, err := s.CompanyByCIK(r.Context(), cik, companyPageLimit, offset)
		if errors.Is(err, store.ErrNotFound) {
			http.NotFound(w, r)
			return
		}
		if err != nil {
			http.Error(w, "query failed", http.StatusInternalServerError)
			return
		}

		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		if err := companyTemplate.ExecuteTemplate(w, "layout.html.tmpl", companyPageData{
			Company:     *company,
			Filings:     filings,
			FilingTotal: total,
			RangeStart:  pageRangeStart(offset, len(filings)),
			RangeEnd:    pageRangeEnd(offset, len(filings)),
			PrevURL:     companyPageURL(cik, offset-companyPageLimit, true),
			NextURL:     companyPageURL(cik, offset+companyPageLimit, offset+companyPageLimit < total),
		}); err != nil {
			// Headers already written; can't change status.
			_ = err
		}
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
