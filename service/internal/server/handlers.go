package server

import (
	"encoding/json"
	"errors"
	"net/http"
	"strconv"

	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

const (
	defaultLimit = 20
	maxLimit     = 100
)

// listResponse is the JSON envelope around a paginated classification list.
type listResponse struct {
	Items  []store.Classification `json:"items"`
	Total  int                    `json:"total"`
	Limit  int                    `json:"limit"`
	Offset int                    `json:"offset"`
}

func handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func handleListFilings(s storer) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		limit := readPositiveIntQuery(r, "limit", defaultLimit)
		if limit > maxLimit {
			limit = maxLimit
		}
		offset := readPositiveIntQuery(r, "offset", 0)

		items, total, err := s.LatestClassifications(r.Context(), limit, offset)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "query failed")
			return
		}
		writeJSON(w, http.StatusOK, listResponse{
			Items:  items,
			Total:  total,
			Limit:  limit,
			Offset: offset,
		})
	}
}

func handleFilingDetail(s storer) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		accession := r.PathValue("accession")
		detail, err := s.FilingByAccession(r.Context(), accession)
		if errors.Is(err, store.ErrNotFound) {
			if wantsHTML(r) {
				http.NotFound(w, r)
				return
			}
			writeError(w, http.StatusNotFound, "filing not found")
			return
		}
		if err != nil {
			if wantsHTML(r) {
				http.Error(w, "query failed", http.StatusInternalServerError)
				return
			}
			writeError(w, http.StatusInternalServerError, "query failed")
			return
		}
		// Content negotiation: browsers (Accept: text/html) see the
		// rendered detail page; programmatic callers get the existing
		// JSON payload unchanged. Same URL, two formats — no breaking
		// change to the API surface.
		if wantsHTML(r) {
			renderDetailHTML(w, detail)
			return
		}
		writeJSON(w, http.StatusOK, detail)
	}
}

// readPositiveIntQuery parses a non-negative integer from a query parameter,
// returning fallback when the param is missing or invalid.
func readPositiveIntQuery(r *http.Request, key string, fallback int) int {
	raw := r.URL.Query().Get(key)
	if raw == "" {
		return fallback
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n < 0 {
		return fallback
	}
	return n
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(body); err != nil {
		// Body already partially written; can't change headers. Log only.
		// Server-side logging happens via the surrounding chain when wired.
		_ = err
	}
}

func writeError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"error": message})
}
