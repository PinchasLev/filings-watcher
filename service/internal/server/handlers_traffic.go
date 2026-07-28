// Traffic dashboard at GET /ops/traffic — the engagement view, kept under the /ops/*
// prefix so Caddy's public 404 keeps it tailnet-only (ADR 0024). Separate from the
// health-focused /ops board: this answers "is the product getting traction" (human
// page views, unique visitors, automated/API-appetite traffic, and the most-viewed
// pages and companies) rather than "is the system healthy".

package server

import (
	"html/template"
	"net/http"
	"time"

	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

var trafficTemplate = template.Must(template.New("traffic.html.tmpl").Funcs(templateFuncs).ParseFS(
	templateFS, "templates/traffic.html.tmpl",
))

type trafficPageData struct {
	Traffic    store.PageViewStats
	RenderedAt string
}

func handleTraffic(s storer) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		traffic, err := s.PageViewSummary(r.Context())
		if err != nil {
			http.Error(w, "query failed", http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		if err := trafficTemplate.ExecuteTemplate(w, "traffic.html.tmpl", trafficPageData{
			Traffic:    traffic,
			RenderedAt: time.Now().UTC().Format(time.RFC3339),
		}); err != nil {
			// Headers already written; can't change status.
			_ = err
		}
	}
}
