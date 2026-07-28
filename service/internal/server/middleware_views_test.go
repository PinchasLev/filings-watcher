package server

import "testing"

func TestClassifyClient(t *testing.T) {
	const browser = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122"
	// A non-datacenter IP (TEST-NET-3, reserved) so UA-based cases classify by UA.
	const homeIP = "203.0.113.7"
	type tc struct {
		ua, ip, want string
	}
	for _, c := range []tc{
		{browser, homeIP, clientHuman},
		{"Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)", homeIP, clientHuman},
		{"Googlebot/2.1 (+http://www.google.com/bot.html)", homeIP, clientCrawler},
		{"facebookexternalhit/1.1", homeIP, clientCrawler},
		{"curl/8.4.0", homeIP, clientAutomated},
		{"python-requests/2.31.0", homeIP, clientAutomated},
		{"", homeIP, clientAutomated},
		{"SomeRandomClient/1.0", homeIP, clientAutomated},
		// The observed case: a spoofed browser UA from an Alibaba Cloud IP -> automated.
		{browser, "47.76.93.214", clientAutomated},
		// A self-identified crawler from a datacenter is still a crawler.
		{"Googlebot/2.1", "34.64.1.1", clientCrawler},
	} {
		if got := classifyClient(c.ua, c.ip); got != c.want {
			t.Errorf("classifyClient(%q, %q) = %q, want %q", c.ua, c.ip, got, c.want)
		}
	}
}

func TestIsDatacenterIP(t *testing.T) {
	datacenter := []string{"47.76.93.214", "47.76.83.75", "34.64.1.1", "159.65.10.20"}
	for _, ip := range datacenter {
		if !isDatacenterIP(ip) {
			t.Errorf("isDatacenterIP(%q) = false, want true", ip)
		}
	}
	notDatacenter := []string{"203.0.113.7", "8.8.8.8", "1.1.1.1", "", "not-an-ip"}
	for _, ip := range notDatacenter {
		if isDatacenterIP(ip) {
			t.Errorf("isDatacenterIP(%q) = true, want false", ip)
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
