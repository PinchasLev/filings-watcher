package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"sort"
)

// DisclosureChange is one material year-over-year change to a company's risk
// factors, as judged by the LLM (ADR 0042). Heading is the risk factor's own
// headline (from the current block, or the prior block for a removed one).
// Direction is the per-change risk shift (worse | eased | neutral).
type DisclosureChange struct {
	Heading     string
	ChangeType  string // "added" | "changed" | "dropped"
	Direction   string // "worse" | "eased" | "neutral"
	Category    string
	Explanation string
	Confidence  float64
	NeedsReview bool
	Similarity  *float64
}

// DisclosureTheme groups a filing's changes under one governed category, so the
// drilldown reads by business theme (Liquidity, Restructuring, …) rather than as a
// flat list. Changes within a theme are ordered most-confident first.
type DisclosureTheme struct {
	Category string
	Changes  []DisclosureChange
}

// DisclosureChangeGroup is the read for one filing's material changes, diffed
// against its prior comparable — an inverted pyramid (ADR 0043): a headline
// (direction + intensity + counts), the synthesis thesis and top effects, and the
// evidence grouped by theme. CurrentPeriod / PriorPeriod are the fiscal period ends
// (period of report), so the years compared are unambiguous.
//
// HasSynthesis is false when the reduce has not run for this filing yet; the page
// then shows the themed evidence without the headline/thesis rather than nothing.
type DisclosureChangeGroup struct {
	Accession     string
	CurrentPeriod string
	PriorPeriod   string

	HasSynthesis      bool
	HeadlineDirection string // "worsening" | "easing" | "mixed"
	HeadlineIntensity string // "major" | "moderate" | "minor"
	MaterialCount     int
	WorseCount        int
	EasedCount        int
	Thesis            string
	TopEffects        []string

	Themes []DisclosureTheme
}

// CompanyDisclosureChanges returns a company's material risk-factor changes,
// grouped by filing (newest fiscal period first), capped at `limit` changes, each
// filing carrying its synthesis headline/thesis and its evidence grouped by theme.
//
// Only the most recent verdict per change is used, so a re-judge (a new prompt or
// model, hence a new judge_version) supersedes rather than double-listing; likewise
// the latest synthesis per filing wins. Supplementary to the company page: callers
// may ignore its error and render the section's empty state.
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
	out := make([]DisclosureChangeGroup, len(ptrs))
	for i, p := range ptrs {
		out[i] = *p
	}
	return out, nil
}

// disclosureChangeEvidence loads the material changes, grouped by filing and then by
// theme within each filing.
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
	// Per group, track theme index by category to preserve encounter order before sorting.
	themeIndex := map[string]map[string]int{}
	for rows.Next() {
		var (
			acc, changeType, direction, category, explanation string
			curPeriod, priorPeriod, heading                   sql.NullString
			similarity                                        sql.NullFloat64
			confidence                                        float64
			needsReview                                       int
		)
		if err := rows.Scan(
			&acc, &curPeriod, &priorPeriod, &changeType, &direction, &similarity,
			&heading, &category, &explanation, &confidence, &needsReview,
		); err != nil {
			return nil, nil, fmt.Errorf("scan disclosure change: %w", err)
		}
		change := DisclosureChange{
			Heading:     heading.String,
			ChangeType:  changeType,
			Direction:   direction,
			Category:    category,
			Explanation: explanation,
			Confidence:  confidence,
			NeedsReview: needsReview != 0,
		}
		if similarity.Valid {
			change.Similarity = &similarity.Float64
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
			themeIndex[acc] = map[string]int{}
		}
		ti := themeIndex[acc]
		idx, seen := ti[category]
		if !seen {
			group.Themes = append(group.Themes, DisclosureTheme{Category: category})
			idx = len(group.Themes) - 1
			ti[category] = idx
		}
		group.Themes[idx].Changes = append(group.Themes[idx].Changes, change)
	}
	if err := rows.Err(); err != nil {
		return nil, nil, err
	}
	// Biggest themes first — a reader scans the dominant clusters before the tail.
	for _, g := range groups {
		sort.SliceStable(g.Themes, func(i, j int) bool {
			return len(g.Themes[i].Changes) > len(g.Themes[j].Changes)
		})
	}
	return groups, byAccession, nil
}

// attachDisclosureSynthesis fills each group's headline/thesis from the latest
// synthesis for that filing. A missing synthesis leaves HasSynthesis false.
func (s *store) attachDisclosureSynthesis(
	ctx context.Context, cik string, groups []*DisclosureChangeGroup,
	byAccession map[string]*DisclosureChangeGroup,
) error {
	if len(groups) == 0 {
		return nil
	}
	const q = `
		SELECT s.accession_number, s.headline_direction, s.headline_intensity,
		       s.material_count, s.worse_count, s.eased_count, s.thesis, s.top_effects
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
		var (
			acc, direction, intensity, thesis string
			material, worse, eased            int
			topEffectsJSON                    string
		)
		if err := rows.Scan(
			&acc, &direction, &intensity, &material, &worse, &eased, &thesis, &topEffectsJSON,
		); err != nil {
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
		g.HeadlineIntensity = intensity
		g.MaterialCount = material
		g.WorseCount = worse
		g.EasedCount = eased
		g.Thesis = thesis
		g.TopEffects = topEffects
	}
	return rows.Err()
}
