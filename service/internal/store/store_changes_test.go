package store_test

import (
	"context"
	"database/sql"
	"fmt"
	"testing"
)

const _section = "risk_factors"
const _model = "voyage-finance-2"

func insPeriodic(t *testing.T, db *sql.DB, acc, cik, period string) {
	t.Helper()
	fy := 0
	if len(period) >= 4 {
		fmt.Sscanf(period[:4], "%d", &fy)
	}
	_, err := db.Exec(
		`INSERT INTO periodic_filings
			(accession_number, cik, company_name, form, filed_at, period_of_report,
			 fiscal_year, parsed, block_count, ingested_at)
		 VALUES (?, ?, 'ACME', '10-K', '2026-01-01', ?, ?, 1, 0, 't')`,
		acc, cik, period, fy,
	)
	if err != nil {
		t.Fatalf("insert periodic_filing: %v", err)
	}
}

func insBlock(t *testing.T, db *sql.DB, acc string, idx int, heading string) {
	t.Helper()
	_, err := db.Exec(
		`INSERT INTO filing_blocks (accession_number, section, block_index, heading, block_text, block_hash)
		 VALUES (?, ?, ?, ?, 'body', ?)`,
		acc, _section, idx, heading, fmt.Sprintf("%s-%d", acc, idx),
	)
	if err != nil {
		t.Fatalf("insert filing_block: %v", err)
	}
}

func insDiff(t *testing.T, db *sql.DB, acc, prior string) {
	t.Helper()
	_, err := db.Exec(
		`INSERT INTO filing_diffs
			(accession_number, section, model_id, prior_accession_number,
			 added_count, changed_count, carried_count, dropped_count, computed_at)
		 VALUES (?, ?, ?, ?, 1, 1, 0, 1, 't')`,
		acc, _section, _model, prior,
	)
	if err != nil {
		t.Fatalf("insert filing_diff: %v", err)
	}
}

func insChange(t *testing.T, db *sql.DB, acc string, seq int, changeType string, curIdx, priIdx any, prior string, sim float64) {
	t.Helper()
	_, err := db.Exec(
		`INSERT INTO block_changes
			(accession_number, section, model_id, change_seq, change_type,
			 current_block_index, prior_block_index, prior_accession_number, similarity)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		acc, _section, _model, seq, changeType, curIdx, priIdx, prior, sim,
	)
	if err != nil {
		t.Fatalf("insert block_change: %v", err)
	}
}

func insVerdict(t *testing.T, db *sql.DB, acc string, seq int, jv string, material bool, cat, expl string, review bool, judgedAt string) {
	t.Helper()
	m, r := 0, 0
	if material {
		m = 1
	}
	if review {
		r = 1
	}
	_, err := db.Exec(
		`INSERT INTO block_change_verdicts
			(accession_number, section, model_id, change_seq, judge_version,
			 is_material, confidence, category, explanation, needs_review, judged_at)
		 VALUES (?, ?, ?, ?, ?, ?, 0.9, ?, ?, ?, ?)`,
		acc, _section, _model, seq, jv, m, cat, expl, r, judgedAt,
	)
	if err != nil {
		t.Fatalf("insert verdict: %v", err)
	}
}

func TestCompanyDisclosureChanges(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	const cik = "0000000123"

	insPeriodic(t, raw, "prior", cik, "2024-12-31")
	insBlock(t, raw, "prior", 0, "Customer concentration risk.")
	insBlock(t, raw, "prior", 1, "A risk we removed.")
	insPeriodic(t, raw, "current", cik, "2025-12-31")
	insBlock(t, raw, "current", 0, "Customer concentration risk.")
	insBlock(t, raw, "current", 1, "Going concern doubt.")

	insDiff(t, raw, "current", "prior")
	insChange(t, raw, "current", 0, "changed", 0, 0, "prior", 0.8)
	insChange(t, raw, "current", 1, "added", 1, nil, "prior", 0.3)
	insChange(t, raw, "current", 2, "dropped", nil, 1, "prior", 0.2)
	// change 0 material; change 1 material + needs-review; change 2 NOT material (excluded)
	insVerdict(t, raw, "current", 0, "jv1", true, "customer concentration", "Customer dependency worsened.", false, "t1")
	insVerdict(t, raw, "current", 1, "jv1", true, "going-concern", "New going-concern risk.", true, "t1")
	insVerdict(t, raw, "current", 2, "jv1", false, "reworded", "Immaterial removal.", false, "t1")
	_ = raw.Close()

	s := openStore(t, dbPath)
	groups, err := s.CompanyDisclosureChanges(context.Background(), cik, 40)
	if err != nil {
		t.Fatalf("CompanyDisclosureChanges: %v", err)
	}
	if len(groups) != 1 {
		t.Fatalf("groups = %d, want 1", len(groups))
	}
	g := groups[0]
	if g.CurrentPeriod != "2025-12-31" || g.PriorPeriod != "2024-12-31" {
		t.Errorf("periods = %q vs %q, want 2025-12-31 vs 2024-12-31", g.CurrentPeriod, g.PriorPeriod)
	}
	if len(g.Changes) != 2 { // the immaterial dropped change is excluded
		t.Fatalf("changes = %d, want 2", len(g.Changes))
	}
	c0 := g.Changes[0]
	if c0.ChangeType != "changed" || c0.Category != "customer concentration" || c0.Heading != "Customer concentration risk." {
		t.Errorf("change 0 = %+v", c0)
	}
	c1 := g.Changes[1]
	if c1.ChangeType != "added" || !c1.NeedsReview {
		t.Errorf("change 1 = %+v, want added + needs-review", c1)
	}
	if c1.Heading != "Going concern doubt." {
		t.Errorf("change 1 heading = %q", c1.Heading)
	}
}

func TestCompanyDisclosureChangesUsesLatestVerdict(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	const cik = "0000000123"
	insPeriodic(t, raw, "prior", cik, "2024-12-31")
	insBlock(t, raw, "prior", 0, "A risk.")
	insPeriodic(t, raw, "current", cik, "2025-12-31")
	insBlock(t, raw, "current", 0, "A risk, reworded.")
	insDiff(t, raw, "current", "prior")
	insChange(t, raw, "current", 0, "changed", 0, 0, "prior", 0.8)
	// An older verdict said material; a re-judge (later judged_at) says not — the
	// re-judge wins, so nothing is surfaced.
	insVerdict(t, raw, "current", 0, "jv1", true, "x", "old verdict", false, "2026-01-01")
	insVerdict(t, raw, "current", 0, "jv2", false, "reworded", "supersedes", false, "2026-06-01")
	_ = raw.Close()

	s := openStore(t, dbPath)
	groups, err := s.CompanyDisclosureChanges(context.Background(), cik, 40)
	if err != nil {
		t.Fatalf("CompanyDisclosureChanges: %v", err)
	}
	if len(groups) != 0 {
		t.Errorf("groups = %d, want 0 (latest verdict is immaterial)", len(groups))
	}
}
