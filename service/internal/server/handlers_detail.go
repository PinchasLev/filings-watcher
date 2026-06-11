// Server-rendered filing detail page. Single URL with the existing JSON
// endpoint at GET /filings/{accession} via content negotiation: browsers
// (Accept: text/html) get the HTML view; programmatic callers
// (Accept: application/json or no Accept header) get the existing JSON
// payload unchanged.
//
// The detail page is the comprehensive single-filing view: ALL
// classifications for the accession (across classifier versions), both
// material and non-material. This is intentionally broader than the
// home page, which filters to is_material=true for browsability.

package server

import (
	"html/template"
	"net/http"
	"strings"

	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

// detailTemplate parses the layout + detail templates at process start.
// The "layout.html.tmpl" name is the entry point ExecuteTemplate uses;
// detail.html.tmpl supplies the "title" and "content" blocks the layout
// expects.
var detailTemplate = template.Must(template.New("layout.html.tmpl").Funcs(templateFuncs).ParseFS(
	templateFS, "templates/layout.html.tmpl", "templates/detail.html.tmpl",
))

// wantsHTML returns true when the request's Accept header explicitly
// names text/html. Browsers do; curl with no Accept header sends */*,
// which yields JSON (the backwards-compatible default). Programmatic
// callers that want JSON either send no Accept header or set
// "application/json" — both fall to JSON here.
func wantsHTML(r *http.Request) bool {
	return strings.Contains(r.Header.Get("Accept"), "text/html")
}

// detailPageData wraps the store struct with the layout's nav state.
// Nav is empty so neither top-bar section is highlighted on a detail
// view — both stay plainly clickable.
type detailPageData struct {
	Nav string
	*store.FilingDetail
}

func renderDetailHTML(w http.ResponseWriter, detail *store.FilingDetail) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	if err := detailTemplate.ExecuteTemplate(w, "layout.html.tmpl", detailPageData{
		Nav:          "",
		FilingDetail: detail,
	}); err != nil {
		// Headers already flushed; can't change status.
		_ = err
	}
}
