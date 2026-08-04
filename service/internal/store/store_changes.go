package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
)

// DisclosureChange is one material year-over-year change to a company's risk
// factors, as judged by the LLM (ADR 0042). Heading is the risk factor's own
// headline (from the current block, or the prior block for a removed one).
// Direction is the per-change risk shift (worse | eased | neutral). IsSpecific is
// the calibration classification (nil until classified): true = company-specific,
// false = common-mode boilerplate; MatchedTheme names the catalog theme when
// common-mode.
type DisclosureChange struct {
	Heading      string
	Excerpt      string // short, clean quote from the risk-factor block_text, shown under the analysis
	ChangeType   string // "added" | "changed" | "dropped"
	Direction    string // "worse" | "eased" | "neutral"
	Category     string
	Explanation  string
	Confidence   float64
	NeedsReview  bool
	Similarity   *float64
	IsSpecific   *bool
	MatchedTheme string
	// Materialization (Phase 2b): the declared -> materialized track update, set when a
	// subsequent 8-K/6-K directly realized this risk.
	Realized            bool
	RealizingEventType  string
	RealizingDate       string
	RealizingAccession  string
	RealizationEvidence string
}

// excerptFromBlock renders a short, clean quote from a risk-factor block for display
// beneath the analysis. It collapses runs of whitespace and trims to ~200 chars on a
// word boundary, appending an ellipsis when truncated.
func excerptFromBlock(s string) string {
	s = strings.Join(strings.Fields(s), " ")
	const max = 200
	if len(s) <= max {
		return s
	}
	cut := s[:max]
	if i := strings.LastIndex(cut, " "); i > 0 {
		cut = cut[:i]
	}
	return strings.TrimRight(cut, " ,;:") + "…"
}

// ThemeCount summarizes how many of a filing's common-mode changes matched a given
// catalog theme, for the collapsed "also disclosed" drill-down.
type ThemeCount struct {
	Theme string
	Count int
}

// DisclosureChangeGroup is the read for one filing's material Risk Factor changes,
// diffed against its prior comparable. The changes are bucketed by the calibration
// classification so the page leads with what matters: SpecificChanges (company-specific,
// highlighted), EasedChanges (eased or removed — the good news), and CommonModeChanges
// (common-mode boilerplate, collapsed; CommonModeThemes is its per-theme tally).
// Unclassified holds changes not yet scored (before the calibration pipeline has run),
// rendered as a plain list so nothing is hidden.
//
// HeadlineDirection is retained only as the internal "stable" marker (a filing diffed
// with zero material changes) — it is no longer a user-facing tone. HasSynthesis is false
// when the reduce has not run; the page then shows the changes without the thesis.
// CurrentPeriod / PriorPeriod are the fiscal period ends, so the years compared are clear.
type DisclosureChangeGroup struct {
	Accession     string
	CurrentPeriod string
	PriorPeriod   string

	HasSynthesis      bool
	HeadlineDirection string // internal marker only: "stable" = diffed, zero material changes
	Thesis            string
	TopEffects        []string

	SpecificChanges   []DisclosureChange
	EasedChanges      []DisclosureChange
	CommonModeChanges []DisclosureChange
	CommonModeThemes  []ThemeCount
	Unclassified      []DisclosureChange
}

// RiskRadarRow is one filing's line in the cross-company feed: the company, how many
// company-specific changes it made this year, and the top few captions so the feed reads
// as "what specifically moved" rather than a list of names. Ticker is empty when the
// issuer is absent from SEC's ticker file.
type RiskRadarRow struct {
	CIK           string
	CompanyName   string
	Ticker        string
	Accession     string
	CurrentPeriod string
	FiledAt       string
	SpecificCount int
	Thesis        string
	TopSpecific   []string
}

// latestSynthesisPredicate keeps only the newest synthesis per (filing, section),
// so a re-synthesis (new prompt/model) supersedes rather than double-listing.
const latestSynthesisPredicate = `
	s.synthesized_at = (
		SELECT MAX(s2.synthesized_at) FROM filing_change_synthesis s2
		 WHERE s2.accession_number = s.accession_number AND s2.section = s.section)`

// A filing belongs in the feed when it has at least one company-specific change, OR no
// specificity has been computed for it yet (a graceful fallback before the calibration
// pipeline runs, so the feed is never empty on a fresh deploy). Common-mode-only filings
// drop out once classified — they surfaced nothing company-specific.
const feedPredicate = `
	s.headline_direction <> 'stable'
	AND (
	  EXISTS (
	    SELECT 1 FROM change_specificity cs
	     WHERE cs.accession_number = s.accession_number AND cs.section = s.section
	       AND cs.is_specific = 1
	       AND cs.classified_at = (
	             SELECT MAX(cs2.classified_at) FROM change_specificity cs2
	              WHERE cs2.accession_number = cs.accession_number AND cs2.section = cs.section
	                AND cs2.model_id = cs.model_id AND cs2.change_seq = cs.change_seq))
	  OR NOT EXISTS (
	    SELECT 1 FROM change_specificity cs
	     WHERE cs.accession_number = s.accession_number AND cs.section = s.section)
	)`

// specificCountExpr counts a filing's latest-classified company-specific changes.
const specificCountExpr = `
	(SELECT COUNT(*) FROM change_specificity cs
	  WHERE cs.accession_number = s.accession_number AND cs.section = s.section
	    AND cs.is_specific = 1
	    AND cs.classified_at = (
	          SELECT MAX(cs2.classified_at) FROM change_specificity cs2
	           WHERE cs2.accession_number = cs.accession_number AND cs2.section = cs.section
	             AND cs2.model_id = cs.model_id AND cs2.change_seq = cs.change_seq))`

// RecentDisclosureChanges returns the cross-company feed of filings whose Risk Factors
// surfaced company-specific changes year over year, newest filing first. Returns the page
// and the total.
func (s *store) RecentDisclosureChanges(
	ctx context.Context, limit, offset int,
) ([]RiskRadarRow, int, error) {
	var total int
	countQ := `SELECT COUNT(*) FROM filing_change_synthesis s
		 WHERE ` + latestSynthesisPredicate + ` AND ` + feedPredicate
	if err := s.db.QueryRowContext(ctx, countQ).Scan(&total); err != nil {
		return nil, 0, fmt.Errorf("risk radar count: %w", err)
	}

	q := `
		SELECT pf.cik,
		       COALESCE(ct.company_name, pf.company_name),
		       COALESCE(ct.ticker, ''),
		       s.accession_number, pf.period_of_report, pf.filed_at,
		       ` + specificCountExpr + `, s.thesis
		  FROM filing_change_synthesis s
		  JOIN periodic_filings pf ON pf.accession_number = s.accession_number
		  LEFT JOIN cik_tickers ct ON ct.cik = pf.cik
		 WHERE ` + latestSynthesisPredicate + ` AND ` + feedPredicate + `
		 ORDER BY pf.filed_at DESC, s.accession_number
		 LIMIT ? OFFSET ?`
	rows, err := s.db.QueryContext(ctx, q, limit, offset)
	if err != nil {
		return nil, 0, fmt.Errorf("risk radar: %w", err)
	}
	defer func() { _ = rows.Close() }()

	var out []RiskRadarRow
	for rows.Next() {
		var r RiskRadarRow
		if err := rows.Scan(
			&r.CIK, &r.CompanyName, &r.Ticker, &r.Accession, &r.CurrentPeriod, &r.FiledAt,
			&r.SpecificCount, &r.Thesis,
		); err != nil {
			return nil, 0, fmt.Errorf("scan risk radar row: %w", err)
		}
		out = append(out, r)
	}
	if err := rows.Err(); err != nil {
		return nil, 0, err
	}
	if err := s.attachTopSpecific(ctx, out); err != nil {
		return nil, 0, err
	}
	return out, total, nil
}

// attachTopSpecific fills each feed row with the captions of its top few company-specific
// changes (highest-confidence first), so the feed reads as "what specifically moved".
func (s *store) attachTopSpecific(ctx context.Context, rows []RiskRadarRow) error {
	if len(rows) == 0 {
		return nil
	}
	const maxCaptions = 2
	idx := make(map[string]*RiskRadarRow, len(rows))
	args := make([]any, 0, len(rows))
	ph := make([]string, 0, len(rows))
	for i := range rows {
		idx[rows[i].Accession] = &rows[i]
		args = append(args, rows[i].Accession)
		ph = append(ph, "?")
	}
	q := `
		SELECT v.accession_number, v.explanation
		  FROM block_change_verdicts v
		  JOIN change_specificity cs
		    ON cs.accession_number = v.accession_number AND cs.section = v.section
		   AND cs.model_id = v.model_id AND cs.change_seq = v.change_seq
		 WHERE v.is_material = 1 AND cs.is_specific = 1
		   AND v.accession_number IN (` + strings.Join(ph, ",") + `)
		   AND v.judged_at = (
		         SELECT MAX(v2.judged_at) FROM block_change_verdicts v2
		          WHERE v2.accession_number = v.accession_number AND v2.section = v.section
		            AND v2.model_id = v.model_id AND v2.change_seq = v.change_seq)
		   AND cs.classified_at = (
		         SELECT MAX(cs2.classified_at) FROM change_specificity cs2
		          WHERE cs2.accession_number = cs.accession_number AND cs2.section = cs.section
		            AND cs2.model_id = cs.model_id AND cs2.change_seq = cs.change_seq)
		 ORDER BY v.accession_number, v.confidence DESC, v.change_seq`
	res, err := s.db.QueryContext(ctx, q, args...)
	if err != nil {
		return fmt.Errorf("risk radar top-specific: %w", err)
	}
	defer func() { _ = res.Close() }()
	for res.Next() {
		var acc, explanation string
		if err := res.Scan(&acc, &explanation); err != nil {
			return fmt.Errorf("scan top-specific: %w", err)
		}
		if r, ok := idx[acc]; ok && len(r.TopSpecific) < maxCaptions {
			r.TopSpecific = append(r.TopSpecific, explanation)
		}
	}
	return res.Err()
}

// CompanyDisclosureChanges returns a company's material Risk Factor changes, grouped by
// filing (newest fiscal period first), each filing carrying its synthesis thesis and its
// changes bucketed by specificity. Stable filings (diffed, zero material changes) are
// appended as standing-risk groups. Supplementary to the company page: callers may ignore
// its error and render the section's empty state.
func (s *store) CompanyDisclosureChanges(
	ctx context.Context, cik string, limit int,
) ([]DisclosureChangeGroup, error) {
	ptrs, byAccession, err := s.disclosureChangeEvidence(ctx, cik, limit)
	if err != nil {
		return nil, err
	}
	if err := s.attachDisclosureSynthesis(ctx, cik, ptrs, byAccession); err != nil {
		return nil, err
	}
	stable, err := s.companyStableSyntheses(ctx, cik, byAccession)
	if err != nil {
		return nil, err
	}
	ptrs = append(ptrs, stable...)
	// Newest fiscal period first across changed and stable alike (ISO period ends sort
	// lexically); StableSort keeps the evidence query's within-period ordering.
	sort.SliceStable(ptrs, func(i, j int) bool {
		return ptrs[i].CurrentPeriod > ptrs[j].CurrentPeriod
	})
	out := make([]DisclosureChangeGroup, len(ptrs))
	for i, p := range ptrs {
		out[i] = *p
	}
	return out, nil
}

// companyStableSyntheses loads a company's stable-filing standing-risk summaries as
// themeless groups — one per filing diffed against its prior year with zero material
// changes. Skips any accession already present as an evidence group.
func (s *store) companyStableSyntheses(
	ctx context.Context, cik string, byAccession map[string]*DisclosureChangeGroup,
) ([]*DisclosureChangeGroup, error) {
	const q = `
		SELECT s.accession_number, pf.period_of_report, ppf.period_of_report,
		       s.thesis, s.top_effects
		  FROM filing_change_synthesis s
		  JOIN periodic_filings pf ON pf.accession_number = s.accession_number
		  LEFT JOIN filing_diffs d
		    ON d.accession_number = s.accession_number AND d.section = s.section
		   AND d.model_id = s.model_id
		  LEFT JOIN periodic_filings ppf ON ppf.accession_number = d.prior_accession_number
		 WHERE pf.cik = ? AND s.headline_direction = 'stable'
		   AND ` + latestSynthesisPredicate
	rows, err := s.db.QueryContext(ctx, q, cik)
	if err != nil {
		return nil, fmt.Errorf("company stable synthesis: %w", err)
	}
	defer func() { _ = rows.Close() }()

	var out []*DisclosureChangeGroup
	for rows.Next() {
		var (
			acc, thesis, topEffectsJSON string
			curPeriod, priorPeriod      sql.NullString
		)
		if err := rows.Scan(&acc, &curPeriod, &priorPeriod, &thesis, &topEffectsJSON); err != nil {
			return nil, fmt.Errorf("scan stable synthesis: %w", err)
		}
		if _, ok := byAccession[acc]; ok {
			continue
		}
		var topEffects []string
		if topEffectsJSON != "" {
			_ = json.Unmarshal([]byte(topEffectsJSON), &topEffects)
		}
		out = append(out, &DisclosureChangeGroup{
			Accession:         acc,
			CurrentPeriod:     curPeriod.String,
			PriorPeriod:       priorPeriod.String,
			HasSynthesis:      true,
			HeadlineDirection: "stable",
			Thesis:            thesis,
			TopEffects:        topEffects,
		})
	}
	return out, rows.Err()
}

// disclosureChangeEvidence loads the material changes, grouped by filing and bucketed by
// specificity (company-specific / eased-or-removed / common-mode / unclassified). A change
// is joined to its latest specificity classification; an unclassified change (nil) falls
// into Unclassified so it still renders before the calibration pipeline has run.
func (s *store) disclosureChangeEvidence(
	ctx context.Context, cik string, limit int,
) ([]*DisclosureChangeGroup, map[string]*DisclosureChangeGroup, error) {
	const q = `
		SELECT v.accession_number,
		       pf.period_of_report,
		       ppf.period_of_report,
		       bc.change_type,
		       v.direction,
		       bc.similarity,
		       COALESCE(cur.heading, pri.heading),
		       v.category, v.explanation, v.confidence, v.needs_review,
		       cs.is_specific, cs.matched_theme,
		       rr.is_realized, rr.realizing_accession, rr.realizing_event_type,
		       rf.filing_date, rr.evidence,
		       COALESCE(cur.block_text, pri.block_text)
		  FROM block_change_verdicts v
		  JOIN filing_diffs d
		    ON d.accession_number = v.accession_number AND d.section = v.section
		   AND d.model_id = v.model_id
		  JOIN periodic_filings pf ON pf.accession_number = v.accession_number
		  LEFT JOIN periodic_filings ppf ON ppf.accession_number = d.prior_accession_number
		  JOIN block_changes bc
		    ON bc.accession_number = v.accession_number AND bc.section = v.section
		   AND bc.model_id = v.model_id AND bc.change_seq = v.change_seq
		  LEFT JOIN filing_blocks cur
		    ON cur.accession_number = v.accession_number AND cur.section = v.section
		   AND cur.block_index = bc.current_block_index
		  LEFT JOIN filing_blocks pri
		    ON pri.accession_number = d.prior_accession_number AND pri.section = v.section
		   AND pri.block_index = bc.prior_block_index
		  LEFT JOIN change_specificity cs
		    ON cs.accession_number = v.accession_number AND cs.section = v.section
		   AND cs.model_id = v.model_id AND cs.change_seq = v.change_seq
		   AND cs.classified_at = (
		         SELECT MAX(cs2.classified_at) FROM change_specificity cs2
		          WHERE cs2.accession_number = cs.accession_number AND cs2.section = cs.section
		            AND cs2.model_id = cs.model_id AND cs2.change_seq = cs.change_seq)
		  LEFT JOIN risk_realizations rr
		    ON rr.accession_number = v.accession_number AND rr.section = v.section
		   AND rr.model_id = v.model_id AND rr.change_seq = v.change_seq
		   AND rr.judged_at = (
		         SELECT MAX(rr2.judged_at) FROM risk_realizations rr2
		          WHERE rr2.accession_number = rr.accession_number AND rr2.section = rr.section
		            AND rr2.model_id = rr.model_id AND rr2.change_seq = rr.change_seq)
		  LEFT JOIN filings rf ON rf.accession_number = rr.realizing_accession
		 WHERE pf.cik = ? AND v.is_material = 1
		   AND v.judged_at = (
		         SELECT MAX(v2.judged_at) FROM block_change_verdicts v2
		          WHERE v2.accession_number = v.accession_number AND v2.section = v.section
		            AND v2.model_id = v.model_id AND v2.change_seq = v.change_seq)
		 ORDER BY pf.period_of_report DESC, v.accession_number, v.confidence DESC, v.change_seq
		 LIMIT ?
	`
	rows, err := s.db.QueryContext(ctx, q, cik, limit)
	if err != nil {
		return nil, nil, fmt.Errorf("company disclosure changes: %w", err)
	}
	defer func() { _ = rows.Close() }()

	var groups []*DisclosureChangeGroup
	byAccession := map[string]*DisclosureChangeGroup{}
	for rows.Next() {
		var (
			acc, changeType, direction, category, explanation                string
			curPeriod, priorPeriod, heading, matchedTheme                    sql.NullString
			similarity                                                       sql.NullFloat64
			confidence                                                       float64
			needsReview                                                      int
			isSpecific, isRealized                                           sql.NullInt64
			realizingAcc, realizingEvent, realizingDate, realizationEvidence sql.NullString
			blockText                                                        sql.NullString
		)
		if err := rows.Scan(
			&acc, &curPeriod, &priorPeriod, &changeType, &direction, &similarity,
			&heading, &category, &explanation, &confidence, &needsReview,
			&isSpecific, &matchedTheme,
			&isRealized, &realizingAcc, &realizingEvent, &realizingDate, &realizationEvidence,
			&blockText,
		); err != nil {
			return nil, nil, fmt.Errorf("scan disclosure change: %w", err)
		}
		change := DisclosureChange{
			Heading:      heading.String,
			Excerpt:      excerptFromBlock(blockText.String),
			ChangeType:   changeType,
			Direction:    direction,
			Category:     category,
			Explanation:  explanation,
			Confidence:   confidence,
			NeedsReview:  needsReview != 0,
			MatchedTheme: matchedTheme.String,
		}
		if similarity.Valid {
			change.Similarity = &similarity.Float64
		}
		if isSpecific.Valid {
			b := isSpecific.Int64 != 0
			change.IsSpecific = &b
		}
		if isRealized.Valid && isRealized.Int64 != 0 {
			change.Realized = true
			change.RealizingAccession = realizingAcc.String
			change.RealizingEventType = realizingEvent.String
			change.RealizingDate = realizingDate.String
			change.RealizationEvidence = realizationEvidence.String
		}
		group, ok := byAccession[acc]
		if !ok {
			group = &DisclosureChangeGroup{
				Accession:     acc,
				CurrentPeriod: curPeriod.String,
				PriorPeriod:   priorPeriod.String,
			}
			groups = append(groups, group)
			byAccession[acc] = group
		}
		switch {
		case change.IsSpecific == nil:
			group.Unclassified = append(group.Unclassified, change)
		case *change.IsSpecific && (change.Direction == "eased" || change.ChangeType == "dropped"):
			group.EasedChanges = append(group.EasedChanges, change)
		case *change.IsSpecific:
			group.SpecificChanges = append(group.SpecificChanges, change)
		default:
			group.CommonModeChanges = append(group.CommonModeChanges, change)
		}
	}
	if err := rows.Err(); err != nil {
		return nil, nil, err
	}
	for _, g := range groups {
		g.CommonModeThemes = summarizeThemes(g.CommonModeChanges)
	}
	return groups, byAccession, nil
}

// summarizeThemes tallies common-mode changes by matched catalog theme, most-common first,
// for the collapsed "also disclosed" drill-down.
func summarizeThemes(changes []DisclosureChange) []ThemeCount {
	if len(changes) == 0 {
		return nil
	}
	counts := map[string]int{}
	order := []string{}
	for _, c := range changes {
		theme := c.MatchedTheme
		if theme == "" {
			theme = "other"
		}
		if _, seen := counts[theme]; !seen {
			order = append(order, theme)
		}
		counts[theme]++
	}
	out := make([]ThemeCount, 0, len(order))
	for _, theme := range order {
		out = append(out, ThemeCount{Theme: theme, Count: counts[theme]})
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].Count > out[j].Count })
	return out
}

// attachDisclosureSynthesis fills each group's thesis/standing-risk from the latest
// synthesis for that filing. A missing synthesis leaves HasSynthesis false.
func (s *store) attachDisclosureSynthesis(
	ctx context.Context, cik string, groups []*DisclosureChangeGroup,
	byAccession map[string]*DisclosureChangeGroup,
) error {
	if len(groups) == 0 {
		return nil
	}
	const q = `
		SELECT s.accession_number, s.headline_direction, s.thesis, s.top_effects
		  FROM filing_change_synthesis s
		  JOIN periodic_filings pf ON pf.accession_number = s.accession_number
		 WHERE pf.cik = ?
		   AND s.synthesized_at = (
		         SELECT MAX(s2.synthesized_at) FROM filing_change_synthesis s2
		          WHERE s2.accession_number = s.accession_number AND s2.section = s.section)
	`
	rows, err := s.db.QueryContext(ctx, q, cik)
	if err != nil {
		return fmt.Errorf("company disclosure synthesis: %w", err)
	}
	defer func() { _ = rows.Close() }()

	for rows.Next() {
		var acc, direction, thesis, topEffectsJSON string
		if err := rows.Scan(&acc, &direction, &thesis, &topEffectsJSON); err != nil {
			return fmt.Errorf("scan disclosure synthesis: %w", err)
		}
		g, ok := byAccession[acc]
		if !ok {
			continue // synthesis for a filing whose changes fell outside the limit
		}
		var topEffects []string
		if topEffectsJSON != "" {
			_ = json.Unmarshal([]byte(topEffectsJSON), &topEffects)
		}
		g.HasSynthesis = true
		g.HeadlineDirection = direction
		g.Thesis = thesis
		g.TopEffects = topEffects
	}
	return rows.Err()
}
