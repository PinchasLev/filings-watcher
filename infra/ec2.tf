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

resource "aws_key_pair" "operator" {
  key_name   = "filings-watcher-operator"
  public_key = var.ssh_public_key
}

resource "aws_instance" "host" {
  ami                    = data.aws_ami.al2023_arm.id
  instance_type          = var.instance_type
  availability_zone      = var.availability_zone
  subnet_id              = data.aws_subnets.default_az.ids[0]
  key_name               = aws_key_pair.operator.key_name
  vpc_security_group_ids = [aws_security_group.host.id]
  iam_instance_profile   = aws_iam_instance_profile.host.name

  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/user_data.sh.tpl", {
    app_user = var.app_user
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
