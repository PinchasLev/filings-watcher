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
	"time"

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

// FilingDetail bundles a filing with its latest-run events (each nested with
// the Items it collated) and, for JSON back-compat, the flat list of all its
// classifications across versions. The HTML detail page renders Events; the
// JSON payload carries both. Events is populated by the handler via
// EventsByAccession — FilingByAccession leaves it nil.
type FilingDetail struct {
	Filing          Filing           `json:"filing"`
	Events          []EventWithItems `json:"events"`
	Classifications []Classification `json:"classifications"`
}

// Event is one filing-level event from the events layer (ADR 0027/0028),
// denormalized with company_name, ticker, and filing_date for list views.
// It is the reduce stage's output: a deduplicated event collating one or more
// per-Item classifications. AnchorItemNumber is the primary substantive Item
// the event centers on (nil for a whole-filing event); Summary is the
// consolidated description — the event analog of a classification's Reasoning.
type Event struct {
	ID               int64   `json:"id"`
	RunID            int64   `json:"run_id"`
	AccessionNumber  string  `json:"accession_number"`
	AnchorItemNumber *string `json:"anchor_item_number"`
	EventType        string  `json:"event_type"`
	EventDomain      string  `json:"event_domain"`
	IsMaterial       bool    `json:"is_material"`
	Confidence       float64 `json:"confidence"`
	Summary          string  `json:"summary"`
	// Denormalized from filings for list responses.
	CompanyName string  `json:"company_name"`
	Ticker      *string `json:"ticker"`
	FilingDate  string  `json:"filing_date"`
	// Form is the SEC form (e.g. "8-K", "6-K"), denormalized from filings so list
	// views can badge each card by form.
	Form string `json:"form"`
	// SubmittedAt is the precise EDGAR-side filing timestamp (ISO 8601
	// with offset, e.g. "2026-06-05T09:05:09-04:00"). Populated for
	// atom-feed-ingested filings; NULL for daily-index-ingested rows
	// because the master.idx file is date-only. The live page sorts
	// on this field.
	SubmittedAt *string `json:"submitted_at"`
}

// EventWithItems is an event plus the per-Item classifications it collated,
// resolved through the event_classifications join. It backs the detail page's
// drill-down: each event expands to the raw Items it consolidated.
type EventWithItems struct {
	Event
	Items []Classification `json:"items"`
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
	// Events layer (ADR 0027/0028). Each filing's current view is the wholesale
	// output of its single greatest run_id — never a per-anchor maximum.
	MaterialEvents(ctx context.Context, eventType string, limit, offset int) ([]Event, int, error)
	CompanyEvents(ctx context.Context, cik string, limit, offset int) (*Company, []Event, int, error)
	LiveEvents(ctx context.Context, since time.Time, limit, offset int) ([]Event, int, error)
	// ListLiveEventsSince returns the latest-run material atom-ingested
	// events whose submitted_at is strictly after `since`, in
	// descending order, capped at `limit`. Backs the live tape's
	// auto-prepend AJAX path: same filters as LiveEvents but anchored
	// to a moving baseline instead of a rolling window.
	ListLiveEventsSince(ctx context.Context, since time.Time, limit int) ([]Event, error)
	MaterialEventTypeCounts(ctx context.Context) ([]EventTypeCount, error)
	EventsByAccession(ctx context.Context, accession string) ([]EventWithItems, error)
	// Operator dashboard reads. Aggregations against the existing tables;
	// no new schema. Surface the ingest cost trajectory and freshness so
	// stalled exports or runaway spend are visible without journal-spelunking.
	//
	// Windows are deliberately rolling, not calendar-aligned: "today" is a
	// full day at 11:59pm UTC and near-zero at 12:01am, which doesn't match
	// how an operator thinks about recent usage. The orchestrator's safety
	// cap stays calendar-day in the gate logic (it needs a hard reset
	// point); the dashboard is a separate question.
	TrailingHoursSpend(ctx context.Context, hours int) (SpendSnapshot, error)
	HourlySpendBuckets(ctx context.Context, hours int) ([]HourlyBucket, error)
	DailySpendBuckets(ctx context.Context, days int) ([]DailyBucket, error)
	// SpendDataStartDate is the UTC date of the earliest recorded llm_call
	// row ("YYYY-MM-DD"), or "" when the table is empty. The dashboard uses
	// it to caveat the 30-day chart when our per-call instrumentation
	// started inside the window — without that note, days predating the
	// instrumentation look like zero-spend days when they're actually
	// no-data days.
	SpendDataStartDate(ctx context.Context) (string, error)
	AtomSnapshotFreshness(ctx context.Context) (*string, error)
	Close() error
}

// SpendSnapshot summarizes Anthropic spend over a window. The window
// itself is set by the caller; this struct is window-agnostic.
type SpendSnapshot struct {
	TotalUSD  float64 `json:"total_usd"`
	CallCount int     `json:"call_count"`
}

// HourlyBucket is one bar of the rolling-24h shape chart. HourStart is the
// UTC hour boundary the bucket covers (e.g., "2026-06-11T08:00:00Z" covers
// 08:00:00 through 08:59:59.999...). Buckets are zero-padded by the store
// so empty hours still appear — the chart's x-axis stays uniform.
type HourlyBucket struct {
	HourStart string  `json:"hour_start"`
	TotalUSD  float64 `json:"total_usd"`
}

// DailyBucket is one bar of the rolling-30-day shape chart. DayStart is
// the UTC midnight boundary the bucket covers ("2026-06-11T00:00:00Z"
// covers 2026-06-11 00:00:00 UTC through 23:59:59.999... UTC). Zero-padded
// like HourlyBucket so the chart's x-axis stays uniform across the window.
type DailyBucket struct {
	DayStart string  `json:"day_start"`
	TotalUSD float64 `json:"total_usd"`
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

// latestRunEventsCTE is the shared "current view" predicate for the events
// layer: each filing's events come wholesale from its single greatest run_id,
// never a per-anchor maximum (ADR 0028). Joining `events` to `latest_run` on
// both accession AND run_id drops any event an older, larger run emitted that
// the latest run did not — the orphan guard.
const latestRunEventsCTE = `
	WITH latest_run AS (
		SELECT accession_number, MAX(run_id) AS run_id
		FROM events
		GROUP BY accession_number
	)
`

// scanEvents reads Event rows in the column order the list queries below
// project. Shared by MaterialEvents, CompanyEvents, and LiveEvents.
func scanEvents(rows *sql.Rows) ([]Event, error) {
	var out []Event
	for rows.Next() {
		var e Event
		var isMaterial int64
		if err := rows.Scan(
			&e.ID, &e.RunID, &e.AccessionNumber, &e.AnchorItemNumber,
			&e.EventType, &e.EventDomain, &isMaterial, &e.Confidence, &e.Summary,
			&e.CompanyName, &e.Ticker, &e.FilingDate, &e.SubmittedAt, &e.Form,
		); err != nil {
			return nil, fmt.Errorf("scan event: %w", err)
		}
		e.IsMaterial = isMaterial != 0
		out = append(out, e)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate events: %w", err)
	}
	return out, nil
}

// MaterialEvents returns the latest-run material events across all filings,
// newest filing first — the events-layer analog of MaterialClassifications and
// the read backing the home list. An empty eventType means "no event-type
// filter". Returns the page plus the total material-event count for pagination.
func (s *store) MaterialEvents(ctx context.Context, eventType string, limit, offset int) ([]Event, int, error) {
	const baseQuery = latestRunEventsCTE + `
		SELECT
			e.id, e.run_id, e.accession_number, e.anchor_item_number,
			e.event_type, e.event_domain, e.is_material, e.confidence, e.summary,
			f.company_name, f.ticker, f.filing_date, f.submitted_at, f.form
		FROM events e
		JOIN latest_run lr
			ON lr.accession_number = e.accession_number AND lr.run_id = e.run_id
		JOIN filings f ON f.accession_number = e.accession_number
		WHERE e.is_material = 1
		  AND (? = '' OR e.event_type = ?)
		ORDER BY f.filing_date DESC, e.accession_number, e.id
		LIMIT ? OFFSET ?
	`
	const countQuery = latestRunEventsCTE + `
		SELECT COUNT(*)
		FROM events e
		JOIN latest_run lr
			ON lr.accession_number = e.accession_number AND lr.run_id = e.run_id
		WHERE e.is_material = 1 AND (? = '' OR e.event_type = ?)
	`

	var total int
	if err := s.db.QueryRowContext(ctx, countQuery, eventType, eventType).Scan(&total); err != nil {
		return nil, 0, fmt.Errorf("count material events: %w", err)
	}

	rows, err := s.db.QueryContext(ctx, baseQuery, eventType, eventType, limit, offset)
	if err != nil {
		return nil, 0, fmt.Errorf("query material events: %w", err)
	}
	defer rows.Close()

	out, err := scanEvents(rows)
	if err != nil {
		return nil, 0, err
	}
	return out, total, nil
}

// CompanyEvents returns a company's latest-run material events, newest filing
// first — the per-company view's events-layer read. Identity resolution
// (cik_tickers, falling back to as-filed) is shared with CompanyByCIK; an
// unknown CIK yields ErrNotFound.
func (s *store) CompanyEvents(ctx context.Context, cik string, limit, offset int) (*Company, []Event, int, error) {
	company, err := s.resolveCompanyIdentity(ctx, cik)
	if err != nil {
		return nil, nil, 0, err
	}

	const baseQuery = latestRunEventsCTE + `
		SELECT
			e.id, e.run_id, e.accession_number, e.anchor_item_number,
			e.event_type, e.event_domain, e.is_material, e.confidence, e.summary,
			f.company_name, f.ticker, f.filing_date, f.submitted_at, f.form
		FROM events e
		JOIN latest_run lr
			ON lr.accession_number = e.accession_number AND lr.run_id = e.run_id
		JOIN filings f ON f.accession_number = e.accession_number
		WHERE e.is_material = 1 AND f.cik = ?
		ORDER BY f.filing_date DESC, e.accession_number, e.id
		LIMIT ? OFFSET ?
	`
	const countQuery = latestRunEventsCTE + `
		SELECT COUNT(*)
		FROM events e
		JOIN latest_run lr
			ON lr.accession_number = e.accession_number AND lr.run_id = e.run_id
		JOIN filings f ON f.accession_number = e.accession_number
		WHERE e.is_material = 1 AND f.cik = ?
	`

	var total int
	if err := s.db.QueryRowContext(ctx, countQuery, cik).Scan(&total); err != nil {
		return nil, nil, 0, fmt.Errorf("count company events: %w", err)
	}

	rows, err := s.db.QueryContext(ctx, baseQuery, cik, limit, offset)
	if err != nil {
		return nil, nil, 0, fmt.Errorf("query company events: %w", err)
	}
	defer rows.Close()

	out, err := scanEvents(rows)
	if err != nil {
		return nil, nil, 0, err
	}
	return company, out, total, nil
}

// LiveEvents returns the latest-run material events whose filings were
// recorded with a precise EDGAR-side submission timestamp, ordered by that
// timestamp DESC and clipped to a rolling window. This is the read backing
// the /live tape: a near-real-time view of what just filed, sorted by event
// time rather than filing_date.
//
// Implicitly atom-feed-only: only the atom ingest path populates
// `submitted_at`, so daily-index-only filings — which lack sub-day
// timestamps — are excluded from the window by the
// `submitted_at IS NOT NULL` predicate. This is the right scope for a
// live tape: filings reconciled overnight by the backstop don't belong
// on a "what's happening right now" view.
//
// `since` is compared as a UTC instant via SQLite's datetime() so that
// offset differences (EDT vs EST) don't produce off-by-an-hour misorderings
// at the DST boundary. At v0 corpus size the function-on-column cost is
// negligible; an index on submitted_at is a future optimization.
// ListLiveEventsSince returns material atom-ingested events strictly
// newer than `since`, descending, capped at `limit`. Anchored on a
// moving baseline (the newest event the viewer has already seen) so
// the live tape can fetch only what hasn't been shown yet.
func (s *store) ListLiveEventsSince(ctx context.Context, since time.Time, limit int) ([]Event, error) {
	sinceUTC := since.UTC().Format(time.RFC3339Nano)
	const q = latestRunEventsCTE + `
		SELECT
			e.id, e.run_id, e.accession_number, e.anchor_item_number,
			e.event_type, e.event_domain, e.is_material, e.confidence, e.summary,
			f.company_name, f.ticker, f.filing_date, f.submitted_at, f.form
		FROM events e
		JOIN latest_run lr
			ON lr.accession_number = e.accession_number AND lr.run_id = e.run_id
		JOIN filings f ON f.accession_number = e.accession_number
		WHERE e.is_material = 1
		  AND f.submitted_at IS NOT NULL
		  AND datetime(f.submitted_at) > datetime(?)
		ORDER BY datetime(f.submitted_at) DESC, e.accession_number, e.id
		LIMIT ?
	`
	rows, err := s.db.QueryContext(ctx, q, sinceUTC, limit)
	if err != nil {
		return nil, fmt.Errorf("list live events since: %w", err)
	}
	defer rows.Close()

	return scanEvents(rows)
}

func (s *store) LiveEvents(ctx context.Context, since time.Time, limit, offset int) ([]Event, int, error) {
	sinceUTC := since.UTC().Format(time.RFC3339Nano)
	const baseQuery = latestRunEventsCTE + `
		SELECT
			e.id, e.run_id, e.accession_number, e.anchor_item_number,
			e.event_type, e.event_domain, e.is_material, e.confidence, e.summary,
			f.company_name, f.ticker, f.filing_date, f.submitted_at, f.form
		FROM events e
		JOIN latest_run lr
			ON lr.accession_number = e.accession_number AND lr.run_id = e.run_id
		JOIN filings f ON f.accession_number = e.accession_number
		WHERE e.is_material = 1
		  AND f.submitted_at IS NOT NULL
		  AND datetime(f.submitted_at) >= datetime(?)
		ORDER BY datetime(f.submitted_at) DESC, e.accession_number, e.id
		LIMIT ? OFFSET ?
	`
	const countQuery = latestRunEventsCTE + `
		SELECT COUNT(*)
		FROM events e
		JOIN latest_run lr
			ON lr.accession_number = e.accession_number AND lr.run_id = e.run_id
		JOIN filings f ON f.accession_number = e.accession_number
		WHERE e.is_material = 1
		  AND f.submitted_at IS NOT NULL
		  AND datetime(f.submitted_at) >= datetime(?)
	`

	var total int
	if err := s.db.QueryRowContext(ctx, countQuery, sinceUTC).Scan(&total); err != nil {
		return nil, 0, fmt.Errorf("count live events: %w", err)
	}

	rows, err := s.db.QueryContext(ctx, baseQuery, sinceUTC, limit, offset)
	if err != nil {
		return nil, 0, fmt.Errorf("query live events: %w", err)
	}
	defer rows.Close()

	out, err := scanEvents(rows)
	if err != nil {
		return nil, 0, err
	}
	return out, total, nil
}

// MaterialEventTypeCounts returns the distribution of latest-run material
// events across event_type values, count DESC — the events-layer analog of
// EventTypeCounts, so the home page's filter chips match an events-based list.
func (s *store) MaterialEventTypeCounts(ctx context.Context) ([]EventTypeCount, error) {
	const query = latestRunEventsCTE + `
		SELECT e.event_type, COUNT(*) AS cnt
		FROM events e
		JOIN latest_run lr
			ON lr.accession_number = e.accession_number AND lr.run_id = e.run_id
		WHERE e.is_material = 1
		GROUP BY e.event_type
		ORDER BY cnt DESC, e.event_type
	`
	rows, err := s.db.QueryContext(ctx, query)
	if err != nil {
		return nil, fmt.Errorf("query material event type counts: %w", err)
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

// EventsByAccession returns one filing's latest-run events, each nested with
// the per-Item classifications it collated (via event_classifications). The
// drill-down for the detail page: events are the headline, the contributing
// Items expand underneath. An event with no linked classifications is returned
// with an empty Items slice (LEFT JOIN). Order: events by id, Items by item.
func (s *store) EventsByAccession(ctx context.Context, accession string) ([]EventWithItems, error) {
	const query = `
		WITH latest AS (
			SELECT e.id, e.run_id, e.accession_number, e.anchor_item_number,
			       e.event_type, e.event_domain, e.is_material, e.confidence, e.summary,
			       f.company_name, f.ticker, f.filing_date, f.form
			  FROM events e
			  JOIN filings f ON f.accession_number = e.accession_number
			 WHERE e.accession_number = ?
			   AND e.run_id = (SELECT MAX(run_id) FROM events WHERE accession_number = ?)
		)
		SELECT
			le.id, le.run_id, le.accession_number, le.anchor_item_number,
			le.event_type, le.event_domain, le.is_material, le.confidence, le.summary,
			le.company_name, le.ticker, le.filing_date, le.form,
			c.id, c.item_number, c.item_title, c.event_type, c.event_domain,
			c.is_material, c.confidence, c.reasoning, c.classifier_version,
			c.taxonomy_version, c.classified_at
		FROM latest le
		LEFT JOIN event_classifications ec ON ec.event_id = le.id
		LEFT JOIN classifications c ON c.id = ec.classification_id
		ORDER BY le.id, c.item_number
	`
	rows, err := s.db.QueryContext(ctx, query, accession, accession)
	if err != nil {
		return nil, fmt.Errorf("query events by accession: %w", err)
	}
	defer rows.Close()

	var order []int64
	byID := make(map[int64]*EventWithItems)
	for rows.Next() {
		var e Event
		var eMaterial int64
		// Nullable classification columns: an event with no linked rows yields
		// a single row with every classification column NULL (LEFT JOIN).
		var (
			cID          sql.NullInt64
			cItemNumber  sql.NullString
			cItemTitle   sql.NullString
			cEventType   sql.NullString
			cEventDomain sql.NullString
			cMaterial    sql.NullInt64
			cConfidence  sql.NullFloat64
			cReasoning   sql.NullString
			cClassifier  sql.NullString
			cTaxonomy    sql.NullString
			cClassified  sql.NullString
		)
		if err := rows.Scan(
			&e.ID, &e.RunID, &e.AccessionNumber, &e.AnchorItemNumber,
			&e.EventType, &e.EventDomain, &eMaterial, &e.Confidence, &e.Summary,
			&e.CompanyName, &e.Ticker, &e.FilingDate, &e.Form,
			&cID, &cItemNumber, &cItemTitle, &cEventType, &cEventDomain,
			&cMaterial, &cConfidence, &cReasoning, &cClassifier, &cTaxonomy, &cClassified,
		); err != nil {
			return nil, fmt.Errorf("scan event with items: %w", err)
		}
		e.IsMaterial = eMaterial != 0

		ewi, seen := byID[e.ID]
		if !seen {
			ewi = &EventWithItems{Event: e}
			byID[e.ID] = ewi
			order = append(order, e.ID)
		}
		if cID.Valid {
			item := Classification{
				ID:                cID.Int64,
				AccessionNumber:   e.AccessionNumber,
				EventType:         cEventType.String,
				EventDomain:       cEventDomain.String,
				IsMaterial:        cMaterial.Int64 != 0,
				Confidence:        cConfidence.Float64,
				Reasoning:         cReasoning.String,
				ClassifierVersion: cClassifier.String,
				TaxonomyVersion:   cTaxonomy.String,
				ClassifiedAt:      cClassified.String,
			}
			if cItemNumber.Valid {
				item.ItemNumber = &cItemNumber.String
			}
			if cItemTitle.Valid {
				item.ItemTitle = &cItemTitle.String
			}
			ewi.Items = append(ewi.Items, item)
		}
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate events by accession: %w", err)
	}

	out := make([]EventWithItems, 0, len(order))
	for _, id := range order {
		out = append(out, *byID[id])
	}
	return out, nil
}
