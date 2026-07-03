// Read-side aggregations over the insider_transactions table (Form 4 data).
// Surfacing only — no signal/scoring here; the orchestrator is still the only
// writer. transaction_date is normalized to YYYYMMDD in comparisons so both
// dashed ("2026-06-22") and undashed ("20260622") stored formats work.

package store

import (
	"context"
	"fmt"
	"strings"
)

// InsiderPulse aggregates a company's open-market insider trading (codes P/S)
// over a rolling window ending now. Dollar sums treat a missing value as zero.
type InsiderPulse struct {
	WindowDays int
	BuyValue   float64
	SellValue  float64
	Buyers     int // distinct insiders with an open-market buy (code P)
	Sellers    int // distinct insiders with an open-market sale (code S)
	BuyCount   int
	SellCount  int
}

// HasActivity reports whether any open-market buy or sell fell in the window.
func (p InsiderPulse) HasActivity() bool { return p.BuyCount > 0 || p.SellCount > 0 }

// InsiderTrade is one recent insider transaction for the company view.
type InsiderTrade struct {
	OwnerName       string
	Role            string
	TransactionDate string
	Code            string
	Shares          float64
	Value           *float64
	Is10b51         bool
}

// CompanyInsiderPulse sums a company's open-market insider buys (P) and sells
// (S) over the trailing windowDays.
func (s *store) CompanyInsiderPulse(ctx context.Context, cik string, windowDays int) (InsiderPulse, error) {
	const q = `
		SELECT
			COALESCE(SUM(CASE WHEN transaction_code='P' THEN COALESCE(transaction_value,0) END),0),
			COALESCE(SUM(CASE WHEN transaction_code='S' THEN COALESCE(transaction_value,0) END),0),
			COUNT(DISTINCT CASE WHEN transaction_code='P' THEN owner_cik END),
			COUNT(DISTINCT CASE WHEN transaction_code='S' THEN owner_cik END),
			COALESCE(SUM(CASE WHEN transaction_code='P' THEN 1 ELSE 0 END),0),
			COALESCE(SUM(CASE WHEN transaction_code='S' THEN 1 ELSE 0 END),0)
		FROM insider_transactions
		WHERE issuer_cik = ?
		  AND replace(transaction_date,'-','') >= strftime('%Y%m%d','now',?)
	`
	p := InsiderPulse{WindowDays: windowDays}
	modifier := fmt.Sprintf("-%d days", windowDays)
	if err := s.db.QueryRowContext(ctx, q, cik, modifier).Scan(
		&p.BuyValue, &p.SellValue, &p.Buyers, &p.Sellers, &p.BuyCount, &p.SellCount,
	); err != nil {
		return InsiderPulse{}, fmt.Errorf("company insider pulse: %w", err)
	}
	return p, nil
}

// CompanyInsiderTrades returns a company's most recent insider transactions
// (all codes), newest first, capped at limit.
func (s *store) CompanyInsiderTrades(ctx context.Context, cik string, limit int) ([]InsiderTrade, error) {
	const q = `
		SELECT owner_name, is_director, is_officer, is_ten_percent_owner, officer_title,
		       transaction_date, transaction_code, shares, transaction_value, is_10b5_1
		FROM insider_transactions
		WHERE issuer_cik = ?
		ORDER BY replace(transaction_date,'-','') DESC, owner_name
		LIMIT ?
	`
	rows, err := s.db.QueryContext(ctx, q, cik, limit)
	if err != nil {
		return nil, fmt.Errorf("company insider trades: %w", err)
	}
	defer rows.Close()

	var out []InsiderTrade
	for rows.Next() {
		var (
			t                            InsiderTrade
			ownerName, officerTitle      *string
			txnDate, code                *string
			shares, value                *float64
			isDir, isOff, isTen, is10b51 int
		)
		if err := rows.Scan(&ownerName, &isDir, &isOff, &isTen, &officerTitle,
			&txnDate, &code, &shares, &value, &is10b51); err != nil {
			return nil, fmt.Errorf("scan insider trade: %w", err)
		}
		t.OwnerName = deref(ownerName)
		t.TransactionDate = deref(txnDate)
		t.Code = deref(code)
		if shares != nil {
			t.Shares = *shares
		}
		t.Value = value
		t.Is10b51 = is10b51 == 1
		t.Role = insiderRole(isDir == 1, isOff == 1, isTen == 1, officerTitle)
		out = append(out, t)
	}
	return out, rows.Err()
}

// InsiderCluster is a company where several insiders made open-market buys in a
// recent window — the notable-activity feed's core item. It's the cross-filing
// aggregation (multiple Form 4s grouped by issuer) that a raw insider feed lacks.
type InsiderCluster struct {
	CIK         string
	Ticker      string
	CompanyName string
	Buyers      int
	Trades      int
	TotalValue  float64
	FirstDate   string
	LastDate    string
}

// NotableInsiderActivity returns companies with at least two distinct insiders
// making open-market buys (code P) within the trailing windowDays, most recent
// cluster first. This is a "cluster buy" — the one insider pattern that showed
// even a modest forward edge and the view no raw insider feed assembles.
func (s *store) NotableInsiderActivity(ctx context.Context, windowDays, limit int) ([]InsiderCluster, error) {
	const q = `
		SELECT issuer_cik,
		       COALESCE(MAX(issuer_ticker), ''),
		       COALESCE(MAX(issuer_name), ''),
		       COUNT(DISTINCT owner_cik),
		       COUNT(*),
		       COALESCE(SUM(COALESCE(transaction_value, 0)), 0),
		       MIN(transaction_date), MAX(transaction_date)
		FROM insider_transactions
		WHERE transaction_code = 'P'
		  AND replace(transaction_date, '-', '') >= strftime('%Y%m%d', 'now', ?)
		GROUP BY issuer_cik
		HAVING COUNT(DISTINCT owner_cik) >= 2
		ORDER BY MAX(replace(transaction_date, '-', '')) DESC, COUNT(DISTINCT owner_cik) DESC
		LIMIT ?
	`
	rows, err := s.db.QueryContext(ctx, q, fmt.Sprintf("-%d days", windowDays), limit)
	if err != nil {
		return nil, fmt.Errorf("notable insider activity: %w", err)
	}
	defer rows.Close()

	var out []InsiderCluster
	for rows.Next() {
		var (
			c           InsiderCluster
			first, last *string
		)
		if err := rows.Scan(&c.CIK, &c.Ticker, &c.CompanyName, &c.Buyers, &c.Trades,
			&c.TotalValue, &first, &last); err != nil {
			return nil, fmt.Errorf("scan insider cluster: %w", err)
		}
		c.FirstDate = deref(first)
		c.LastDate = deref(last)
		out = append(out, c)
	}
	return out, rows.Err()
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

// insiderRole renders a human role label from the Section 16 relationship flags.
func insiderRole(director, officer, tenPct bool, officerTitle *string) string {
	var parts []string
	if officer {
		if officerTitle != nil && strings.TrimSpace(*officerTitle) != "" {
			parts = append(parts, strings.TrimSpace(*officerTitle))
		} else {
			parts = append(parts, "Officer")
		}
	}
	if director {
		parts = append(parts, "Director")
	}
	if tenPct {
		parts = append(parts, "10% Owner")
	}
	if len(parts) == 0 {
		return "Insider"
	}
	return strings.Join(parts, ", ")
}
