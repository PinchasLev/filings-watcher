// Package sqlutil holds small SQL helpers shared across the service.
package sqlutil

import (
	"regexp"
	"strings"
)

// tokenRe matches the structural tokens that govern statement splitting:
// the BEGIN/END words (word-boundary matched, case-insensitive) that delimit a
// trigger body, and the ';' separator itself.
var tokenRe = regexp.MustCompile(`(?i)\bBEGIN\b|\bEND\b|;`)

// SplitStatements splits a SQL script into individual statements on ';'.
//
// A CREATE TRIGGER body is a BEGIN...END block whose inner statements are
// themselves ';'-terminated, so a naive Split(";") would tear a trigger apart.
// SplitStatements tracks BEGIN/END nesting and splits only on semicolons at
// depth 0; plain DDL (no BEGIN/END) is unaffected. Line comments (-- to end of
// line) are stripped first. It mirrors the orchestrator's Python migration
// runner so both apply the same migration files identically.
func SplitStatements(sqlText string) []string {
	var lines []string
	for _, line := range strings.Split(sqlText, "\n") {
		if i := strings.Index(line, "--"); i >= 0 {
			line = line[:i]
		}
		lines = append(lines, line)
	}
	cleaned := strings.Join(lines, "\n")

	var out []string
	var buf strings.Builder
	depth := 0
	last := 0
	for _, loc := range tokenRe.FindAllStringIndex(cleaned, -1) {
		buf.WriteString(cleaned[last:loc[0]])
		tok := cleaned[loc[0]:loc[1]]
		last = loc[1]
		switch {
		case strings.EqualFold(tok, "BEGIN"):
			depth++
			buf.WriteString(tok)
		case strings.EqualFold(tok, "END"):
			if depth > 0 {
				depth--
			}
			buf.WriteString(tok)
		case depth == 0: // a ';' at statement level
			if stmt := strings.TrimSpace(buf.String()); stmt != "" {
				out = append(out, stmt)
			}
			buf.Reset()
		default: // a ';' inside a trigger body
			buf.WriteString(tok)
		}
	}
	buf.WriteString(cleaned[last:])
	if stmt := strings.TrimSpace(buf.String()); stmt != "" {
		out = append(out, stmt)
	}
	return out
}
