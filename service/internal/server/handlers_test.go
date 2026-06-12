package server_test

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	_ "modernc.org/sqlite"

	"github.com/PinchasLev/filings-watcher/service/internal/server"
	"github.com/PinchasLev/filings-watcher/service/internal/store"
)

// fakeStore is a hand-rolled stand-in for the server's storer interface.
// Used to exercise error paths the real SQLite-backed store cannot easily
// produce in tests. If this grows past ~50 lines, replace with mockery v3
// (gitignored generation) per the repo's testing discipline.
type fakeStore struct {
	listResult []store.Classification
	listTotal  int
	listErr    error

	filingResult *store.FilingDetail
	filingErr    error

	eventsResult []store.EventWithItems
	eventsErr    error

	materialEventsResult []store.Event
	materialEventsTotal  int
	materialEventsErr    error

	eventTypeCountResult []store.EventTypeCount
	eventTypeCountErr    error

	lookupCIKResult string
	lookupCIKErr    error

	companyResult *store.Company
	companyEvents []store.Event
	companyTotal  int
	companyErr    error

	liveEventsResult []store.Event
	liveEventsTotal  int
	liveEventsErr    error

	listLiveSinceResult []store.Event
	listLiveSinceErr    error

	trailingSpendByHours map[int]store.SpendSnapshot
	trailingSpendErr     error

	hourlyBucketsResult []store.HourlyBucket
	hourlyBucketsErr    error

	dailyBucketsResult []store.DailyBucket
	dailyBucketsErr    error

	spendStartResult string
	spendStartErr    error

	freshnessResult *string
	freshnessErr    error

	trailingSpendCalledWith []int
	hourlyBucketsCalledWith []int
	dailyBucketsCalledWith  []int

	listCalledWith    struct{ limit, offset int }
	filingCalledWith  string
	eventsCalledWith  string
	lookupCalledWith  string
	companyCalledWith struct {
		cik           string
		limit, offset int
	}
	materialCalledWith struct {
		eventType     string
		limit, offset int
	}
	liveCalledWith struct {
		since         time.Time
		limit, offset int
	}
	listLiveSinceCalledWith []time.Time
	listLiveSinceLimitWith  []int
}

func (f *fakeStore) LatestClassifications(
	_ context.Context, limit, offset int,
) ([]store.Classification, int, error) {
	f.listCalledWith.limit = limit
	f.listCalledWith.offset = offset
	return f.listResult, f.listTotal, f.listErr
}

func (f *fakeStore) FilingByAccession(
	_ context.Context, accession string,
) (*store.FilingDetail, error) {
	f.filingCalledWith = accession
	return f.filingResult, f.filingErr
}

func (f *fakeStore) EventsByAccession(
	_ context.Context, accession string,
) ([]store.EventWithItems, error) {
	f.eventsCalledWith = accession
	return f.eventsResult, f.eventsErr
}

func (f *fakeStore) MaterialEvents(
	_ context.Context, eventType string, limit, offset int,
) ([]store.Event, int, error) {
	f.materialCalledWith.eventType = eventType
	f.materialCalledWith.limit = limit
	f.materialCalledWith.offset = offset
	return f.materialEventsResult, f.materialEventsTotal, f.materialEventsErr
}

func (f *fakeStore) MaterialEventTypeCounts(
	_ context.Context,
) ([]store.EventTypeCount, error) {
	return f.eventTypeCountResult, f.eventTypeCountErr
}

func (f *fakeStore) LookupCIKByTicker(
	_ context.Context, ticker string,
) (string, error) {
	f.lookupCalledWith = ticker
	return f.lookupCIKResult, f.lookupCIKErr
}

func (f *fakeStore) CompanyEvents(
	_ context.Context, cik string, limit, offset int,
) (*store.Company, []store.Event, int, error) {
	f.companyCalledWith.cik = cik
	f.companyCalledWith.limit = limit
	f.companyCalledWith.offset = offset
	return f.companyResult, f.companyEvents, f.companyTotal, f.companyErr
}

func (f *fakeStore) LiveEvents(
	_ context.Context, since time.Time, limit, offset int,
) ([]store.Event, int, error) {
	f.liveCalledWith.since = since
	f.liveCalledWith.limit = limit
	f.liveCalledWith.offset = offset
	return f.liveEventsResult, f.liveEventsTotal, f.liveEventsErr
}

func (f *fakeStore) ListLiveEventsSince(
	_ context.Context, since time.Time, limit int,
) ([]store.Event, error) {
	f.listLiveSinceCalledWith = append(f.listLiveSinceCalledWith, since)
	f.listLiveSinceLimitWith = append(f.listLiveSinceLimitWith, limit)
	return f.listLiveSinceResult, f.listLiveSinceErr
}

func (f *fakeStore) TrailingHoursSpend(_ context.Context, hours int) (store.SpendSnapshot, error) {
	f.trailingSpendCalledWith = append(f.trailingSpendCalledWith, hours)
	if f.trailingSpendErr != nil {
		return store.SpendSnapshot{}, f.trailingSpendErr
	}
	return f.trailingSpendByHours[hours], nil
}

func (f *fakeStore) HourlySpendBuckets(_ context.Context, hours int) ([]store.HourlyBucket, error) {
	f.hourlyBucketsCalledWith = append(f.hourlyBucketsCalledWith, hours)
	return f.hourlyBucketsResult, f.hourlyBucketsErr
}

func (f *fakeStore) DailySpendBuckets(_ context.Context, days int) ([]store.DailyBucket, error) {
	f.dailyBucketsCalledWith = append(f.dailyBucketsCalledWith, days)
	return f.dailyBucketsResult, f.dailyBucketsErr
}

func (f *fakeStore) SpendDataStartDate(_ context.Context) (string, error) {
	return f.spendStartResult, f.spendStartErr
}

func (f *fakeStore) AtomSnapshotFreshness(_ context.Context) (*string, error) {
	return f.freshnessResult, f.freshnessErr
}

// migrationsDir locates the shared SQL migrations directory.
func migrationsDir(t *testing.T) string {
	t.Helper()
	dir, err := filepath.Abs(filepath.Join("..", "..", "..", "orchestrator", "db", "migrations"))
	if err != nil {
		t.Fatalf("resolve migrations dir: %v", err)
	}
	return dir
}

func splitStatements(s string) []string {
	var lines []string
	for _, line := range strings.Split(s, "\n") {
		if i := strings.Index(line, "--"); i >= 0 {
			line = line[:i]
		}
		lines = append(lines, line)
	}
	cleaned := strings.Join(lines, "\n")
	var out []string
	for _, raw := range strings.Split(cleaned, ";") {
		stmt := strings.TrimSpace(raw)
		if stmt != "" {
			out = append(out, stmt)
		}
	}
	return out
}

// seededStore returns a fresh Store with one filing and one classification.
func seededStore(t *testing.T) store.Store {
	t.Helper()
	dbPath := filepath.Join(t.TempDir(), "test.db")
	raw, err := sql.Open("sqlite", dbPath)
	if err != nil {
		t.Fatalf("open raw: %v", err)
	}

	files, err := filepath.Glob(filepath.Join(migrationsDir(t), "*.sql"))
	if err != nil {
		t.Fatalf("glob: %v", err)
	}
	for _, f := range files {
		body, err := os.ReadFile(f)
		if err != nil {
			t.Fatalf("read: %v", err)
		}
		for _, stmt := range splitStatements(string(body)) {
			if _, err := raw.Exec(stmt); err != nil {
				t.Fatalf("exec migration: %v", err)
			}
		}
	}

	if _, err := raw.Exec(`
		INSERT INTO filings (accession_number, cik, ticker, company_name, form,
			filing_date, primary_document, primary_document_url, items_json, fetched_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
	`,
		"0001-26-001", "0000000001", "AAPL", "Apple Inc.", "8-K",
		"2026-04-30", "aapl.htm", "https://www.sec.gov/aapl.htm",
		`[{"number":"2.02"}]`, time.Now().UTC().Format(time.RFC3339Nano),
	); err != nil {
		t.Fatalf("insert filing: %v", err)
	}
	if _, err := raw.Exec(`
		INSERT INTO classifications (accession_number, item_number, item_title,
			event_type, event_domain, is_material, confidence, reasoning,
			classifier_version, taxonomy_version, classified_at)
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
	`,
		"0001-26-001", "2.02", "Results of Operations",
		"earnings_release", "financial", 1, 0.98, "Earnings.",
		"haiku-4.5+prompt-aaaa1111", "v1", time.Now().UTC().Format(time.RFC3339Nano),
	); err != nil {
		t.Fatalf("insert classification: %v", err)
	}
	_ = raw.Close()

	s, err := store.Open(dbPath)
	if err != nil {
		t.Fatalf("open store: %v", err)
	}
	t.Cleanup(func() { _ = s.Close() })
	return s
}

func TestHealthEndpoint(t *testing.T) {
	srv := httptest.NewServer(server.New(seededStore(t)))
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/health")
	if err != nil {
		t.Fatalf("GET /health: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}
	var body map[string]string
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body["status"] != "ok" {
		t.Errorf("status = %q, want ok", body["status"])
	}
}

func TestListFilingsEndpoint(t *testing.T) {
	srv := httptest.NewServer(server.New(seededStore(t)))
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/filings")
	if err != nil {
		t.Fatalf("GET /filings: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}

	var body struct {
		Items  []store.Classification `json:"items"`
		Total  int                    `json:"total"`
		Limit  int                    `json:"limit"`
		Offset int                    `json:"offset"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Total != 1 || len(body.Items) != 1 {
		t.Fatalf("total=%d items=%d, want 1/1", body.Total, len(body.Items))
	}
	if body.Items[0].EventType != "earnings_release" {
		t.Errorf("event_type = %q, want earnings_release", body.Items[0].EventType)
	}
}

func TestFilingDetailEndpoint_Success(t *testing.T) {
	srv := httptest.NewServer(server.New(seededStore(t)))
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/filings/0001-26-001")
	if err != nil {
		t.Fatalf("GET /filings/{accession}: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status = %d, want 200", resp.StatusCode)
	}

	var detail store.FilingDetail
	if err := json.NewDecoder(resp.Body).Decode(&detail); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if detail.Filing.AccessionNumber != "0001-26-001" {
		t.Errorf("accession = %q, want 0001-26-001", detail.Filing.AccessionNumber)
	}
	if len(detail.Classifications) != 1 {
		t.Errorf("classifications = %d, want 1", len(detail.Classifications))
	}
}

func TestFilingDetailEndpoint_NotFound(t *testing.T) {
	srv := httptest.NewServer(server.New(seededStore(t)))
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/filings/does-not-exist")
	if err != nil {
		t.Fatalf("GET nonexistent: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Errorf("status = %d, want 404", resp.StatusCode)
	}
}

func TestListFilings_LimitClampedToMax(t *testing.T) {
	srv := httptest.NewServer(server.New(seededStore(t)))
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/filings?limit=10000")
	if err != nil {
		t.Fatalf("GET: %v", err)
	}
	defer resp.Body.Close()

	var body struct {
		Limit int `json:"limit"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Limit > 100 {
		t.Errorf("limit = %d, want clamped to <= 100", body.Limit)
	}
}

func TestListFilings_StoreErrorReturns500(t *testing.T) {
	fake := &fakeStore{listErr: errors.New("simulated store failure")}
	srv := httptest.NewServer(server.New(fake))
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/filings")
	if err != nil {
		t.Fatalf("GET /filings: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusInternalServerError {
		t.Errorf("status = %d, want 500", resp.StatusCode)
	}
}

func TestFilingDetail_StoreErrorReturns500(t *testing.T) {
	fake := &fakeStore{filingErr: errors.New("simulated store failure")}
	srv := httptest.NewServer(server.New(fake))
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/filings/any")
	if err != nil {
		t.Fatalf("GET: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusInternalServerError {
		t.Errorf("status = %d, want 500", resp.StatusCode)
	}
}

func TestFilingDetail_NotFoundSentinelReturns404(t *testing.T) {
	fake := &fakeStore{filingErr: store.ErrNotFound}
	srv := httptest.NewServer(server.New(fake))
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/filings/whatever")
	if err != nil {
		t.Fatalf("GET: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Errorf("status = %d, want 404", resp.StatusCode)
	}
}

func TestListFilings_PassesQueryParamsToStore(t *testing.T) {
	fake := &fakeStore{}
	srv := httptest.NewServer(server.New(fake))
	defer srv.Close()

	_, err := http.Get(srv.URL + "/filings?limit=7&offset=3")
	if err != nil {
		t.Fatalf("GET: %v", err)
	}
	if fake.listCalledWith.limit != 7 {
		t.Errorf("limit passed to store = %d, want 7", fake.listCalledWith.limit)
	}
	if fake.listCalledWith.offset != 3 {
		t.Errorf("offset passed to store = %d, want 3", fake.listCalledWith.offset)
	}
}
