package store_test

import (
	"context"
	"database/sql"
	"errors"
	"testing"
	"time"

	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

// insertRun adds a runs-ledger row and returns its run_id. run_id is the
// monotonic versioning axis the events read path orders on (ADR 0028).
func insertRun(t *testing.T, db *sql.DB, stage, status string) int64 {
	t.Helper()
	res, err := db.Exec(
		`INSERT INTO runs (stage, config_version, taxonomy_version, status, started_at)
		 VALUES (?, ?, ?, ?, ?)`,
		stage, "reducer-test+v1", "v1", status, time.Now().UTC().Format(time.RFC3339Nano),
	)
	if err != nil {
		t.Fatalf("insert run: %v", err)
	}
	id, err := res.LastInsertId()
	if err != nil {
		t.Fatalf("run last insert id: %v", err)
	}
	return id
}

// insertEvent adds one event row under runID and returns its id.
func insertEvent(
	t *testing.T, db *sql.DB, runID int64,
	accession, anchor, eventType, domain string, material bool, conf float64, summary string,
) int64 {
	t.Helper()
	mat := 0
	if material {
		mat = 1
	}
	res, err := db.Exec(
		`INSERT INTO events (run_id, accession_number, anchor_item_number,
			event_type, event_domain, is_material, confidence, summary)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
		runID, accession, anchor, eventType, domain, mat, conf, summary,
	)
	if err != nil {
		t.Fatalf("insert event: %v", err)
	}
	id, err := res.LastInsertId()
	if err != nil {
		t.Fatalf("event last insert id: %v", err)
	}
	return id
}

// classificationID looks up a seeded classification row id by (accession, item).
func classificationID(t *testing.T, db *sql.DB, accession, item string) int64 {
	t.Helper()
	var id int64
	if err := db.QueryRow(
		`SELECT id FROM classifications WHERE accession_number = ? AND item_number = ?`,
		accession, item,
	).Scan(&id); err != nil {
		t.Fatalf("lookup classification id (%s, %s): %v", accession, item, err)
	}
	return id
}

// linkEventClassification records that an event collated a classification row.
func linkEventClassification(t *testing.T, db *sql.DB, eventID, classID int64) {
	t.Helper()
	if _, err := db.Exec(
		`INSERT INTO event_classifications (event_id, classification_id) VALUES (?, ?)`,
		eventID, classID,
	); err != nil {
		t.Fatalf("link event %d -> classification %d: %v", eventID, classID, err)
	}
}

// TestMaterialEvents_LatestRunWholesale_DropsOrphans is the central guard from
// ADR 0028: when a newer run emits fewer events than an older one, the read
// must surface ONLY the newer run's events wholesale. An anchor that existed
// only in the older, larger run must not resurface — which a per-anchor maximum
// would wrongly do.
func TestMaterialEvents_LatestRunWholesale_DropsOrphans(t *testing.T) {
	dbPath, raw := freshDBPath(t)

	// Older run R1: two events for the filing.
	r1 := insertRun(t, raw, "reduce", "succeeded")
	insertEvent(t, raw, r1, "0001-26-001", "2.02", "earnings_release", "financial", true, 0.95, "R1 earnings")
	insertEvent(t, raw, r1, "0001-26-001", "5.02", "exec_departure", "governance", true, 0.90, "R1 departure (orphan)")

	// Newer run R2: a single event. The 5.02 event exists only in R1.
	r2 := insertRun(t, raw, "reduce", "succeeded")
	insertEvent(t, raw, r2, "0001-26-001", "2.02", "earnings_release", "financial", true, 0.97, "R2 earnings")

	_ = raw.Close()
	s := openStore(t, dbPath)

	events, total, err := s.MaterialEvents(context.Background(), "", 50, 0)
	if err != nil {
		t.Fatalf("MaterialEvents: %v", err)
	}
	if total != 1 {
		t.Fatalf("total = %d, want 1 (only R2's wholesale output)", total)
	}
	if len(events) != 1 {
		t.Fatalf("events = %d, want 1", len(events))
	}
	if events[0].RunID != r2 {
		t.Errorf("run_id = %d, want latest run %d", events[0].RunID, r2)
	}
	if events[0].Summary != "R2 earnings" {
		t.Errorf("summary = %q, want %q", events[0].Summary, "R2 earnings")
	}
	for _, e := range events {
		if e.AnchorItemNumber != nil && *e.AnchorItemNumber == "5.02" {
			t.Errorf("orphan 5.02 event from run %d resurfaced", r1)
		}
	}
}

// TestMaterialEvents_ReturnsFilingFields confirms the denormalized company,
// ticker, and filing_date ride along for list rendering.
func TestMaterialEvents_ReturnsFilingFields(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	r := insertRun(t, raw, "reduce", "succeeded")
	insertEvent(t, raw, r, "0001-26-001", "2.02", "earnings_release", "financial", true, 0.95, "earnings")
	_ = raw.Close()
	s := openStore(t, dbPath)

	events, _, err := s.MaterialEvents(context.Background(), "", 50, 0)
	if err != nil {
		t.Fatalf("MaterialEvents: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("events = %d, want 1", len(events))
	}
	if events[0].CompanyName != "Apple Inc." {
		t.Errorf("company_name = %q, want Apple Inc.", events[0].CompanyName)
	}
	if events[0].Ticker == nil || *events[0].Ticker != "AAPL" {
		t.Errorf("ticker = %v, want AAPL", events[0].Ticker)
	}
	if events[0].FilingDate != "2026-04-30" {
		t.Errorf("filing_date = %q, want 2026-04-30", events[0].FilingDate)
	}
}

// TestMaterialEvents_FiltersMaterialityAndEventType checks the is_material gate
// and the optional event_type filter.
func TestMaterialEvents_FiltersMaterialityAndEventType(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	r := insertRun(t, raw, "reduce", "succeeded")
	insertEvent(t, raw, r, "0001-26-001", "2.02", "earnings_release", "financial", true, 0.95, "earnings")
	insertEvent(t, raw, r, "0001-26-001", "5.02", "exec_departure", "governance", false, 0.60, "non-material departure")
	insertEvent(t, raw, r, "0001-26-001", "8.01", "other_material", "other", true, 0.80, "other")
	_ = raw.Close()
	s := openStore(t, dbPath)

	all, total, err := s.MaterialEvents(context.Background(), "", 50, 0)
	if err != nil {
		t.Fatalf("MaterialEvents (no filter): %v", err)
	}
	if total != 2 || len(all) != 2 {
		t.Fatalf("material total/len = %d/%d, want 2/2 (non-material excluded)", total, len(all))
	}

	earnings, total, err := s.MaterialEvents(context.Background(), "earnings_release", 50, 0)
	if err != nil {
		t.Fatalf("MaterialEvents (filtered): %v", err)
	}
	if total != 1 || len(earnings) != 1 || earnings[0].EventType != "earnings_release" {
		t.Fatalf("filtered total/len/type = %d/%d/%q, want 1/1/earnings_release",
			total, len(earnings), eventTypeOf(earnings))
	}
}

// TestMaterialEvents_RespectsLimitAndOffset checks pagination over events.
func TestMaterialEvents_RespectsLimitAndOffset(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	r := insertRun(t, raw, "reduce", "succeeded")
	insertEvent(t, raw, r, "0001-26-001", "2.02", "earnings_release", "financial", true, 0.95, "a")
	insertEvent(t, raw, r, "0001-26-001", "5.02", "exec_departure", "governance", true, 0.90, "b")
	_ = raw.Close()
	s := openStore(t, dbPath)

	page1, total, err := s.MaterialEvents(context.Background(), "", 1, 0)
	if err != nil {
		t.Fatalf("page 1: %v", err)
	}
	page2, _, err := s.MaterialEvents(context.Background(), "", 1, 1)
	if err != nil {
		t.Fatalf("page 2: %v", err)
	}
	if total != 2 {
		t.Fatalf("total = %d, want 2", total)
	}
	if len(page1) != 1 || len(page2) != 1 {
		t.Fatalf("page sizes = %d/%d, want 1/1", len(page1), len(page2))
	}
	if page1[0].ID == page2[0].ID {
		t.Errorf("pages returned the same event: id %d", page1[0].ID)
	}
}

// TestMaterialEventTypeCounts_LatestRunMaterialOnly checks the chip counts
// exclude non-material events and come from the latest run.
func TestMaterialEventTypeCounts_LatestRunMaterialOnly(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	r := insertRun(t, raw, "reduce", "succeeded")
	insertEvent(t, raw, r, "0001-26-001", "2.02", "earnings_release", "financial", true, 0.95, "earnings")
	insertEvent(t, raw, r, "0001-26-001", "5.02", "exec_departure", "governance", false, 0.60, "non-material")
	_ = raw.Close()
	s := openStore(t, dbPath)

	counts, err := s.MaterialEventTypeCounts(context.Background())
	if err != nil {
		t.Fatalf("MaterialEventTypeCounts: %v", err)
	}
	if len(counts) != 1 {
		t.Fatalf("counts = %d, want 1 (non-material excluded)", len(counts))
	}
	if counts[0].EventType != "earnings_release" || counts[0].Count != 1 {
		t.Errorf("got %+v, want earnings_release:1", counts[0])
	}
}

// TestEventsByAccession_NestsContributingClassifications is the drill-down: one
// consolidated event collating both seeded Items must expand to those two
// per-Item classifications, ordered by item number.
func TestEventsByAccession_NestsContributingClassifications(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	r := insertRun(t, raw, "reduce", "succeeded")
	evID := insertEvent(t, raw, r, "0001-26-001", "2.02",
		"earnings_release", "financial", true, 0.96, "Consolidated: earnings + departure")
	linkEventClassification(t, raw, evID, classificationID(t, raw, "0001-26-001", "2.02"))
	linkEventClassification(t, raw, evID, classificationID(t, raw, "0001-26-001", "5.02"))
	_ = raw.Close()
	s := openStore(t, dbPath)

	ewis, err := s.EventsByAccession(context.Background(), "0001-26-001")
	if err != nil {
		t.Fatalf("EventsByAccession: %v", err)
	}
	if len(ewis) != 1 {
		t.Fatalf("events = %d, want 1", len(ewis))
	}
	if ewis[0].Summary != "Consolidated: earnings + departure" {
		t.Errorf("summary = %q", ewis[0].Summary)
	}
	if len(ewis[0].Items) != 2 {
		t.Fatalf("nested items = %d, want 2", len(ewis[0].Items))
	}
	if got := derefItem(ewis[0].Items[0].ItemNumber); got != "2.02" {
		t.Errorf("first item = %q, want 2.02", got)
	}
	if got := derefItem(ewis[0].Items[1].ItemNumber); got != "5.02" {
		t.Errorf("second item = %q, want 5.02", got)
	}
	if ewis[0].Items[0].Reasoning == "" {
		t.Error("nested classification missing reasoning")
	}
}

// TestEventsByAccession_EventWithoutLinksHasNoItems confirms the LEFT JOIN path:
// an event with no contributing classifications still returns, with empty Items.
func TestEventsByAccession_EventWithoutLinksHasNoItems(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	r := insertRun(t, raw, "reduce", "succeeded")
	insertEvent(t, raw, r, "0001-26-001", "8.01", "other_material", "other", true, 0.70, "Unlinked event")
	_ = raw.Close()
	s := openStore(t, dbPath)

	ewis, err := s.EventsByAccession(context.Background(), "0001-26-001")
	if err != nil {
		t.Fatalf("EventsByAccession: %v", err)
	}
	if len(ewis) != 1 {
		t.Fatalf("events = %d, want 1", len(ewis))
	}
	if len(ewis[0].Items) != 0 {
		t.Errorf("items = %d, want 0", len(ewis[0].Items))
	}
}

// TestEventsByAccession_OnlyLatestRun confirms the detail drill-down also
// respects latest-run-wholesale: an older run's events do not appear.
func TestEventsByAccession_OnlyLatestRun(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	r1 := insertRun(t, raw, "reduce", "succeeded")
	insertEvent(t, raw, r1, "0001-26-001", "2.02", "earnings_release", "financial", true, 0.95, "old")
	insertEvent(t, raw, r1, "0001-26-001", "5.02", "exec_departure", "governance", true, 0.90, "old orphan")
	r2 := insertRun(t, raw, "reduce", "succeeded")
	insertEvent(t, raw, r2, "0001-26-001", "2.02", "earnings_release", "financial", true, 0.97, "new")
	_ = raw.Close()
	s := openStore(t, dbPath)

	ewis, err := s.EventsByAccession(context.Background(), "0001-26-001")
	if err != nil {
		t.Fatalf("EventsByAccession: %v", err)
	}
	if len(ewis) != 1 {
		t.Fatalf("events = %d, want 1 (latest run only)", len(ewis))
	}
	if ewis[0].RunID != r2 || ewis[0].Summary != "new" {
		t.Errorf("got run %d %q, want run %d %q", ewis[0].RunID, ewis[0].Summary, r2, "new")
	}
}

// TestCompanyEvents_ScopedToCIKWithIdentity checks the per-company events read
// returns the resolved identity and only that CIK's events.
func TestCompanyEvents_ScopedToCIKWithIdentity(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	r := insertRun(t, raw, "reduce", "succeeded")
	insertEvent(t, raw, r, "0001-26-001", "2.02", "earnings_release", "financial", true, 0.95, "earnings")
	_ = raw.Close()
	s := openStore(t, dbPath)

	company, events, total, err := s.CompanyEvents(context.Background(), "0000000001", 50, 0)
	if err != nil {
		t.Fatalf("CompanyEvents: %v", err)
	}
	if company == nil || company.CIK != "0000000001" {
		t.Fatalf("company = %+v, want CIK 0000000001", company)
	}
	if company.CompanyName != "Apple Inc." {
		t.Errorf("company name = %q, want Apple Inc.", company.CompanyName)
	}
	if total != 1 || len(events) != 1 {
		t.Fatalf("total/len = %d/%d, want 1/1", total, len(events))
	}
	if events[0].CompanyName != "Apple Inc." {
		t.Errorf("event company_name = %q, want Apple Inc.", events[0].CompanyName)
	}
}

// TestCompanyEvents_UnknownCIKReturnsErrNotFound mirrors CompanyByCIK's contract.
func TestCompanyEvents_UnknownCIKReturnsErrNotFound(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	_ = raw.Close()
	s := openStore(t, dbPath)

	_, _, _, err := s.CompanyEvents(context.Background(), "9999999999", 50, 0)
	if !errors.Is(err, store.ErrNotFound) {
		t.Fatalf("want ErrNotFound, got %v", err)
	}
}

// derefItem renders an optional item number for assertions; nil becomes "".
func derefItem(p *string) string {
	if p == nil {
		return ""
	}
	return *p
}

// eventTypeOf safely reads the first event's type for an error message.
func eventTypeOf(events []store.Event) string {
	if len(events) == 0 {
		return "<none>"
	}
	return events[0].EventType
}
