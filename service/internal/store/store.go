// Package store reads filings and classifications from the shared SQLite
// database. The Python orchestrator is the only writer; this package only
// runs SELECT statements.
package store

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"

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

// EventTypeCount pairs an event_type taxonomy value with the number of
// material classifications that currently carry it. Returned by
// EventTypeCounts in descending count order so consumers can render
// the most-common categories first without re-sorting.
type EventTypeCount struct {
	EventType string `json:"event_type"`
	Count     int    `json:"count"`
}

// Company is the identity header for the per-company view: the stable CIK
// plus the current canonical ticker and name. Identity resolves from the
// cik_tickers mirror (SEC's authoritative current mapping) when present;
// for filers absent from that file (funds, foreign filers) it falls back
// to the as-filed name/ticker on the company's most recent filing. CIK is
// the only stable grouping key (ADR 0025); ticker and name are display.
type Company struct {
	CIK         string `json:"cik"`
	Ticker      string `json:"ticker"`
	CompanyName string `json:"company_name"`
}

// Store is the public read-only interface this package provides. The
// concrete implementation is intentionally unexported so consumers cannot
// declare or pass the struct type directly — dependency injection is
// enforced at the type system level.
type Store interface {
	LatestClassifications(ctx context.Context, limit, offset int) ([]Classification, int, error)
	FilingByAccession(ctx context.Context, accession string) (*FilingDetail, error)
	MaterialClassifications(ctx context.Context, eventType string, limit, offset int) ([]Classification, int, error)
	EventTypeCounts(ctx context.Context) ([]EventTypeCount, error)
	LookupCIKByTicker(ctx context.Context, ticker string) (string, error)
	CompanyByCIK(ctx context.Context, cik string, limit, offset int) (*Company, []Classification, int, error)
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

// MaterialClassifications returns latest-per-(accession, item, classifier)
// classifications restricted to is_material = true, ordered by
// filing_date DESC (the user-facing freshness signal) with classified_at
// DESC as the tiebreaker for items sharing a filing_date. An empty
// eventType means "no event-type filter" (return material events
// across all taxonomy categories).
func (s *store) MaterialClassifications(ctx context.Context, eventType string, limit, offset int) ([]Classification, int, error) {
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
		WHERE l.is_material = 1
		  AND (? = '' OR l.event_type = ?)
		ORDER BY f.filing_date DESC, l.classified_at DESC
		LIMIT ? OFFSET ?
	`
	const countQuery = `
		WITH ranked AS (
			SELECT is_material, event_type,
				ROW_NUMBER() OVER (
					PARTITION BY accession_number, COALESCE(item_number, ''), classifier_version
					ORDER BY classified_at DESC
				) AS rn
			FROM classifications
		)
		SELECT COUNT(*) FROM ranked
		WHERE rn = 1 AND is_material = 1 AND (? = '' OR event_type = ?)
	`

	var total int
	if err := s.db.QueryRowContext(ctx, countQuery, eventType, eventType).Scan(&total); err != nil {
		return nil, 0, fmt.Errorf("count material: %w", err)
	}

	rows, err := s.db.QueryContext(ctx, baseQuery, eventType, eventType, limit, offset)
	if err != nil {
		return nil, 0, fmt.Errorf("query material: %w", err)
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

// EventTypeCounts returns the distribution of latest-per-item material
// classifications across event_type taxonomy values, ordered by count
// DESC. Consumed by the home page's filter nav to render chip badges.
// Non-material classifications are excluded so the chip counts match
// what the default home page view shows.
func (s *store) EventTypeCounts(ctx context.Context) ([]EventTypeCount, error) {
	const query = `
		WITH ranked AS (
			SELECT event_type, is_material,
				ROW_NUMBER() OVER (
					PARTITION BY accession_number, COALESCE(item_number, ''), classifier_version
					ORDER BY classified_at DESC
				) AS rn
			FROM classifications
		)
		SELECT event_type, COUNT(*) AS cnt
		FROM ranked
		WHERE rn = 1 AND is_material = 1
		GROUP BY event_type
		ORDER BY cnt DESC, event_type
	`
	rows, err := s.db.QueryContext(ctx, query)
	if err != nil {
		return nil, fmt.Errorf("query event type counts: %w", err)
	}
	defer rows.Close()

	var out []EventTypeCount
	for rows.Next() {
		var e EventTypeCount
		if err := rows.Scan(&e.EventType, &e.Count); err != nil {
			return nil, fmt.Errorf("scan event type count: %w", err)
		}
		out = append(out, e)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate event type counts: %w", err)
	}
	return out, nil
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

// LookupCIKByTicker resolves a user-facing ticker symbol to its CIK via the
// cik_tickers mirror. SEC stores symbols uppercase and unique, so matching
// is exact on an uppercased input — exactly the safe reverse of the stable
// CIK grouping key (ADR 0025), with none of the drift or collision risk a
// company-name search would carry. Returns ErrNotFound when no symbol matches.
//
// A single ticker maps to exactly one CIK in SEC's file; multi-class symbols
// (e.g., GOOG and GOOGL) are distinct rows that both point at one CIK, so
// resolving either lands on the same company view.
func (s *store) LookupCIKByTicker(ctx context.Context, ticker string) (string, error) {
	const query = `SELECT cik FROM cik_tickers WHERE ticker = ? LIMIT 1`
	var cik string
	err := s.db.QueryRowContext(ctx, query, strings.ToUpper(strings.TrimSpace(ticker))).Scan(&cik)
	if errors.Is(err, sql.ErrNoRows) {
		return "", ErrNotFound
	}
	if err != nil {
		return "", fmt.Errorf("lookup cik by ticker: %w", err)
	}
	return cik, nil
}

// CompanyByCIK returns a company's identity header plus a page of its
// material classifications, newest filing first — the per-company view
// reachable via /companies/{cik} and the ticker-search redirect.
//
// Identity resolves from cik_tickers (canonical current name + ticker)
// when present, falling back to the as-filed identity on the company's
// most recent filing for filers absent from SEC's ticker file. A CIK
// unknown to both the mirror and the filings table yields ErrNotFound;
// a CIK known to the mirror but with no classified filings yet returns a
// valid identity with an empty list (total 0) so the caller can render a
// "tracked, nothing classified yet" state distinct from a 404.
//
// The classification page mirrors MaterialClassifications (latest-per-
// item-and-classifier, is_material = 1, filing_date DESC) but scoped to a
// single CIK — same signal-dense framing as the home list.
func (s *store) CompanyByCIK(ctx context.Context, cik string, limit, offset int) (*Company, []Classification, int, error) {
	company, err := s.resolveCompanyIdentity(ctx, cik)
	if err != nil {
		return nil, nil, 0, err
	}

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
		WHERE l.is_material = 1 AND f.cik = ?
		ORDER BY f.filing_date DESC, l.classified_at DESC
		LIMIT ? OFFSET ?
	`
	const countQuery = `
		WITH ranked AS (
			SELECT c.is_material, f.cik,
				ROW_NUMBER() OVER (
					PARTITION BY c.accession_number, COALESCE(c.item_number, ''), c.classifier_version
					ORDER BY c.classified_at DESC
				) AS rn
			FROM classifications c
			JOIN filings f ON f.accession_number = c.accession_number
		)
		SELECT COUNT(*) FROM ranked WHERE rn = 1 AND is_material = 1 AND cik = ?
	`

	var total int
	if err := s.db.QueryRowContext(ctx, countQuery, cik).Scan(&total); err != nil {
		return nil, nil, 0, fmt.Errorf("count company classifications: %w", err)
	}

	rows, err := s.db.QueryContext(ctx, baseQuery, cik, limit, offset)
	if err != nil {
		return nil, nil, 0, fmt.Errorf("query company classifications: %w", err)
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
			return nil, nil, 0, fmt.Errorf("scan company row: %w", err)
		}
		c.IsMaterial = isMaterial != 0
		out = append(out, c)
	}
	if err := rows.Err(); err != nil {
		return nil, nil, 0, fmt.Errorf("iterate company rows: %w", err)
	}
	return company, out, total, nil
}

// resolveCompanyIdentity returns the display identity for a CIK, preferring
// the canonical cik_tickers row and falling back to the as-filed identity on
// the company's most recent filing. Returns ErrNotFound when the CIK appears
// in neither — the signal the company handler turns into a 404.
func (s *store) resolveCompanyIdentity(ctx context.Context, cik string) (*Company, error) {
	c := Company{CIK: cik}
	const canonicalQuery = `SELECT ticker, company_name FROM cik_tickers WHERE cik = ?`
	err := s.db.QueryRowContext(ctx, canonicalQuery, cik).Scan(&c.Ticker, &c.CompanyName)
	if err == nil {
		return &c, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return nil, fmt.Errorf("query company identity: %w", err)
	}

	// Not in SEC's ticker file (fund, foreign filer, …). Fall back to the
	// most recent filing's as-filed name and ticker, if we have any filing.
	const fallbackQuery = `
		SELECT company_name, ticker FROM filings
		 WHERE cik = ? ORDER BY filing_date DESC LIMIT 1
	`
	var ticker sql.NullString
	err = s.db.QueryRowContext(ctx, fallbackQuery, cik).Scan(&c.CompanyName, &ticker)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, fmt.Errorf("query fallback identity: %w", err)
	}
	if ticker.Valid {
		c.Ticker = ticker.String
	}
	return &c, nil
}
