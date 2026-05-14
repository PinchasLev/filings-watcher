// Package server wires HTTP handlers over the store. Uses stdlib net/http
// with the Go 1.22+ pattern-routing syntax per ADR 0009 (no third-party
// HTTP framework).
package server

import (
	"context"
	"net/http"

	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

// storer is the narrow interface this package needs from the store. Defined
// here (at the point of use) rather than in the store package, so handlers
// depend on the subset they actually call, and tests can supply a small fake.
// Idiomatic Go: "accept interfaces, return structs."
type storer interface {
	LatestClassifications(ctx context.Context, limit, offset int) ([]store.Classification, int, error)
	FilingByAccession(ctx context.Context, accession string) (*store.FilingDetail, error)
}

// New returns an http.Handler with all routes registered.
func New(s storer) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", handleHealth)
	mux.HandleFunc("GET /filings", handleListFilings(s))
	mux.HandleFunc("GET /filings/{accession}", handleFilingDetail(s))
	return mux
}
