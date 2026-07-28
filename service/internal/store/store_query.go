package store

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"
)

// queryResultRowCap bounds a console result set so a broad query can't return the
// whole table into a web page.
const queryResultRowCap = 500

// writeStmtRe matches any statement keyword that could mutate the database. Used to
// reject non-read queries in the operator console (belt-and-suspenders alongside the
// SELECT/WITH prefix check — a WITH-clause can legally precede a DELETE in SQLite).
var writeStmtRe = regexp.MustCompile(
	`\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|ATTACH|DETACH|PRAGMA|VACUUM|REINDEX)\b`,
)

// validateReadOnly rejects anything that isn't a single read-only SELECT/WITH query.
func validateReadOnly(query string) error {
	trimmed := strings.TrimSpace(strings.TrimSuffix(strings.TrimSpace(query), ";"))
	if trimmed == "" {
		return errors.New("empty query")
	}
	if strings.Contains(trimmed, ";") {
		return errors.New("only a single statement is allowed")
	}
	upper := strings.ToUpper(trimmed)
	if !strings.HasPrefix(upper, "SELECT") && !strings.HasPrefix(upper, "WITH") {
		return errors.New("only read-only SELECT / WITH queries are allowed")
	}
	if m := writeStmtRe.FindString(upper); m != "" {
		return fmt.Errorf("disallowed keyword %q — this console is read-only", m)
	}
	return nil
}

// RunReadOnlyQuery executes an operator-supplied SELECT and returns the column names
// and rows (all values stringified). It backs the tailnet-only /ops/query console —
// the ad-hoc slice-and-dice layer over the raw logs. Read-only is enforced by
// validation (SELECT/WITH only, single statement, no write keywords); a SELECT cannot
// mutate data in SQLite regardless. Bounded by a timeout and a row cap.
func (s *store) RunReadOnlyQuery(
	ctx context.Context, query string,
) (cols []string, rows [][]string, truncated bool, err error) {
	if err := validateReadOnly(query); err != nil {
		return nil, nil, false, err
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()

	rs, err := s.db.QueryContext(ctx, query)
	if err != nil {
		return nil, nil, false, fmt.Errorf("query: %w", err)
	}
	defer func() { _ = rs.Close() }()

	cols, err = rs.Columns()
	if err != nil {
		return nil, nil, false, fmt.Errorf("columns: %w", err)
	}
	for rs.Next() {
		if len(rows) >= queryResultRowCap {
			truncated = true
			break
		}
		vals := make([]any, len(cols))
		ptrs := make([]any, len(cols))
		for i := range vals {
			ptrs[i] = &vals[i]
		}
		if err := rs.Scan(ptrs...); err != nil {
			return nil, nil, false, fmt.Errorf("scan: %w", err)
		}
		rec := make([]string, len(cols))
		for i, v := range vals {
			rec[i] = cellString(v)
		}
		rows = append(rows, rec)
	}
	return cols, rows, truncated, rs.Err()
}

// cellString renders a scanned SQL value as a display string.
func cellString(v any) string {
	switch t := v.(type) {
	case nil:
		return ""
	case []byte:
		return string(t)
	case string:
		return t
	default:
		return fmt.Sprintf("%v", t)
	}
}
