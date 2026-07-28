// Datacenter / cloud-hosting IP detection, for visit tracking. The User-Agent
// classifier can't catch a scraper that spoofs a real browser UA — but such traffic
// almost always originates from a cloud/hosting IP, not a home or office ISP (the
// scrapers we observed came from Alibaba Cloud with a spoofed Chrome/macOS UA and a
// fake google.com referer). So a browser-looking UA from a datacenter range is
// reclassified as "automated" rather than counted as a human visitor.
//
// This is a CURATED, NON-EXHAUSTIVE seed list of major cloud/hosting providers'
// principal ranges — a best-effort heuristic, like the UA classifier. It catches the
// common offenders (and the ones we've actually seen) without a dependency; it does
// NOT cover every VPS/proxy, and AWS/Azure are too fragmented to hardcode safely.
// The robust, comprehensive version is an ASN / IP-intelligence dataset (e.g. MaxMind
// GeoLite2 ASN) — a later upgrade if the numbers warrant it. Add ranges here as new
// scraper sources show up in /ops/query.

package server

import "net"

// datacenterCIDRs are principal IPv4 ranges of major cloud/hosting providers.
var datacenterCIDRs = []string{
	// Alibaba Cloud (observed) + other known intl blocks.
	"47.74.0.0/15", "47.76.0.0/14", "47.80.0.0/13", "47.235.0.0/16",
	"47.236.0.0/14", "47.240.0.0/14", "47.246.0.0/15", "47.252.0.0/14", "8.208.0.0/12",
	// Google Cloud.
	"34.64.0.0/10", "35.184.0.0/13", "35.192.0.0/14", "35.196.0.0/15",
	"35.198.0.0/16", "35.199.0.0/16", "35.200.0.0/13", "130.211.0.0/16", "104.196.0.0/14",
	// DigitalOcean.
	"104.131.0.0/16", "134.209.0.0/16", "138.197.0.0/16", "146.190.0.0/16",
	"157.230.0.0/16", "159.65.0.0/16", "159.89.0.0/16", "161.35.0.0/16",
	"165.227.0.0/16", "167.71.0.0/16", "167.99.0.0/16", "178.62.0.0/16",
	"188.166.0.0/16", "206.189.0.0/16", "46.101.0.0/16", "68.183.0.0/16",
	// Hetzner.
	"5.9.0.0/16", "49.12.0.0/15", "78.46.0.0/15", "88.99.0.0/16",
	"94.130.0.0/16", "95.216.0.0/15", "116.202.0.0/15", "135.181.0.0/16",
	"138.201.0.0/16", "159.69.0.0/16", "168.119.0.0/16", "195.201.0.0/16",
	// OVH.
	"51.68.0.0/14", "51.75.0.0/16", "51.83.0.0/16", "51.89.0.0/16",
	"51.91.0.0/16", "51.178.0.0/16", "54.36.0.0/16", "137.74.0.0/16", "145.239.0.0/16",
	// Linode / Akamai.
	"45.33.0.0/16", "45.56.0.0/16", "45.79.0.0/16", "139.144.0.0/16",
	"172.104.0.0/15", "173.255.192.0/18", "96.126.96.0/19",
	// Vultr.
	"45.32.0.0/16", "45.63.0.0/16", "45.76.0.0/15", "108.61.0.0/16", "149.28.0.0/16",
	// Oracle Cloud.
	"129.146.0.0/16", "132.145.0.0/16", "140.238.0.0/16", "150.136.0.0/16",
	"158.101.0.0/16", "168.138.0.0/16",
}

// datacenterNets is the parsed form, built once at package init.
var datacenterNets = parseCIDRs(datacenterCIDRs)

func parseCIDRs(cidrs []string) []*net.IPNet {
	nets := make([]*net.IPNet, 0, len(cidrs))
	for _, c := range cidrs {
		if _, n, err := net.ParseCIDR(c); err == nil {
			nets = append(nets, n)
		}
	}
	return nets
}

// isDatacenterIP reports whether an IP falls in a known cloud/hosting range.
func isDatacenterIP(ip string) bool {
	if ip == "" {
		return false
	}
	parsed := net.ParseIP(ip)
	if parsed == nil {
		return false
	}
	for _, n := range datacenterNets {
		if n.Contains(parsed) {
			return true
		}
	}
	return false
}
