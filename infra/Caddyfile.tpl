{
    email ${acme_email}
}

# Apex is the canonical public URL serving the Filings Radar product
# from the local Go service. Security headers harden the response;
# the Content-Security-Policy allow-lists the jsdelivr CDN we use for
# Pico.css (and inline <style> in the layout template) and disallows
# scripts entirely since the page has none. See ADR 0024.
#
# /ops/* is explicitly blocked at the Caddy layer: those routes exist
# (or will exist) on the Go service for operator use, but must never
# be reachable from the public internet. Tailnet machines reach the
# Go service directly at 127.0.0.1:8080 (per ADR 0014's mesh-VPN
# operator-access design) and see /ops/* normally. The 404 here is
# the public-side enforcement of the same boundary.
filingsradar.com {
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        Referrer-Policy "strict-origin-when-cross-origin"
        Permissions-Policy "geolocation=(), microphone=(), camera=()"
        Content-Security-Policy "default-src 'self'; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; script-src 'none'; img-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self';"
    }
    handle /ops/* {
        respond "Not Found" 404
    }
    handle {
        reverse_proxy 127.0.0.1:8080
    }
}

# www → apex 301 redirect. Anyone who types www.filingsradar.com lands
# on the canonical apex URL; search engines canonicalize accordingly.
www.filingsradar.com {
    redir https://filingsradar.com{uri} permanent
}

# staging.filingsradar.com currently redirects to apex. The DNS record
# is preserved for the future when a real staging environment exists
# on a separate host; at that point this block becomes a reverse_proxy
# to the staging host's tailnet IP. Single-host "staging" against the
# same binary and database would be theater (see ADR 0024).
staging.filingsradar.com {
    redir https://filingsradar.com{uri} permanent
}
