package server

import "testing"

func TestClassifyClient(t *testing.T) {
	cases := map[string]string{
		"Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/120": clientHuman,
		"Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)":      clientHuman,
		"Googlebot/2.1 (+http://www.google.com/bot.html)":             clientCrawler,
		"facebookexternalhit/1.1":                                     clientCrawler,
		"curl/8.4.0":                                                  clientAutomated,
		"python-requests/2.31.0":                                      clientAutomated,
		"Go-http-client/2.0":                                          clientAutomated,
		"":                                                            clientAutomated,
		"SomeRandomClient/1.0":                                        clientAutomated,
	}
	for ua, want := range cases {
		if got := classifyClient(ua); got != want {
			t.Errorf("classifyClient(%q) = %q, want %q", ua, got, want)
		}
	}
}

func TestIsTrackedPage(t *testing.T) {
	tracked := []string{"/", "/live", "/radar", "/insiders", "/filings", "/companies/0000320193", "/filings/0001-24-000001"}
	for _, p := range tracked {
		if !isTrackedPage(p) {
			t.Errorf("isTrackedPage(%q) = false, want true", p)
		}
	}
	untracked := []string{"/health", "/static/live.js", "/api/live-events", "/ops/", "/ops/traffic", "/wp-admin", "/favicon.ico"}
	for _, p := range untracked {
		if isTrackedPage(p) {
			t.Errorf("isTrackedPage(%q) = true, want false", p)
		}
	}
}

func TestReferrerHost(t *testing.T) {
	cases := map[string]string{
		"https://www.google.com/search?q=filings": "www.google.com",
		"https://t.co/abc":                        "t.co",
		"":                                        "",
	}
	for in, want := range cases {
		if got := referrerHost(in); got != want {
			t.Errorf("referrerHost(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestVisitorDayHashRotatesAndGroups(t *testing.T) {
	// Same (ip, ua) on the same day -> same hash (groups a visitor's views);
	// empty IP -> empty hash (uncounted).
	a := visitorDayHash("1.2.3.4", "Mozilla/5.0")
	b := visitorDayHash("1.2.3.4", "Mozilla/5.0")
	c := visitorDayHash("5.6.7.8", "Mozilla/5.0")
	if a == "" || a != b {
		t.Errorf("same visitor same day should hash equal and non-empty: %q vs %q", a, b)
	}
	if a == c {
		t.Errorf("different IPs should hash differently")
	}
	if visitorDayHash("", "Mozilla/5.0") != "" {
		t.Errorf("empty IP should yield empty hash")
	}
}
