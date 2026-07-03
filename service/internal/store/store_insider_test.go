package store_test

import (
	"context"
	"database/sql"
	"testing"
	"time"
)

func insertInsiderTxn(
	t *testing.T, db *sql.DB, accession, cik, owner, name, code string, value float64, daysAgo int,
) {
	t.Helper()
	date := time.Now().AddDate(0, 0, -daysAgo).Format("2006-01-02") // dashed → exercises normalization
	_, err := db.Exec(
		`INSERT INTO insider_transactions
			(accession_number, txn_seq, filed_at, issuer_cik, issuer_ticker, owner_cik, owner_name,
			 is_officer, officer_title, transaction_date, transaction_code, shares, transaction_value,
			 is_10b5_1, ingested_at)
		 VALUES (?, 0, ?, ?, 'TST', ?, ?, 1, 'CEO', ?, ?, 100, ?, 0, '2026-01-01T00:00:00Z')`,
		accession, date, cik, owner, name, date, code, value,
	)
	if err != nil {
		t.Fatalf("insert insider txn: %v", err)
	}
}

func TestCompanyInsiderPulseAndTrades(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	const cik = "0000000999"
	insertInsiderTxn(t, raw, "acc1", cik, "OWNER_A", "ALICE", "P", 10000, 5)  // in 30d
	insertInsiderTxn(t, raw, "acc2", cik, "OWNER_B", "BOB", "P", 20000, 45)   // in 90d only
	insertInsiderTxn(t, raw, "acc3", cik, "OWNER_C", "CAROL", "S", 5000, 10)  // sell in 30d
	insertInsiderTxn(t, raw, "acc4", cik, "OWNER_D", "DAVE", "P", 99000, 200) // outside both
	_ = raw.Close()

	s := openStore(t, dbPath)
	ctx := context.Background()

	p30, err := s.CompanyInsiderPulse(ctx, cik, 30)
	if err != nil {
		t.Fatalf("pulse30: %v", err)
	}
	if p30.BuyValue != 10000 || p30.Buyers != 1 || p30.BuyCount != 1 {
		t.Errorf("pulse30 buys = %v/%d/%d, want 10000/1/1", p30.BuyValue, p30.Buyers, p30.BuyCount)
	}
	if p30.SellValue != 5000 || p30.Sellers != 1 || p30.SellCount != 1 {
		t.Errorf("pulse30 sells = %v/%d/%d, want 5000/1/1", p30.SellValue, p30.Sellers, p30.SellCount)
	}
	if !p30.HasActivity() {
		t.Error("pulse30 HasActivity = false, want true")
	}

	p90, err := s.CompanyInsiderPulse(ctx, cik, 90)
	if err != nil {
		t.Fatalf("pulse90: %v", err)
	}
	if p90.BuyValue != 30000 || p90.Buyers != 2 || p90.BuyCount != 2 {
		t.Errorf("pulse90 buys = %v/%d/%d, want 30000/2/2 (200d-old buy must be excluded)",
			p90.BuyValue, p90.Buyers, p90.BuyCount)
	}

	trades, err := s.CompanyInsiderTrades(ctx, cik, 20)
	if err != nil {
		t.Fatalf("trades: %v", err)
	}
	if len(trades) != 4 {
		t.Fatalf("trades = %d, want 4 (all codes, all dates)", len(trades))
	}
	if trades[0].OwnerName != "ALICE" { // newest first (5 days ago)
		t.Errorf("first trade owner = %q, want ALICE", trades[0].OwnerName)
	}
	if trades[0].Role != "CEO" {
		t.Errorf("first trade role = %q, want CEO", trades[0].Role)
	}
	if trades[0].Value == nil || *trades[0].Value != 10000 {
		t.Errorf("first trade value = %v, want 10000", trades[0].Value)
	}
}

func TestLookupCIKByTicker_InsiderFallback(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	// "TST" is absent from cik_tickers (the SEC map) and present only in insider
	// data via issuer_ticker — the discoverability gap the fallback closes.
	insertInsiderTxn(t, raw, "accX", "0000042042", "OWNER_X", "XAVIER", "P", 1000, 3)
	_ = raw.Close()
	s := openStore(t, dbPath)

	cik, err := s.LookupCIKByTicker(context.Background(), "tst")
	if err != nil {
		t.Fatalf("LookupCIKByTicker fallback: %v", err)
	}
	if cik != "0000042042" {
		t.Errorf("cik = %q, want 0000042042 (resolved via insider issuer_ticker)", cik)
	}
}

func TestNotableInsiderActivity_ClustersOnly(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	// A cluster: two distinct insiders buying the same issuer, recently.
	insertInsiderTxn(t, raw, "c1", "0000000111", "OWN_A", "ALICE", "P", 5000, 4)
	insertInsiderTxn(t, raw, "c2", "0000000111", "OWN_B", "BOB", "P", 7000, 3)
	// A lone buyer at another issuer — not a cluster, must be excluded.
	insertInsiderTxn(t, raw, "l1", "0000000222", "OWN_C", "CAROL", "P", 9000, 2)
	// An old cluster, outside the 30-day window — excluded.
	insertInsiderTxn(t, raw, "o1", "0000000333", "OWN_D", "DAVE", "P", 1000, 200)
	insertInsiderTxn(t, raw, "o2", "0000000333", "OWN_E", "EVE", "P", 1000, 201)
	// A recent 2-buyer cluster below the dollar floor — excluded.
	insertInsiderTxn(t, raw, "s1", "0000000444", "OWN_F", "FRANK", "P", 1000, 3)
	insertInsiderTxn(t, raw, "s2", "0000000444", "OWN_G", "GRETA", "P", 2000, 3)
	_ = raw.Close()

	s := openStore(t, dbPath)
	clusters, err := s.NotableInsiderActivity(context.Background(), 30, 10000, 60)
	if err != nil {
		t.Fatalf("NotableInsiderActivity: %v", err)
	}
	if len(clusters) != 1 {
		t.Fatalf("clusters = %d, want 1 (only the recent >=2-buyer issuer)", len(clusters))
	}
	c := clusters[0]
	if c.CIK != "0000000111" || c.Buyers != 2 || c.Trades != 2 {
		t.Errorf("cluster = %+v, want cik 0000000111 / 2 buyers / 2 trades", c)
	}
	if c.TotalValue != 12000 {
		t.Errorf("total value = %v, want 12000", c.TotalValue)
	}
}

func TestCompanyInsiderPulse_NoData(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	_ = raw.Close()
	s := openStore(t, dbPath)

	p, err := s.CompanyInsiderPulse(context.Background(), "0000000000", 30)
	if err != nil {
		t.Fatalf("pulse: %v", err)
	}
	if p.HasActivity() {
		t.Errorf("HasActivity = true for a company with no insider rows, want false")
	}
}
