// Package store reads filings and classifications from the shared SQLite
// database. The Python orchestrator is the only writer; this package only
// runs SELECT statements.
package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"

	_ "modernc.org/sqlite"
)

// Filing is the metadata view of one filing exposed by the API.
type Filing struct {
	AccessionNumber    string  `json:"accession_number"`
	CIK                string  `json:"cik"`
	Ticker             *string `json:"ticker"`
	CompanyName        string  `json:"company_name"`
	Form               string  `json:"form"`
	FilingDate         string  `json:"filing_date"`
	ReportDate         *string `json:"report_date"`
	PrimaryDocument    string  `json:"primary_document"`
	PrimaryDocumentURL string  `json:"primary_document_url"`
}

// Classification is one row from the classifications table, denormalized
// with the company_name and ticker from the filings table for list views.
type Classification struct {
	ID                int64   `json:"id"`
	AccessionNumber   string  `json:"accession_number"`
	ItemNumber        *string `json:"item_number"`
	ItemTitle         *string `json:"item_title"`
	EventType         string  `json:"event_type"`
	EventDomain       string  `json:"event_domain"`
	IsMaterial        bool    `json:"is_material"`
	Confidence        float64 `json:"confidence"`
	Reasoning         string  `json:"reasoning"`
	ClassifierVersion string  `json:"classifier_version"`
	TaxonomyVersion   string  `json:"taxonomy_version"`
	ClassifiedAt      string  `json:"classified_at"`
	// Denormalized for list response.
	CompanyName string  `json:"company_name"`
	Ticker      *string `json:"ticker"`
	FilingDate  string  `json:"filing_date"`
}

// FilingDetail bundles a filing with all its classifications across versions.
type FilingDetail struct {
	Filing          Filing           `json:"filing"`
	Classifications []Classification `json:"classifications"`
}

// ErrNotFound is returned when a query targets a specific record that doesn't exist.
var ErrNotFound = errors.New("not found")

// Store is the public read-only interface this package provides. The
// concrete implementation is intentionally unexported so consumers cannot
// declare or pass the struct type directly — dependency injection is
// enforced at the type system level.
type Store interface {
	LatestClassifications(ctx context.Context, limit, offset int) ([]Classification, int, error)
	FilingByAccession(ctx context.Context, accession string) (*FilingDetail, error)
	Close() error
}

// store is the SQLite-backed implementation. Unexported by design.
type store struct {
	db *sql.DB
}

// Open opens a read-only handle to the SQLite database at dbPath.
//
// The orchestrator (Python) is responsible for creating the file and
// applying migrations; this function does neither. WAL is set on the file
// by the writer side; readers inherit it automatically and do not need to
// re-set it.
func Open(dbPath string) (Store, error) {
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	if err := db.Ping(); err != nil {
		_ = db.Close()
		return nil, fmt.Errorf("ping sqlite: %w", err)
	}
	return &store{db: db}, nil
}

// Close releases the underlying handle.
func (s *store) Close() error {
	return s.db.Close()
}

// LatestClassifications returns the most recent classification per (filing
// item, classifier_version), most-recently-classified first.
//
// `limit` caps the page size; `offset` advances through the list. Returns
// the page of items and the total count of latest-per-item rows for
// pagination headers.
func (s *store) LatestClassifications(ctx context.Context, limit, offset int) ([]Classification, int, error) {
	const baseQuery = `
		WITH ranked AS (
			SELECT
				c.id, c.accession_number, c.item_number, c.item_title,
				c.event_type, c.event_domain, c.is_material, c.confidence,
				c.reasoning, c.classifier_version, c.taxonomy_version,
				c.classified_at,
				ROW_NUMBER() OVER (
					PARTITION BY c.accession_number, COALESCE(c.item_number, ''), c.classifier_version
					ORDER BY c.classified_at DESC
				) AS rn
			FROM classifications c
		),
		latest AS (
			SELECT * FROM ranked WHERE rn = 1
		)
		SELECT
			l.id, l.accession_number, l.item_number, l.item_title,
			l.event_type, l.event_domain, l.is_material, l.confidence,
			l.reasoning, l.classifier_version, l.taxonomy_version, l.classified_at,
			f.company_name, f.ticker, f.filing_date
		FROM latest l
		JOIN filings f ON f.accession_number = l.accession_number
		ORDER BY l.classified_at DESC
		LIMIT ? OFFSET ?
	`
	const countQuery = `
		WITH ranked AS (
			SELECT 1 AS dummy,
				ROW_NUMBER() OVER (
					PARTITION BY accession_number, COALESCE(item_number, ''), classifier_version
					ORDER BY classified_at DESC
				) AS rn
			FROM classifications
		)
		SELECT COUNT(*) FROM ranked WHERE rn = 1
	`

	var total int
	if err := s.db.QueryRowContext(ctx, countQuery).Scan(&total); err != nil {
		return nil, 0, fmt.Errorf("count latest: %w", err)
	}

	rows, err := s.db.QueryContext(ctx, baseQuery, limit, offset)
	if err != nil {
		return nil, 0, fmt.Errorf("query latest: %w", err)
	}
	defer rows.Close()

	out := make([]Classification, 0, limit)
	for rows.Next() {
		var c Classification
		var isMaterial int64
		if err := rows.Scan(
			&c.ID, &c.AccessionNumber, &c.ItemNumber, &c.ItemTitle,
			&c.EventType, &c.EventDomain, &isMaterial, &c.Confidence,
			&c.Reasoning, &c.ClassifierVersion, &c.TaxonomyVersion, &c.ClassifiedAt,
			&c.CompanyName, &c.Ticker, &c.FilingDate,
		); err != nil {
			return nil, 0, fmt.Errorf("scan row: %w", err)
		}
		c.IsMaterial = isMaterial != 0
		out = append(out, c)
	}
	if err := rows.Err(); err != nil {
		return nil, 0, fmt.Errorf("iterate rows: %w", err)
	}
	return out, total, nil
}

// FilingByAccession returns one filing plus every classification ever
// recorded for it (across all classifier versions), newest first.
func (s *store) FilingByAccession(ctx context.Context, accession string) (*FilingDetail, error) {
	const filingQuery = `
		SELECT accession_number, cik, ticker, company_name, form,
		       filing_date, report_date, primary_document, primary_document_url
		  FROM filings
		 WHERE accession_number = ?
	`
	var f Filing
	err := s.db.QueryRowContext(ctx, filingQuery, accession).Scan(
		&f.AccessionNumber, &f.CIK, &f.Ticker, &f.CompanyName, &f.Form,
		&f.FilingDate, &f.ReportDate, &f.PrimaryDocument, &f.PrimaryDocumentURL,
	)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, fmt.Errorf("query filing: %w", err)
	}

	const classQuery = `
		SELECT c.id, c.accession_number, c.item_number, c.item_title,
		       c.event_type, c.event_domain, c.is_material, c.confidence,
		       c.reasoning, c.classifier_version, c.taxonomy_version,
		       c.classified_at,
		       f.company_name, f.ticker, f.filing_date
		  FROM classifications c
		  JOIN filings f ON f.accession_number = c.accession_number
		 WHERE c.accession_number = ?
		 ORDER BY c.classified_at DESC
	`
	rows, err := s.db.QueryContext(ctx, classQuery, accession)
	if err != nil {
		return nil, fmt.Errorf("query classifications: %w", err)
	}
	defer rows.Close()

	var classifications []Classification
	for rows.Next() {
		var c Classification
		var isMaterial int64
		if err := rows.Scan(
			&c.ID, &c.AccessionNumber, &c.ItemNumber, &c.ItemTitle,
			&c.EventType, &c.EventDomain, &isMaterial, &c.Confidence,
			&c.Reasoning, &c.ClassifierVersion, &c.TaxonomyVersion, &c.ClassifiedAt,
			&c.CompanyName, &c.Ticker, &c.FilingDate,
		); err != nil {
			return nil, fmt.Errorf("scan classification: %w", err)
		}
		c.IsMaterial = isMaterial != 0
		classifications = append(classifications, c)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate classifications: %w", err)
	}

	return &FilingDetail{Filing: f, Classifications: classifications}, nil
}
