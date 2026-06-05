# SSM document that writes the canonical Caddyfile to /etc/caddy/Caddyfile
# and gracefully reloads caddy. The Caddyfile body lives in
# `Caddyfile.tpl` as the single source of truth.
#
# Why an SSM document rather than user_data: `aws_instance.host` has
# `user_data_replace_on_change = true`, so changing the Caddyfile via
# user_data forces a full instance replacement (and the two-step
# recovery — `filings-deploy` + `filings-install-orchestrate-timer` —
# that follows). For a runtime config artifact like the Caddyfile,
# that blast radius is wrong. Extracting it here makes future Caddy
# changes (cache headers, CSP updates, new routes, header tuning,
# etc.) a one-liner: edit the template, `terraform apply` (in-place
# update of this doc, no instance touched), re-run this doc with
# `aws ssm send-command --document-name filings-install-caddy-config`,
# and caddy reloads gracefully without dropped connections.
#
# Bootstrap fallback in user_data.sh.tpl:
#   `user_data.sh.tpl` retains an inline copy of the Caddyfile (the
#   version frozen at the time this doc was introduced). It is used
#   only at first-boot of a fresh instance so caddy can come up before
#   the operator runs this SSM doc. Once the operator runs this doc,
#   the canonical Caddyfile.tpl content overwrites the bootstrap copy
#   on disk.
#
#   The two copies will drift as Caddyfile.tpl gains cache headers,
#   CSP changes, etc. After instance replacement, the operator MUST
#   re-run this SSM doc to reconcile — see
#   terraform-apply-may-replace-host memory note. A future cleanup PR
#   may retire the user_data copy entirely (accepting one instance
#   replacement to do so), at which point caddy would not start at
#   first-boot until this doc runs.
#
# Caddy reload is graceful: in-flight requests complete against the
# old config while the new one takes over. `caddy validate` runs
# first; a malformed Caddyfile aborts the install before reload, so a
# bad config can never wedge the running caddy process.

resource "aws_ssm_document" "install_caddy_config" {
  name            = "filings-install-caddy-config"
  document_type   = "Command"
  document_format = "JSON"

  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Write /etc/caddy/Caddyfile from the canonical template and reload caddy"
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "install"
      inputs = {
        runCommand = [
          "set -euo pipefail",
          "cat > /etc/caddy/Caddyfile <<'CADDYFILE_EOF'",
          templatefile("${path.module}/Caddyfile.tpl", {
            acme_email = var.acme_email
          }),
          "CADDYFILE_EOF",
          "chown caddy:caddy /etc/caddy/Caddyfile",
          "chmod 0644 /etc/caddy/Caddyfile",
          # Validate before reload — caddy validate exits non-zero on a
          # malformed config, aborting this step before the reload runs.
          # A bad Caddyfile therefore cannot wedge the running process.
          "/usr/local/bin/caddy validate --config /etc/caddy/Caddyfile",
          # Graceful reload: in-flight connections complete on the old
          # config; new connections use the new config.
          "systemctl reload caddy",
          "echo \"install of /etc/caddy/Caddyfile complete; caddy reloaded\"",
        ]
      }
    }]
  })

  tags = {
    Name = "filings-install-caddy-config"
  }
}
