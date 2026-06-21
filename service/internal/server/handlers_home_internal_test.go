// Tests internal helpers in handlers_home.go that aren't exposed through
// the storer interface and aren't observable from the external server_test
// package. Lives in the `server` package (not `server_test`) so it can
// reach the unexported helpers directly.

package server

import "testing"

func TestEdgarFilingURL(t *testing.T) {
	tests := []struct {
		accession string
		want      string
	}{
		{
			accession: "0001234567-26-000001",
			want:      "https://www.sec.gov/Archives/edgar/data/1234567/000123456726000001/0001234567-26-000001-index.htm",
		},
		{
			accession: "0000000001-26-000001",
			want:      "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/0000000001-26-000001-index.htm",
		},
		{
			accession: "bogus",
			want:      "https://www.sec.gov/",
		},
	}
	for _, tt := range tests {
		got := edgarFilingURL(tt.accession)
		if got != tt.want {
			t.Errorf("edgarFilingURL(%q) = %q; want %q", tt.accession, got, tt.want)
		}
	}
}

func TestAnchorLabel(t *testing.T) {
	item := "5.02"
	exhibit := "EX-99.1"
	empty := ""
	tests := []struct {
		name   string
		anchor *string
		want   string
	}{
		{name: "8-K item gets Item prefix", anchor: &item, want: "Item 5.02"},
		{name: "6-K exhibit label shown verbatim", anchor: &exhibit, want: "EX-99.1"},
		{name: "nil renders empty", anchor: nil, want: ""},
		{name: "empty string renders empty", anchor: &empty, want: ""},
	}
	for _, tt := range tests {
		if got := anchorLabel(tt.anchor); got != tt.want {
			t.Errorf("%s: anchorLabel() = %q; want %q", tt.name, got, tt.want)
		}
	}
}
