# Postiz: publishing and analytics.
#
# One EC2 instance running upstream's own compose topology, not eight ECS
# services. Its stack is postiz + postgres + redis + temporal +
# temporal-postgres + elasticsearch; hand-decomposing that into ECS would mean
# three databases and an Elasticsearch cluster to operate for a service that
# publishes forty posts a day. Upstream tests the compose arrangement; we do
# not need to invent a different one.
#
# It is deployed as-is and only ever called over its REST API. That network
# boundary is what keeps its AGPL licence at arm's length.

data "aws_secretsmanager_secret" "postiz" {
  name = var.postiz_secrets_name
}

data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-kernel-6.1-x86_64"]
  }
}

resource "aws_iam_role" "postiz" {
  name = "${var.name}-postiz"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "postiz" {
  name = "${var.name}-postiz"
  role = aws_iam_role.postiz.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = data.aws_secretsmanager_secret.postiz.arn
    }]
  })
}

# Session Manager instead of SSH: no key pair to distribute, no port 22 open,
# and access is IAM-audited.
resource "aws_iam_role_policy_attachment" "postiz_ssm" {
  role       = aws_iam_role.postiz.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "postiz" {
  name = "${var.name}-postiz"
  role = aws_iam_role.postiz.name
}

resource "aws_security_group" "postiz" {
  name        = "${var.name}-postiz"
  description = "Postiz host. Inbound from the public ALB only."
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 4007
    to_port         = 4007
    protocol        = "tcp"
    security_groups = [aws_security_group.postiz_alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# The pipeline Lambdas also need to reach it.
resource "aws_security_group_rule" "postiz_from_lambda" {
  type                     = "ingress"
  from_port                = 4007
  to_port                  = 4007
  protocol                 = "tcp"
  security_group_id        = aws_security_group.postiz.id
  source_security_group_id = aws_security_group.lambda.id
}

# Databases and uploads on their own volume, so the instance can be replaced
# without losing the connected channels and their OAuth tokens -- which are the
# expensive thing here, given TikTok's audit and YouTube's quota review.
resource "aws_ebs_volume" "postiz_data" {
  availability_zone = aws_subnet.private[0].availability_zone
  size              = 100
  type              = "gp3"
  encrypted         = true
  tags              = { Name = "${var.name}-postiz-data" }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_instance" "postiz" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.postiz_instance_type
  subnet_id              = aws_subnet.private[0].id
  vpc_security_group_ids = [aws_security_group.postiz.id]
  iam_instance_profile   = aws_iam_instance_profile.postiz.name

  root_block_device {
    volume_size = 30
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = templatefile("${path.module}/postiz-userdata.sh.tftpl", {
    secret_arn   = data.aws_secretsmanager_secret.postiz.arn
    region       = var.region
    compose_file = file("${path.module}/../docker/postiz-compose.yaml")
  })

  # Replacing the instance is a deliberate act: the data volume is separate, but
  # a replacement still means downtime for the publishing path.
  user_data_replace_on_change = false

  tags = { Name = "${var.name}-postiz" }
}

resource "aws_volume_attachment" "postiz_data" {
  device_name = "/dev/xvdf"
  volume_id   = aws_ebs_volume.postiz_data.id
  instance_id = aws_instance.postiz.id
}

# Nightly snapshots. The OAuth tokens for four platforms live in this database.
resource "aws_dlm_lifecycle_policy" "postiz_data" {
  description        = "${var.name}-postiz-data nightly snapshots"
  execution_role_arn = aws_iam_role.dlm.arn
  state              = "ENABLED"

  policy_details {
    resource_types = ["VOLUME"]
    target_tags    = { Name = "${var.name}-postiz-data" }

    schedule {
      name = "nightly"

      create_rule {
        interval      = 24
        interval_unit = "HOURS"
        times         = ["03:00"]
      }

      retain_rule {
        count = 14
      }

      copy_tags = true
    }
  }
}

resource "aws_iam_role" "dlm" {
  name = "${var.name}-dlm"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "dlm.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "dlm" {
  role       = aws_iam_role.dlm.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSDataLifecycleManagerServiceRole"
}

# --- Public endpoint ---------------------------------------------------------
#
# Public because it has to be. Connecting a channel is an OAuth flow the owner
# completes in a browser, and the platforms redirect back to a URL they have on
# file. Postiz's own login plus DISABLE_REGISTRATION=true is the boundary.

resource "aws_security_group" "postiz_alb" {
  name        = "${var.name}-postiz-alb"
  description = "Public ALB for the Postiz UI and OAuth callbacks."
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_lb" "postiz" {
  name               = "${var.name}-postiz"
  internal           = false
  load_balancer_type = "application"
  subnets            = aws_subnet.public[*].id
  security_groups    = [aws_security_group.postiz_alb.id]
}

resource "aws_lb_target_group" "postiz" {
  name        = "${var.name}-postiz"
  port        = 4007
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "instance"

  health_check {
    path                = "/"
    matcher             = "200-399"
    interval            = 30
    timeout             = 10
    healthy_threshold   = 2
    unhealthy_threshold = 5
  }
}

resource "aws_lb_target_group_attachment" "postiz" {
  target_group_arn = aws_lb_target_group.postiz.arn
  target_id        = aws_instance.postiz.id
  port             = 4007
}

resource "aws_lb_listener" "postiz_https" {
  count             = var.postiz_certificate_arn == "" ? 0 : 1
  load_balancer_arn = aws_lb.postiz.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.postiz_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.postiz.arn
  }
}

resource "aws_lb_listener" "postiz_http" {
  load_balancer_arn = aws_lb.postiz.arn
  port              = 80
  protocol          = "HTTP"

  # Redirect to HTTPS once a certificate exists; until then serve directly so
  # the deployment can be verified, accepting that no channel can be connected
  # over plain HTTP.
  default_action {
    type             = var.postiz_certificate_arn == "" ? "forward" : "redirect"
    target_group_arn = var.postiz_certificate_arn == "" ? aws_lb_target_group.postiz.arn : null

    dynamic "redirect" {
      for_each = var.postiz_certificate_arn == "" ? [] : [1]
      content {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
}
