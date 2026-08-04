package store

import (
	"strings"
	"testing"
)

func TestExcerptFromBlock(t *testing.T) {
	long := strings.Repeat("word ", 60) // 300 chars, well over the 200-char limit
	cases := []struct {
		name, in string
		want     func(string) bool
	}{
		{"empty", "", func(s string) bool { return s == "" }},
		{"collapses whitespace", "  a\n\tb   c  ", func(s string) bool { return s == "a b c" }},
		{"short kept whole", "A brief risk.", func(s string) bool { return s == "A brief risk." }},
		{"long truncated on a word boundary", long, func(s string) bool {
			body := strings.TrimSuffix(s, "…")
			return strings.HasSuffix(s, "…") && len(body) <= 200 && !strings.HasSuffix(body, " ")
		}},
	}
	for _, c := range cases {
		if got := excerptFromBlock(c.in); !c.want(got) {
			t.Errorf("%s: excerptFromBlock(%q) = %q", c.name, c.in, got)
		}
	}
}
