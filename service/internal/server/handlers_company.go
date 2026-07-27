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
	"strings"

	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

const companyPageLimit = 50

// insiderTradesLimit caps the recent-insider-transactions table on the company page.
const insiderTradesLimit = 20

// disclosureChangesLimit caps the material risk-factor changes surfaced on the page.
const disclosureChangesLimit = 40

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
	// Disclosure change-detection (ADR 0042): material year-over-year risk-factor
	// changes, grouped by filing. Supplementary — a query error renders the empty
	// state rather than failing the page.
	DisclosureChanges []store.DisclosureChangeGroup
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
		changes, _ := s.CompanyDisclosureChanges(r.Context(), canonicalCIK, disclosureChangesLimit)

		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		if err := companyTemplate.ExecuteTemplate(w, "layout.html.tmpl", companyPageData{
			Company:           *company,
			Events:            events,
			FilingTotal:       total,
			Pulse30:           pulse30,
			Pulse90:           pulse90,
			InsiderTrades:     trades,
			DisclosureChanges: changes,
			RangeStart:        pageRangeStart(offset, len(events)),
			RangeEnd:          pageRangeEnd(offset, len(events)),
			PrevURL:           companyPageURL(cik, offset-companyPageLimit, true),
			NextURL:           companyPageURL(cik, offset+companyPageLimit, offset+companyPageLimit < total),
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

// disclosureChangeLabel maps a diff change type to a reader-facing badge word.
func disclosureChangeLabel(changeType string) string {
	switch changeType {
	case "added":
		return "New"
	case "changed":
		return "Revised"
	case "dropped":
		return "Removed"
	default:
		return changeType
	}
}

// disclosureDirectionLabel maps a per-change risk direction to a badge word. This
// is the *meaning* of a change (did the risk get worse or ease), which leads the
// display over the mechanical added/changed/dropped.
func disclosureDirectionLabel(direction string) string {
	switch direction {
	case "worse":
		return "Worse"
	case "eased":
		return "Eased"
	case "neutral":
		return "Neutral"
	default:
		return direction
	}
}

// riskShiftLabel composes the filing-level headline from its two axes — e.g.
// ("major", "worsening") -> "Major worsening". These are separate judgments
// (which way vs. how much), so the label carries both (ADR 0043).
func riskShiftLabel(intensity, direction string) string {
	switch intensity {
	case "major", "moderate", "minor":
		return strings.Title(intensity) + " " + direction //nolint:staticcheck // ASCII enum values.
	default:
		return strings.Title(direction) //nolint:staticcheck // ASCII enum values.
	}
}

// disclosureCategoryLabel maps a governed risk-theme value to a human label. The
// vocabulary is fixed (ADR 0043); an unmapped value falls back to a title-cased
// de-underscored form so nothing is silently blank.
func disclosureCategoryLabel(category string) string {
	switch category {
	case "liquidity_going_concern":
		return "Liquidity & going concern"
	case "debt_capital_structure":
		return "Debt & capital structure"
	case "impairment_asset_value":
		return "Impairment & asset value"
	case "restructuring_workforce":
		return "Restructuring & workforce"
	case "litigation_legal":
		return "Litigation & legal"
	case "regulatory_compliance":
		return "Regulatory & compliance"
	case "ma_strategic":
		return "M&A & strategic"
	case "operations_supply_chain":
		return "Operations & supply chain"
	case "market_competition":
		return "Market & competition"
	case "technology_cybersecurity":
		return "Technology & cybersecurity"
	case "governance_controls":
		return "Governance & controls"
	case "macro_geopolitical":
		return "Macro & geopolitical"
	case "environmental_climate":
		return "Environmental & climate"
	case "other":
		return "Other"
	default:
		return strings.Title(strings.ReplaceAll(category, "_", " ")) //nolint:staticcheck // ASCII values.
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
