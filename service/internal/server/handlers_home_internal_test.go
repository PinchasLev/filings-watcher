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
