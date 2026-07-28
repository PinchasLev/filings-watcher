// Read-only SQL console at GET /ops/query — the ad-hoc slice-and-dice layer over the
// raw logs, kept under /ops/* so Caddy's public 404 keeps it tailnet-only. The canned
// /ops/traffic panels answer the few questions we know matter; this answers the ones
// we don't, without a BI platform: type a SELECT, get a table. Read-only is enforced
// in the store (SELECT/WITH only, single statement, no write keywords).

package server

import (
	"html/template"
	"net/http"
	"strings"
	"time"
)

var queryTemplate = template.Must(template.New("query.html.tmpl").Funcs(templateFuncs).ParseFS(
	templateFS, "templates/query.html.tmpl",
))

type queryExample struct {
	Label string
	SQL   string
}

// queryExamples seed the console and double as documentation of the log schema.
var queryExamples = []queryExample{
	{"Recent views", "SELECT viewed_at, path, client_kind, referrer_host FROM page_views ORDER BY viewed_at DESC LIMIT 50"},
	{"Human views by day", "SELECT substr(viewed_at,1,10) AS day, COUNT(*) AS views FROM page_views WHERE client_kind='human' GROUP BY day ORDER BY day DESC"},
	{"Unique visitors by day", "SELECT substr(viewed_at,1,10) AS day, COUNT(DISTINCT visitor_day_hash) AS visitors FROM page_views WHERE client_kind='human' AND visitor_day_hash<>'' GROUP BY day ORDER BY day DESC"},
	{"Top referrers", "SELECT referrer_host, COUNT(*) AS n FROM page_views WHERE client_kind='human' AND referrer_host<>'' GROUP BY referrer_host ORDER BY n DESC LIMIT 20"},
	{"By client kind", "SELECT client_kind, COUNT(*) AS n FROM page_views GROUP BY client_kind ORDER BY n DESC"},
}

type queryPageData struct {
	Query      string
	Columns    []string
	Rows       [][]string
	RowCount   int
	Truncated  bool
	Error      string
	Examples   []queryExample
	RenderedAt string
}

func handleQueryConsole(s storer) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		q := strings.TrimSpace(r.URL.Query().Get("sql"))
		data := queryPageData{
			Query:      q,
			Examples:   queryExamples,
			RenderedAt: time.Now().UTC().Format(time.RFC3339),
		}
		if q != "" {
			cols, rows, truncated, err := s.RunReadOnlyQuery(r.Context(), q)
			if err != nil {
				data.Error = err.Error()
			} else {
				data.Columns = cols
				data.Rows = rows
				data.RowCount = len(rows)
				data.Truncated = truncated
			}
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		if err := queryTemplate.ExecuteTemplate(w, "query.html.tmpl", data); err != nil {
			_ = err
		}
	}
}
