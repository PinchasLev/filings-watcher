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

// insSpecificity records a change's calibration classification (PR2): specific vs
// common-mode, keyed on judge/catalog/specificity versions.
func insSpecificity(t *testing.T, db *sql.DB, acc string, seq int, specific bool, theme, classifiedAt string) {
	t.Helper()
	sp := 0
	if specific {
		sp = 1
	}
	_, err := db.Exec(
		`INSERT INTO change_specificity
			(accession_number, section, model_id, change_seq, judge_version,
			 catalog_version, specificity_version, is_specific, matched_theme, confidence, classified_at)
		 VALUES (?, ?, ?, ?, 'jv1', 'cv1', 'sv1', ?, ?, 0.9, ?)`,
		acc, _section, _model, seq, sp, theme, classifiedAt,
	)
	if err != nil {
		t.Fatalf("insert specificity: %v", err)
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

func radarExec(t *testing.T, db *sql.DB, q string, args ...any) {
	t.Helper()
	if _, err := db.Exec(q, args...); err != nil {
		t.Fatalf("exec: %v", err)
	}
}

// Before the calibration pipeline has run, changes have no specificity classification and
// fall into Unclassified so nothing disappears.
func TestCompanyDisclosureChangesFallbackWhenUnclassified(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	const cik = "0000000123"

	insPeriodic(t, raw, "prior", cik, "2024-12-31")
	insBlock(t, raw, "prior", 0, "Customer concentration risk.")
	insPeriodic(t, raw, "current", cik, "2025-12-31")
	insBlock(t, raw, "current", 0, "Customer concentration risk.")
	insBlock(t, raw, "current", 1, "Going concern doubt.")
	insDiff(t, raw, "current", "prior")
	insChange(t, raw, "current", 0, "changed", 0, 0, "prior", 0.8)
	insChange(t, raw, "current", 1, "added", 1, nil, "prior", 0.3)
	insChange(t, raw, "current", 2, "dropped", nil, 1, "prior", 0.2)
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
		t.Errorf("periods = %q vs %q", g.CurrentPeriod, g.PriorPeriod)
	}
	// Two material changes, unclassified -> both in the fallback bucket; the immaterial dropped is excluded.
	if len(g.Unclassified) != 2 {
		t.Fatalf("Unclassified = %d, want 2", len(g.Unclassified))
	}
	if n := len(g.SpecificChanges) + len(g.EasedChanges) + len(g.CommonModeChanges); n != 0 {
		t.Errorf("classified buckets = %d, want 0 (nothing classified)", n)
	}
	if g.Unclassified[1].ChangeType != "added" || !g.Unclassified[1].NeedsReview {
		t.Errorf("second change = %+v, want added + needs-review", g.Unclassified[1])
	}
}

// With specificity classified, changes bucket into company-specific, eased/removed, and
// common-mode (collapsed, tallied by theme).
func TestCompanyDisclosureChangesBuckets(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	const cik = "0000000123"

	insPeriodic(t, raw, "prior", cik, "2024-12-31")
	insBlock(t, raw, "prior", 1, "A lawsuit risk.")
	insPeriodic(t, raw, "current", cik, "2025-12-31")
	insBlock(t, raw, "current", 0, "Customer risk, worse.")
	insBlock(t, raw, "current", 2, "Tariff exposure.")
	insDiff(t, raw, "current", "prior")
	insChange(t, raw, "current", 0, "changed", 0, 0, "prior", 0.9)   // specific
	insChange(t, raw, "current", 1, "dropped", nil, 1, "prior", 0.5) // specific + removed -> eased
	insChange(t, raw, "current", 2, "added", 2, nil, "prior", 0.4)   // common-mode
	insVerdict(t, raw, "current", 0, "jv1", true, "customer concentration", "Lost a top customer.", false, "t1")
	insVerdict(t, raw, "current", 1, "jv1", true, "litigation", "Resolved and removed a lawsuit risk.", false, "t1")
	insVerdict(t, raw, "current", 2, "jv1", true, "macro_geopolitical", "Generic tariff exposure.", false, "t1")
	insSpecificity(t, raw, "current", 0, true, "", "t2")
	insSpecificity(t, raw, "current", 1, true, "", "t2")
	insSpecificity(t, raw, "current", 2, false, "tariffs_trade_policy", "t2")
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
	if len(g.SpecificChanges) != 1 || g.SpecificChanges[0].Explanation != "Lost a top customer." {
		t.Errorf("SpecificChanges = %+v", g.SpecificChanges)
	}
	if g.SpecificChanges[0].Excerpt != "body" {
		t.Errorf("SpecificChanges[0].Excerpt = %q, want the block_text quote", g.SpecificChanges[0].Excerpt)
	}
	if len(g.EasedChanges) != 1 || g.EasedChanges[0].ChangeType != "dropped" {
		t.Errorf("EasedChanges = %+v, want one dropped change", g.EasedChanges)
	}
	if len(g.CommonModeChanges) != 1 || g.CommonModeChanges[0].MatchedTheme != "tariffs_trade_policy" {
		t.Errorf("CommonModeChanges = %+v", g.CommonModeChanges)
	}
	if len(g.CommonModeThemes) != 1 || g.CommonModeThemes[0].Theme != "tariffs_trade_policy" || g.CommonModeThemes[0].Count != 1 {
		t.Errorf("CommonModeThemes = %+v", g.CommonModeThemes)
	}
	if len(g.Unclassified) != 0 {
		t.Errorf("Unclassified = %d, want 0", len(g.Unclassified))
	}
}

func TestCompanyDisclosureChangesAttachesSynthesis(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	const cik = "0000000123"
	insPeriodic(t, raw, "prior", cik, "2024-12-31")
	insBlock(t, raw, "prior", 0, "Debt risk.")
	insPeriodic(t, raw, "current", cik, "2025-12-31")
	insBlock(t, raw, "current", 0, "Debt risk, worse.")
	insDiff(t, raw, "current", "prior")
	insChange(t, raw, "current", 0, "changed", 0, 0, "prior", 0.8)
	insVerdict(t, raw, "current", 0, "jv1", true, "debt_capital_structure", "Leverage rose.", false, "t1")
	insSynthesis(
		t, raw, "current", "worsening", "major", 1, 1, 0,
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
	if g.Thesis != "Debt profile deteriorated sharply." {
		t.Errorf("thesis = %q", g.Thesis)
	}
	if len(g.TopEffects) != 2 || g.TopEffects[0] != "Leverage rose" {
		t.Errorf("top effects = %v", g.TopEffects)
	}
	if len(g.Unclassified) != 1 {
		t.Errorf("Unclassified = %d, want 1 (unclassified change under the thesis)", len(g.Unclassified))
	}
}

func TestRecentDisclosureChangesFallbackIncludesAll(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	const periodicCols = `(accession_number, cik, company_name, form, filed_at,
		period_of_report, fiscal_year, parsed, block_count, ingested_at)`
	// Alpha filed later than Beta, and is in cik_tickers (canonical name + ticker).
	if _, err := raw.Exec(`INSERT INTO periodic_filings ` + periodicCols + `
		VALUES ('acc-a','0000000111','Alpha Corp','10-K','2026-03-01','2025-12-31',2025,1,0,'t')`); err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec(`INSERT INTO periodic_filings ` + periodicCols + `
		VALUES ('acc-b','0000000222','Beta Inc','10-K','2026-02-01','2025-12-31',2025,1,0,'t')`); err != nil {
		t.Fatal(err)
	}
	if _, err := raw.Exec(`INSERT INTO cik_tickers (cik, ticker, company_name, updated_at)
		VALUES ('0000000111','ALPH','Alpha Corporation','t')`); err != nil {
		t.Fatal(err)
	}
	insSynthesis(t, raw, "acc-a", "worsening", "major", 44, 35, 9, "Alpha thesis.", `["x"]`, "t1")
	insSynthesis(t, raw, "acc-b", "worsening", "minor", 3, 3, 0, "Beta thesis.", `["y"]`, "t1")
	_ = raw.Close()

	s := openStore(t, dbPath)
	// No specificity computed yet -> both filings appear (graceful fallback).
	rows, total, err := s.RecentDisclosureChanges(context.Background(), 40, 0)
	if err != nil {
		t.Fatalf("RecentDisclosureChanges: %v", err)
	}
	if total != 2 || len(rows) != 2 {
		t.Fatalf("all: total=%d rows=%d, want 2/2", total, len(rows))
	}
	if rows[0].Accession != "acc-a" || rows[1].Accession != "acc-b" {
		t.Errorf("order = %q, %q, want acc-a, acc-b (newest filed first)", rows[0].Accession, rows[1].Accession)
	}
	if rows[0].Ticker != "ALPH" || rows[0].CompanyName != "Alpha Corporation" {
		t.Errorf("alpha identity = %q %q", rows[0].Ticker, rows[0].CompanyName)
	}
	if rows[0].SpecificCount != 0 {
		t.Errorf("alpha SpecificCount = %d, want 0 (unclassified)", rows[0].SpecificCount)
	}
	if rows[1].Ticker != "" || rows[1].CompanyName != "Beta Inc" {
		t.Errorf("beta identity = %q %q", rows[1].Ticker, rows[1].CompanyName)
	}
}

func TestRecentDisclosureChangesFiltersToSpecific(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	const cik1, cik2 = "0000000111", "0000000222"
	// "spec" filing has a company-specific change; "boiler" filing is common-mode only.
	insPeriodic(t, raw, "spec", cik1, "2025-12-31")
	insBlock(t, raw, "spec", 0, "Merger risk.")
	insDiff(t, raw, "spec", "spec-prior")
	insChange(t, raw, "spec", 0, "added", 0, nil, "spec-prior", 0.9)
	insVerdict(t, raw, "spec", 0, "jv1", true, "ma_activity", "Norfolk Southern merger announced.", false, "t1")
	insSpecificity(t, raw, "spec", 0, true, "", "t2")
	insSynthesis(t, raw, "spec", "worsening", "major", 1, 1, 0, "Spec thesis.", `["x"]`, "t1")

	insPeriodic(t, raw, "boiler", cik2, "2025-12-31")
	insBlock(t, raw, "boiler", 0, "Tariff risk.")
	insDiff(t, raw, "boiler", "boiler-prior")
	insChange(t, raw, "boiler", 0, "added", 0, nil, "boiler-prior", 0.5)
	insVerdict(t, raw, "boiler", 0, "jv1", true, "macro_geopolitical", "Generic tariff exposure.", false, "t1")
	insSpecificity(t, raw, "boiler", 0, false, "tariffs_trade_policy", "t2")
	insSynthesis(t, raw, "boiler", "worsening", "minor", 1, 1, 0, "Boiler thesis.", `["y"]`, "t1")
	_ = raw.Close()

	s := openStore(t, dbPath)
	rows, total, err := s.RecentDisclosureChanges(context.Background(), 40, 0)
	if err != nil {
		t.Fatalf("RecentDisclosureChanges: %v", err)
	}
	if total != 1 || len(rows) != 1 || rows[0].Accession != "spec" {
		t.Fatalf("feed = total %d rows %d, want just the specific filing", total, len(rows))
	}
	if rows[0].SpecificCount != 1 {
		t.Errorf("SpecificCount = %d, want 1", rows[0].SpecificCount)
	}
	if len(rows[0].TopSpecific) != 1 || rows[0].TopSpecific[0] != "Norfolk Southern merger announced." {
		t.Errorf("TopSpecific = %v", rows[0].TopSpecific)
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

	// Company page: the stable filing surfaces as a standing-risk group.
	groups, err := s.CompanyDisclosureChanges(context.Background(), cik, 40)
	if err != nil {
		t.Fatalf("CompanyDisclosureChanges: %v", err)
	}
	if len(groups) != 1 {
		t.Fatalf("groups = %d, want 1 (stable filing)", len(groups))
	}
	g := groups[0]
	if !g.HasSynthesis || g.HeadlineDirection != "stable" {
		t.Errorf("headline = %+v, want stable with synthesis", g)
	}
	if g.PriorPeriod != "2024-12-31" {
		t.Errorf("PriorPeriod = %q, want 2024-12-31 (from the diff)", g.PriorPeriod)
	}
	if n := len(g.SpecificChanges) + len(g.EasedChanges) + len(g.CommonModeChanges) + len(g.Unclassified); n != 0 {
		t.Errorf("change buckets = %d, want 0 (no material evidence)", n)
	}
	if len(g.TopEffects) != 2 || g.Thesis == "" {
		t.Errorf("standing risks = %+v / thesis %q", g.TopEffects, g.Thesis)
	}

	// Feed: a stable filing is "what did not move" — excluded.
	rows, total, err := s.RecentDisclosureChanges(context.Background(), 50, 0)
	if err != nil {
		t.Fatalf("RecentDisclosureChanges: %v", err)
	}
	if total != 0 || len(rows) != 0 {
		t.Errorf("feed total = %d / rows %d, want 0 (stable excluded)", total, len(rows))
	}
}

func insFiling(t *testing.T, db *sql.DB, acc, cik, form, filingDate string) {
	t.Helper()
	_, err := db.Exec(
		`INSERT INTO filings (accession_number,cik,ticker,company_name,form,filing_date,
			primary_document,primary_document_url,items_json,fetched_at)
		 VALUES (?,?,'ACME','ACME Corp',?,?,'d','u','[]','t')`,
		acc, cik, form, filingDate,
	)
	if err != nil {
		t.Fatalf("insert filing: %v", err)
	}
}

func insRealization(
	t *testing.T, db *sql.DB, acc string, seq int, rv string, realized bool,
	realizingAcc, eventType, evidence, judgedAt string,
) {
	t.Helper()
	r := 0
	if realized {
		r = 1
	}
	_, err := db.Exec(
		`INSERT INTO risk_realizations (accession_number,section,model_id,change_seq,
			judge_version,realization_version,is_realized,realizing_accession,realizing_event_type,
			realizing_item,evidence,confidence,checked_through,judged_at)
		 VALUES (?,?,?,?,'jv1',?,?,?,?,'1.01',?,0.9,'2026-07-01',?)`,
		acc, _section, _model, seq, rv, r, realizingAcc, eventType, evidence, judgedAt,
	)
	if err != nil {
		t.Fatalf("insert realization: %v", err)
	}
}

func TestCompanyDisclosureChangesSurfacesMaterialization(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	const cik = "0000000123"
	insPeriodic(t, raw, "prior", cik, "2024-12-31")
	insBlock(t, raw, "prior", 0, "A risk.")
	insPeriodic(t, raw, "current", cik, "2025-12-31")
	insBlock(t, raw, "current", 0, "Merger risk.")
	insDiff(t, raw, "current", "prior")
	insChange(t, raw, "current", 0, "changed", 0, 0, "prior", 0.9)
	insVerdict(t, raw, "current", 0, "jv1", true, "ma_activity", "Norfolk Southern merger risk.", false, "t1")
	insSpecificity(t, raw, "current", 0, true, "", "t2")
	// the realizing 8-K + the realization verdict
	insFiling(t, raw, "eightk", cik, "8-K", "2026-05-01")
	insRealization(t, raw, "current", 0, "rv1", true, "eightk", "ma_activity", "The ICE merger agreement realizes it.", "t3")
	_ = raw.Close()

	s := openStore(t, dbPath)
	groups, err := s.CompanyDisclosureChanges(context.Background(), cik, 40)
	if err != nil {
		t.Fatalf("CompanyDisclosureChanges: %v", err)
	}
	if len(groups) != 1 || len(groups[0].SpecificChanges) != 1 {
		t.Fatalf("groups/specific = %+v", groups)
	}
	c := groups[0].SpecificChanges[0]
	if !c.Realized {
		t.Fatal("Realized = false, want true")
	}
	if c.RealizingAccession != "eightk" || c.RealizingEventType != "ma_activity" || c.RealizingDate != "2026-05-01" {
		t.Errorf("realizing = %q %q %q", c.RealizingAccession, c.RealizingEventType, c.RealizingDate)
	}
	if c.RealizationEvidence != "The ICE merger agreement realizes it." {
		t.Errorf("evidence = %q", c.RealizationEvidence)
	}
}

func TestMaterializationUsesLatestVerdict(t *testing.T) {
	dbPath, raw := freshDBPath(t)
	const cik = "0000000123"
	insPeriodic(t, raw, "prior", cik, "2024-12-31")
	insBlock(t, raw, "prior", 0, "A risk.")
	insPeriodic(t, raw, "current", cik, "2025-12-31")
	insBlock(t, raw, "current", 0, "A risk.")
	insDiff(t, raw, "current", "prior")
	insChange(t, raw, "current", 0, "changed", 0, 0, "prior", 0.9)
	insVerdict(t, raw, "current", 0, "jv1", true, "ma_activity", "A specific risk.", false, "t1")
	insSpecificity(t, raw, "current", 0, true, "", "t2")
	insFiling(t, raw, "eightk", cik, "8-K", "2026-05-01")
	// an older realized verdict, then a newer re-check that says not realized — latest wins
	insRealization(t, raw, "current", 0, "rv-old", true, "eightk", "ma_activity", "old", "2026-06-01T00:00:00Z")
	insRealization(t, raw, "current", 0, "rv-new", false, "", "", "", "2026-07-15T00:00:00Z")
	_ = raw.Close()

	s := openStore(t, dbPath)
	groups, err := s.CompanyDisclosureChanges(context.Background(), cik, 40)
	if err != nil {
		t.Fatalf("CompanyDisclosureChanges: %v", err)
	}
	if len(groups) != 1 || len(groups[0].SpecificChanges) != 1 {
		t.Fatalf("groups/specific = %+v", groups)
	}
	if groups[0].SpecificChanges[0].Realized {
		t.Error("Realized = true, want false (latest verdict is not-realized)")
	}
}
