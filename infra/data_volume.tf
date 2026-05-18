# Dedicated EBS volume for application data (SQLite DB, Caddy ACME state).
# Lives across instance replacement so substrate changes that recreate the
# host do not destroy production data.
#
# See ADR 0019 for the durability story and recovery procedure.

resource "aws_ebs_volume" "data" {
  availability_zone = var.availability_zone
  size              = var.data_volume_gb
  type              = "gp3"
  encrypted         = true

  tags = {
    Name     = "filings-watcher-data"
    Snapshot = "daily"
  }

  # Protect against accidental destruction: deleting the data volume must be
  # a deliberate two-step operator action (remove the lifecycle block, then
  # terraform destroy).
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_volume_attachment" "data" {
  device_name = "/dev/sdh"
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.host.id

  # When the host is replaced, detach the data volume cleanly. The default
  # behaviour would also delete the volume on instance termination, which we
  # explicitly do not want.
  stop_instance_before_detaching = true
}
