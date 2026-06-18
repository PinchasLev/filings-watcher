package sqlutil_test

import (
	"strings"
	"testing"

	"github.com/PinchasLev/filings-watcher/service/internal/sqlutil"
)

func TestSplitStatements_KeepsTriggerBodyIntact(t *testing.T) {
	sql := `CREATE TABLE t (a INTEGER);
CREATE TRIGGER t_no_update BEFORE UPDATE ON t
BEGIN
    SELECT RAISE(ABORT, 'append-only');
END;
CREATE INDEX t_idx ON t (a);`

	got := sqlutil.SplitStatements(sql)
	if len(got) != 3 {
		t.Fatalf("expected 3 statements, got %d: %#v", len(got), got)
	}
	if !strings.HasPrefix(got[1], "CREATE TRIGGER") {
		t.Fatalf("statement 1 not the trigger: %q", got[1])
	}
	if !strings.Contains(got[1], "RAISE(ABORT, 'append-only')") {
		t.Fatalf("trigger body lost: %q", got[1])
	}
	if !strings.HasSuffix(strings.TrimSpace(got[1]), "END") {
		t.Fatalf("trigger not terminated at END: %q", got[1])
	}
	if !strings.HasPrefix(got[2], "CREATE INDEX") {
		t.Fatalf("statement 2 not the index: %q", got[2])
	}
}

func TestSplitStatements_PlainDDLUnaffected(t *testing.T) {
	sql := "CREATE TABLE a (x INTEGER); CREATE TABLE b (y TEXT);"
	got := sqlutil.SplitStatements(sql)
	if len(got) != 2 {
		t.Fatalf("expected 2 statements, got %d: %#v", len(got), got)
	}
}
