# 0036. External web-service health check

- **Status:** Accepted
- **Date:** 2026-06-23

## Context

The CloudWatch alarms to date watch the *host*: two heartbeats (host + drainer), the EC2 status check, and — after ADR 0035 — memory/swap pressure. None of them watch the *web service itself*. The 2026-06-22 incident review surfaced the gap: if `filings-server` (the Go app) crash-looped, panicked, or its TLS cert expired while the box stayed healthy, the site would be down and **nothing would page** — the host heartbeat keeps firing, the EC2 status check passes, and the memory alarms are about the box, not the app. The user-facing surface had no liveness alarm at all.

`filings-server` is `Type=simple` with `Restart=on-failure`, so systemd already auto-restarts it on a crash; the gap is purely observability — a crash-loop or a hard-down app would recover or not, but either way no one would be told.

## Decision

A **Route 53 health check** probes `https://filingsradar.com/health` every 30 s from multiple AWS edge locations (`failure_threshold = 3`), and a CloudWatch alarm (`filings-watcher-site-unhealthy`) pages the existing SNS topic when `HealthCheckStatus` goes to 0. Because the probe runs from outside AWS, it exercises the entire user path — DNS → Caddy → TLS → the Go app's `/health` handler — in one check, catching a crash-loop, panic, OOM, expired cert, or DNS/host failure. `/health` is the Go server's lightweight liveness endpoint (no DB query). The alarm uses `treat_missing_data = notBreaching`: the real signal is a *present* `HealthCheckStatus = 0` from an AWS-managed continuous metric, so breaching would only add a transient false page at creation time.

## Alternatives considered

### On-box heartbeat (a `filings-server` liveness metric)

Rejected as the primary: an on-box probe can't see a failure in the parts of the path *outside* the process — Caddy down, TLS/cert broken, DNS misconfigured, or the host up-but-unreachable. An external check tests what a user actually experiences.

### CloudWatch Synthetics canary

Rejected: more capable (content assertions, screenshots) but materially heavier — a Lambda, an S3 artifact bucket, and IAM, plus per-run cost — for no benefit over "is the site answering 200." Revisit only if we need multi-step or content-level checks.

## Consequences

- A web-service outage on an otherwise-healthy host now pages within ~2–3 min (≈90 s health-check debounce + the alarm period), closing the last unmonitored failure class from the incident review.
- Pure Terraform (a `aws_route53_health_check` + an alarm) — no host change and no SSM-document re-run, so it deploys with a plain `terraform apply`.
- The check overlaps the host-heartbeat alarm for "whole box down," but that is intentional: this one specifically says *the site is down*, which is the operator's first question, and it fires for app-only failures the heartbeat cannot see.
- Cost is ~$0.50/month for the health check plus the usual per-alarm cents.
- It depends on `/health` remaining publicly reachable through Caddy; a routing change that hides it would need this path updated.
