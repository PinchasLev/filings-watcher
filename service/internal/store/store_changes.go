package store

import (
	"context"
	"database/sql"
	"fmt"
)

// DisclosureChange is one material year-over-year change to a company's risk
// factors, as judged by the LLM (ADR 0042). Heading is the risk factor's own
// headline (from the current block, or the prior block for a removed one).
type DisclosureChange struct {
	Heading     string
	ChangeType  string // "added" | "changed" | "dropped"
	Category    string
	Explanation string
	Confidence  float64
	NeedsReview bool
	Similarity  *float64
}

// DisclosureChangeGroup is the set of material changes from one filing, diffed
// against its prior comparable. CurrentPeriod / PriorPeriod are the fiscal period
// ends (period of report), surfaced so the fiscal years being compared are
// unambiguous rather than inferred from the filing date.
type DisclosureChangeGroup struct {
	Accession     string
	CurrentPeriod string
	PriorPeriod   string
	Changes       []DisclosureChange
}

// CompanyDisclosureChanges returns a company's material risk-factor changes,
// grouped by filing (newest fiscal period first), capped at `limit` changes.
//
// Only the most recent verdict per change is used, so a re-judge (a new prompt
// or model, hence a new judge_version) supersedes an older verdict rather than
// double-listing. Supplementary to the company page: callers may ignore its error
// and render the section's empty state.
func (s *store) CompanyDisclosureChanges(
	ctx context.Context, cik string, limit int,
) ([]DisclosureChangeGroup, error) {
	const q = `
		SELECT v.accession_number,
		       pf.period_of_report,
		       ppf.period_of_report,
		       bc.change_type,
		       bc.similarity,
		       COALESCE(cur.heading, pri.heading),
		       v.category, v.explanation, v.confidence, v.needs_review
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
		 WHERE pf.cik = ? AND v.is_material = 1
		   AND v.judged_at = (
		         SELECT MAX(v2.judged_at) FROM block_change_verdicts v2
		          WHERE v2.accession_number = v.accession_number AND v2.section = v.section
		            AND v2.model_id = v.model_id AND v2.change_seq = v.change_seq)
		 ORDER BY pf.period_of_report DESC, v.accession_number, v.change_seq
		 LIMIT ?
	`
	rows, err := s.db.QueryContext(ctx, q, cik, limit)
	if err != nil {
		return nil, fmt.Errorf("company disclosure changes: %w", err)
	}
	defer func() { _ = rows.Close() }()

	var groups []DisclosureChangeGroup
	byAccession := map[string]int{}
	for rows.Next() {
		var (
			acc, changeType, category, explanation string
			curPeriod, priorPeriod, heading        sql.NullString
			similarity                             sql.NullFloat64
			confidence                             float64
			needsReview                            int
		)
		if err := rows.Scan(
			&acc, &curPeriod, &priorPeriod, &changeType, &similarity,
			&heading, &category, &explanation, &confidence, &needsReview,
		); err != nil {
			return nil, fmt.Errorf("scan disclosure change: %w", err)
		}
		change := DisclosureChange{
			Heading:     heading.String,
			ChangeType:  changeType,
			Category:    category,
			Explanation: explanation,
			Confidence:  confidence,
			NeedsReview: needsReview != 0,
		}
		if similarity.Valid {
			change.Similarity = &similarity.Float64
		}
		idx, ok := byAccession[acc]
		if !ok {
			groups = append(groups, DisclosureChangeGroup{
				Accession:     acc,
				CurrentPeriod: curPeriod.String,
				PriorPeriod:   priorPeriod.String,
			})
			idx = len(groups) - 1
			byAccession[acc] = idx
		}
		groups[idx].Changes = append(groups[idx].Changes, change)
	}
	return groups, rows.Err()
}
