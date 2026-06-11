package store_test

import (
	"context"
	"database/sql"
	"testing"
	"time"
)

// insertFilingWithSubmittedAt seeds a second filing with an explicit
// submitted_at, so LiveEvents tests can mix timestamped and untimestamped
// rows without disturbing the default seedFilingAndClassifications fixture.
func insertFilingWithSubmittedAt(
	t *testing.T, db *sql.DB,
	accession, cik, company string, submittedAt *string,
) {
	t.Helper()
	const q = `
		INSERT INTO filings (accession_number, cik, ticker, company_name, form,
			filing_date, primary_document, primary_document_url, items_json,
			fetched_at, submitted_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
	`
	if _, err := db.Exec(q,
		accession, cik, nil, company, "8-K",
		"2026-04-30", "x.htm", "https://www.sec.gov/x.htm", `[]`,
		time.Now().UTC().Format(time.RFC3339Nano), submittedAt,
	); err != nil {
		t.Fatalf("insert filing %s: %v", accession, err)
	}
}

// TestLiveEvents_ExcludesNullSubmittedAt is the central guarantee of the
// live tape: rows without a precise EDGAR-side timestamp (the daily-index
// path) are out of scope. Only atom-ingested filings — the ones that
// carry submitted_at — appear.
func TestLiveEvents_ExcludesNullSubmittedAt(t *testing.T) {
	dbPath, raw := freshDBPath(t)

	// Seeded filing (0001-26-001) has NULL submitted_at.
	r := insertRun(t, raw, "reduce", "succeeded")
	insertEvent(t, raw, r, "0001-26-001", "2.02", "earnings_release", "financial", true, 0.95, "no submitted_at")

	// A second filing with submitted_at set, and an event under it.
	atomSubmitted := "2026-06-05T14:00:00-04:00"
	insertFilingWithSubmittedAt(t, raw, "0002-26-002", "0000000002", "Atom Co.", &atomSubmitted)
	insertEvent(t, raw, r, "0002-26-002", "5.02", "exec_departure", "governance", true, 0.90, "with submitted_at")

	_ = raw.Close()
	s := openStore(t, dbPath)

	since := time.Date(2026, 6, 1, 0, 0, 0, 0, time.UTC)
	events, total, err := s.LiveEvents(context.Background(), since, 50, 0)
	if err != nil {
		t.Fatalf("LiveEvents: %v", err)
	}
	if total != 1 || len(events) != 1 {
		t.Fatalf("total/len = %d/%d, want 1/1 (NULL submitted_at excluded)", total, len(events))
	}
	if events[0].AccessionNumber != "0002-26-002" {
		t.Errorf("accession = %q, want 0002-26-002 (atom-ingested)", events[0].AccessionNumber)
	}
	if events[0].SubmittedAt == nil || *events[0].SubmittedAt != atomSubmitted {
		t.Errorf("submitted_at = %v, want %q", events[0].SubmittedAt, atomSubmitted)
	}
}

// TestLiveEvents_HonorsSinceWindow checks that events older than `since`
// are excluded — the rolling-window semantic of the live tape.
func TestLiveEvents_HonorsSinceWindow(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	r := insertRun(t, raw, "reduce", "succeeded")

	oldTS := "2026-06-05T10:00:00-04:00" // 14:00 UTC
	newTS := "2026-06-05T13:30:00-04:00" // 17:30 UTC
	insertFilingWithSubmittedAt(t, raw, "0010-26-010", "0000000010", "Old Co.", &oldTS)
	insertFilingWithSubmittedAt(t, raw, "0011-26-011", "0000000011", "New Co.", &newTS)
	insertEvent(t, raw, r, "0010-26-010", "2.02", "earnings_release", "financial", true, 0.95, "old")
	insertEvent(t, raw, r, "0011-26-011", "5.02", "exec_departure", "governance", true, 0.90, "new")

	_ = raw.Close()
	s := openStore(t, dbPath)

	// Window starts at 16:00 UTC; only the 17:30 event qualifies.
	since := time.Date(2026, 6, 5, 16, 0, 0, 0, time.UTC)
	events, total, err := s.LiveEvents(context.Background(), since, 50, 0)
	if err != nil {
		t.Fatalf("LiveEvents: %v", err)
	}
	if total != 1 || len(events) != 1 {
		t.Fatalf("total/len = %d/%d, want 1/1 (older filing outside window)", total, len(events))
	}
	if events[0].AccessionNumber != "0011-26-011" {
		t.Errorf("accession = %q, want 0011-26-011 (inside window)", events[0].AccessionNumber)
	}
}

// TestLiveEvents_OrdersBySubmittedAtDESC confirms newest-first ordering by
// the precise submission timestamp — the live tape's defining axis.
func TestLiveEvents_OrdersBySubmittedAtDESC(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	r := insertRun(t, raw, "reduce", "succeeded")

	earlier := "2026-06-05T12:00:00-04:00"
	later := "2026-06-05T13:30:00-04:00"
	// Insert in reverse-chronological-of-arrival order to prove the query
	// sort is doing the work, not insertion order.
	insertFilingWithSubmittedAt(t, raw, "0020-26-020", "0000000020", "Earlier Co.", &earlier)
	insertFilingWithSubmittedAt(t, raw, "0021-26-021", "0000000021", "Later Co.", &later)
	insertEvent(t, raw, r, "0020-26-020", "2.02", "earnings_release", "financial", true, 0.95, "earlier")
	insertEvent(t, raw, r, "0021-26-021", "5.02", "exec_departure", "governance", true, 0.90, "later")

	_ = raw.Close()
	s := openStore(t, dbPath)

	since := time.Date(2026, 6, 1, 0, 0, 0, 0, time.UTC)
	events, _, err := s.LiveEvents(context.Background(), since, 50, 0)
	if err != nil {
		t.Fatalf("LiveEvents: %v", err)
	}
	if len(events) != 2 {
		t.Fatalf("len = %d, want 2", len(events))
	}
	if events[0].AccessionNumber != "0021-26-021" {
		t.Errorf("first = %q, want later filing 0021-26-021", events[0].AccessionNumber)
	}
	if events[1].AccessionNumber != "0020-26-020" {
		t.Errorf("second = %q, want earlier filing 0020-26-020", events[1].AccessionNumber)
	}
}

// TestCountLiveEventsSince_StrictGreaterThan confirms the count uses ">"
// (strict), not ">=" — the boundary event the page rendered with shouldn't
// re-trigger the freshness banner on every poll.
func TestCountLiveEventsSince_StrictGreaterThan(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	r := insertRun(t, raw, "reduce", "succeeded")

	boundary := "2026-06-11T15:30:00-04:00"
	after := "2026-06-11T15:31:00-04:00"
	insertFilingWithSubmittedAt(t, raw, "0040-26-040", "0000000040", "Boundary Co.", &boundary)
	insertFilingWithSubmittedAt(t, raw, "0041-26-041", "0000000041", "After Co.", &after)
	insertEvent(t, raw, r, "0040-26-040", "2.02", "earnings_release", "financial", true, 0.95, "boundary")
	insertEvent(t, raw, r, "0041-26-041", "5.02", "exec_departure", "governance", true, 0.90, "after")

	_ = raw.Close()
	s := openStore(t, dbPath)

	// "since" equal to the boundary timestamp — the boundary event itself
	// must NOT count (strict >). Only the after event qualifies.
	since, _ := time.Parse(time.RFC3339, boundary)
	n, err := s.CountLiveEventsSince(context.Background(), since)
	if err != nil {
		t.Fatalf("CountLiveEventsSince: %v", err)
	}
	if n != 1 {
		t.Errorf("n = %d, want 1 (boundary event excluded, after event included)", n)
	}
}

// TestCountLiveEventsSince_ExcludesNullSubmittedAt mirrors LiveEvents:
// daily-index reconciled rows lack sub-day timestamps and don't
// belong on the "right now" surface.
func TestCountLiveEventsSince_ExcludesNullSubmittedAt(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	r := insertRun(t, raw, "reduce", "succeeded")

	// Seeded filing has NULL submitted_at. Add a row's worth of events.
	insertEvent(t, raw, r, "0001-26-001", "2.02", "earnings_release", "financial", true, 0.95, "daily-index style")

	// Plus one atom-style row.
	after := "2026-06-11T15:31:00-04:00"
	insertFilingWithSubmittedAt(t, raw, "0050-26-050", "0000000050", "Atom Co.", &after)
	insertEvent(t, raw, r, "0050-26-050", "5.02", "exec_departure", "governance", true, 0.90, "atom-style")

	_ = raw.Close()
	s := openStore(t, dbPath)

	since := time.Date(2026, 6, 1, 0, 0, 0, 0, time.UTC)
	n, err := s.CountLiveEventsSince(context.Background(), since)
	if err != nil {
		t.Fatalf("CountLiveEventsSince: %v", err)
	}
	if n != 1 {
		t.Errorf("n = %d, want 1 (NULL submitted_at excluded)", n)
	}
}

// TestLiveEvents_FiltersNonMaterial confirms the same materiality gate as
// the home page — non-material events stay off the live tape so the surface
// stays consistent across views.
func TestLiveEvents_FiltersNonMaterial(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	r := insertRun(t, raw, "reduce", "succeeded")

	ts := "2026-06-05T13:30:00-04:00"
	insertFilingWithSubmittedAt(t, raw, "0030-26-030", "0000000030", "Mixed Co.", &ts)
	insertEvent(t, raw, r, "0030-26-030", "2.02", "earnings_release", "financial", true, 0.95, "material")
	insertEvent(t, raw, r, "0030-26-030", "5.02", "exec_departure", "governance", false, 0.40, "non-material")

	_ = raw.Close()
	s := openStore(t, dbPath)

	since := time.Date(2026, 6, 1, 0, 0, 0, 0, time.UTC)
	_, total, err := s.LiveEvents(context.Background(), since, 50, 0)
	if err != nil {
		t.Fatalf("LiveEvents: %v", err)
	}
	if total != 1 {
		t.Fatalf("total = %d, want 1 (non-material excluded)", total)
	}
}
