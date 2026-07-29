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
	// Two distinct categories -> two single-change themes (immaterial dropped excluded).
	total := 0
	for _, th := range g.Themes {
		total += len(th.Changes)
	}
	if total != 2 {
		t.Fatalf("changes = %d, want 2", total)
	}
	if len(g.Themes) != 2 {
		t.Fatalf("themes = %d, want 2", len(g.Themes))
	}
	// Confidence ties, so themes fall in change_seq order: concentration (0) then going-concern (1).
	t0 := g.Themes[0]
	c0 := t0.Changes[0]
	if t0.Category != "customer concentration" || c0.ChangeType != "changed" ||
		c0.Heading != "Customer concentration risk." {
		t.Errorf("theme 0 = %+v", t0)
	}
	t1 := g.Themes[1]
	c1 := t1.Changes[0]
	if t1.Category != "going-concern" || c1.ChangeType != "added" || !c1.NeedsReview {
		t.Errorf("theme 1 = %+v, want going-concern added + needs-review", t1)
	}
	if c1.Heading != "Going concern doubt." {
		t.Errorf("change 1 heading = %q", c1.Heading)
	}
	if g.HasSynthesis {
		t.Errorf("HasSynthesis = true, want false (no synthesis row inserted)")
	}
}

func insSynthesis(
	t *testing.T, db *sql.DB, acc, direction, intensity string,
	material, worse, eased int, thesis, topEffectsJSON, synthesizedAt string,
) {
	t.Helper()
	_, err := db.Exec(
		`INSERT INTO filing_change_synthesis
			(accession_number, section, model_id, judge_version, synthesis_version,
			 headline_direction, headline_intensity, material_count, worse_count,
			 eased_count, neutral_count, thesis, top_effects, synthesized_at)
		 VALUES (?, ?, ?, 'jv1', 'sv1', ?, ?, ?, ?, ?, 0, ?, ?, ?)`,
		acc, _section, _model, direction, intensity, material, worse, eased,
		thesis, topEffectsJSON, synthesizedAt,
	)
	if err != nil {
		t.Fatalf("insert synthesis: %v", err)
	}
}

func TestCompanyDisclosureChangesAttachesSynthesis(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	const cik = "0000000123"
	insPeriodic(t, raw, "prior", cik, "2024-12-31")
	insBlock(t, raw, "prior", 0, "Debt risk.")
	insBlock(t, raw, "prior", 1, "Liquidity risk.")
	insPeriodic(t, raw, "current", cik, "2025-12-31")
	insBlock(t, raw, "current", 0, "Debt risk, worse.")
	insBlock(t, raw, "current", 1, "Liquidity risk, worse.")
	insDiff(t, raw, "current", "prior")
	insChange(t, raw, "current", 0, "changed", 0, 0, "prior", 0.8)
	insChange(t, raw, "current", 1, "changed", 1, 1, "prior", 0.8)
	// Two material changes in the SAME category -> one theme with two changes.
	insVerdict(t, raw, "current", 0, "jv1", true, "debt_capital_structure", "Leverage rose.", false, "t1")
	insVerdict(t, raw, "current", 1, "jv1", true, "debt_capital_structure", "Covenant tightened.", false, "t1")
	insSynthesis(
		t, raw, "current", "worsening", "major", 2, 2, 0,
		"Debt profile deteriorated sharply.", `["Leverage rose", "Covenant tightened"]`, "t2",
	)
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
	if !g.HasSynthesis {
		t.Fatal("HasSynthesis = false, want true")
	}
	if g.HeadlineDirection != "worsening" || g.HeadlineIntensity != "major" {
		t.Errorf("headline = %q %q, want major worsening", g.HeadlineIntensity, g.HeadlineDirection)
	}
	if g.MaterialCount != 2 || g.WorseCount != 2 || g.EasedCount != 0 {
		t.Errorf("counts = m%d w%d e%d, want 2/2/0", g.MaterialCount, g.WorseCount, g.EasedCount)
	}
	if g.Thesis != "Debt profile deteriorated sharply." {
		t.Errorf("thesis = %q", g.Thesis)
	}
	if len(g.TopEffects) != 2 || g.TopEffects[0] != "Leverage rose" {
		t.Errorf("top effects = %v", g.TopEffects)
	}
	if len(g.Themes) != 1 || len(g.Themes[0].Changes) != 2 {
		t.Fatalf("themes = %+v, want 1 theme with 2 changes", g.Themes)
	}
	if g.Themes[0].Category != "debt_capital_structure" {
		t.Errorf("theme category = %q", g.Themes[0].Category)
	}
}

func radarExec(t *testing.T, db *sql.DB, q string, args ...any) {
	t.Helper()
	if _, err := db.Exec(q, args...); err != nil {
		t.Fatalf("exec: %v", err)
	}
}

func TestRecentDisclosureChanges(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	const periodicCols = `(accession_number, cik, company_name, form, filed_at,
		period_of_report, fiscal_year, parsed, block_count, ingested_at)`
	// Alpha filed later than Beta, and is in cik_tickers (canonical name + ticker).
	radarExec(t, raw, `INSERT INTO periodic_filings `+periodicCols+`
		VALUES ('acc-a','0000000111','Alpha Corp','10-K','2026-03-01','2025-12-31',2025,1,0,'t')`)
	radarExec(t, raw, `INSERT INTO periodic_filings `+periodicCols+`
		VALUES ('acc-b','0000000222','Beta Inc','10-K','2026-02-01','2025-12-31',2025,1,0,'t')`)
	radarExec(t, raw, `INSERT INTO cik_tickers (cik, ticker, company_name, updated_at)
		VALUES ('0000000111','ALPH','Alpha Corporation','t')`)
	insSynthesis(t, raw, "acc-a", "worsening", "major", 44, 35, 9, "Alpha thesis.", `["x"]`, "t1")
	insSynthesis(t, raw, "acc-b", "worsening", "minor", 3, 3, 0, "Beta thesis.", `["y"]`, "t1")
	_ = raw.Close()

	s := openStore(t, dbPath)
	ctx := context.Background()

	rows, total, err := s.RecentDisclosureChanges(ctx, "", 40, 0)
	if err != nil {
		t.Fatalf("RecentDisclosureChanges: %v", err)
	}
	if total != 2 || len(rows) != 2 {
		t.Fatalf("all: total=%d rows=%d, want 2/2", total, len(rows))
	}
	// Newest filed first: Alpha (03-01) before Beta (02-01).
	if rows[0].Accession != "acc-a" || rows[1].Accession != "acc-b" {
		t.Errorf("order = %q, %q, want acc-a, acc-b", rows[0].Accession, rows[1].Accession)
	}
	// Alpha's identity comes from cik_tickers (canonical name + ticker).
	if rows[0].Ticker != "ALPH" || rows[0].CompanyName != "Alpha Corporation" {
		t.Errorf("alpha identity = %q %q, want ALPH / Alpha Corporation", rows[0].Ticker, rows[0].CompanyName)
	}
	if rows[0].HeadlineIntensity != "major" || rows[0].MaterialCount != 44 || rows[0].WorseCount != 35 {
		t.Errorf("alpha headline = %+v", rows[0])
	}
	// Beta is absent from cik_tickers: empty ticker, as-filed name.
	if rows[1].Ticker != "" || rows[1].CompanyName != "Beta Inc" {
		t.Errorf("beta identity = %q %q, want empty / Beta Inc", rows[1].Ticker, rows[1].CompanyName)
	}

	// Intensity filter.
	maj, majTotal, err := s.RecentDisclosureChanges(ctx, "major", 40, 0)
	if err != nil {
		t.Fatalf("RecentDisclosureChanges(major): %v", err)
	}
	if majTotal != 1 || len(maj) != 1 || maj[0].Accession != "acc-a" {
		t.Errorf("major filter = total %d rows %d, want just acc-a", majTotal, len(maj))
	}

	// Filter-chip counts.
	counts, err := s.RiskRadarIntensityCounts(ctx)
	if err != nil {
		t.Fatalf("RiskRadarIntensityCounts: %v", err)
	}
	if counts.Total != 2 || counts.Major != 1 || counts.Minor != 1 || counts.Moderate != 0 {
		t.Errorf("counts = %+v, want total 2 / major 1 / minor 1 / moderate 0", counts)
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

func TestStableSynthesisRendersOnCompanyPageAndExcludedFromFeed(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	const cik = "0000000123"
	insPeriodic(t, raw, "prior", cik, "2024-12-31")
	insPeriodic(t, raw, "current", cik, "2025-12-31")
	insDiff(t, raw, "current", "prior") // diffed, but no material changes -> stable
	insSynthesis(t, raw, "current", "stable", "none", 0, 0, 0,
		"Standing risks concentrated in supply chain.", `["supply concentration","fx exposure"]`, "t1")
	_ = raw.Close()

	s := openStore(t, dbPath)

	// Company page: the stable filing surfaces as a themeless standing-risk group.
	groups, err := s.CompanyDisclosureChanges(context.Background(), cik, 40)
	if err != nil {
		t.Fatalf("CompanyDisclosureChanges: %v", err)
	}
	if len(groups) != 1 {
		t.Fatalf("groups = %d, want 1 (stable filing)", len(groups))
	}
	g := groups[0]
	if !g.HasSynthesis || g.HeadlineDirection != "stable" || g.HeadlineIntensity != "none" {
		t.Errorf("headline = %+v, want stable/none with synthesis", g)
	}
	if g.PriorPeriod != "2024-12-31" {
		t.Errorf("PriorPeriod = %q, want 2024-12-31 (from the diff)", g.PriorPeriod)
	}
	if len(g.Themes) != 0 {
		t.Errorf("themes = %d, want 0 (no material evidence)", len(g.Themes))
	}
	if len(g.TopEffects) != 2 || g.Thesis == "" {
		t.Errorf("standing risks = %+v / thesis %q", g.TopEffects, g.Thesis)
	}

	// Movement feed: a stable filing is "what did not move" — excluded from the feed.
	rows, total, err := s.RecentDisclosureChanges(context.Background(), "", 50, 0)
	if err != nil {
		t.Fatalf("RecentDisclosureChanges: %v", err)
	}
	if total != 0 || len(rows) != 0 {
		t.Errorf("feed total = %d / rows %d, want 0 (stable excluded)", total, len(rows))
	}
	counts, err := s.RiskRadarIntensityCounts(context.Background())
	if err != nil {
		t.Fatalf("RiskRadarIntensityCounts: %v", err)
	}
	if counts.Total != 0 {
		t.Errorf("counts.Total = %d, want 0 (stable excluded)", counts.Total)
	}
}
