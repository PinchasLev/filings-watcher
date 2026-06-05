data "aws_ami" "al2023_arm" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-arm64"]
  }

  filter {
    name   = "architecture"
    values = ["arm64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

resource "aws_instance" "host" {
  ami                    = data.aws_ami.al2023_arm.id
  instance_type          = var.instance_type
  availability_zone      = var.availability_zone
  subnet_id              = data.aws_subnets.default_az.ids[0]
  vpc_security_group_ids = [aws_security_group.host.id]
  iam_instance_profile   = aws_iam_instance_profile.host.name

  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/user_data.sh.tpl", {
    app_user       = var.app_user
    acme_email     = var.acme_email
    data_volume_id = aws_ebs_volume.data.id
  })

  root_block_device {
    volume_size = var.root_volume_gb
    volume_type = "gp3"
    encrypted   = true

    tags = {
      Name = "filings-watcher-host-root"
    }
  }

  metadata_options {
    http_tokens                 = "required"
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 1
  }

  tags = {
    Name = "filings-watcher-host"
  }

  # Pin the AMI against the floating `most_recent` data source above.
  # Without this, any `terraform apply` after Amazon publishes a newer
  # AL2023 ARM build silently force-replaces this instance, cascade-
  # replaces the EIP association and data-volume attachment, and takes
  # the service down until both `filings-deploy` and
  # `filings-install-orchestrate-timer` SSM commands are re-run on the
  # fresh host. See 2026-06-05 outage. Bump the AMI explicitly (taint
  # this resource or `terraform apply -replace`) when an OS refresh is
  # actually wanted; routine security patches land via dnf-automatic on
  # the live host (user_data.sh.tpl:25-27).
  lifecycle {
    ignore_changes = [ami]
  }
}

resource "aws_eip" "host" {
  domain = "vpc"

  tags = {
    Name = "filings-watcher-eip"
  }
}

resource "aws_eip_association" "host" {
  instance_id   = aws_instance.host.id
  allocation_id = aws_eip.host.id
}
