# MoneyPrinterTurbo: the render service.
#
# Three things about it drive this configuration, all verified in its source:
#
#  * Task state lives in process memory unless enable_redis is true, so a task
#    replacement loses every in-flight render's state. Redis is not optional.
#  * There is no object-storage support anywhere in it. Renders are written to
#    local disk, so the storage directory must outlive the container -- hence
#    EFS rather than ephemeral storage.
#  * Its only auth is one shared secret, and /docs, /openapi.json and /ping are
#    unauthenticated. It stays on an internal ALB, never public.

# --- Task state --------------------------------------------------------------

resource "aws_elasticache_subnet_group" "mpt" {
  name       = "${var.name}-mpt"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_elasticache_replication_group" "mpt" {
  replication_group_id = "${var.name}-mpt"
  description          = "MoneyPrinterTurbo task state and queue"
  engine               = "redis"
  engine_version       = "7.1"
  node_type            = "cache.t4g.micro"
  num_cache_clusters   = 1
  parameter_group_name = "default.redis7"
  port                 = 6379
  subnet_group_name    = aws_elasticache_subnet_group.mpt.name
  security_group_ids   = [aws_security_group.data.id]

  # No auth token and no TLS: this sits in a private subnet reachable only from
  # the task security group, and MoneyPrinterTurbo's Redis client does not
  # support TLS. Its own state module notes that Redis must be inside the trust
  # boundary, because every value is round-tripped through literal_eval.
  transit_encryption_enabled = false
  at_rest_encryption_enabled = true

  # Task state is reconstructible -- a lost render is parked by the reconciler
  # and re-run -- so daily snapshots would be paying for nothing.
  snapshot_retention_limit = 0
  apply_immediately        = true
}

# --- Render storage ----------------------------------------------------------

resource "aws_efs_file_system" "mpt" {
  creation_token = "${var.name}-mpt-storage"
  encrypted      = true

  # Renders are read once, uploaded to object storage, then reaped. Anything
  # still here after a month is abandoned, so let it age out cheaply.
  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }

  tags = { Name = "${var.name}-mpt-storage" }
}

resource "aws_efs_mount_target" "mpt" {
  count           = length(aws_subnet.private)
  file_system_id  = aws_efs_file_system.mpt.id
  subnet_id       = aws_subnet.private[count.index].id
  security_groups = [aws_security_group.data.id]
}

resource "aws_efs_access_point" "mpt" {
  file_system_id = aws_efs_file_system.mpt.id

  posix_user {
    uid = 0
    gid = 0
  }

  root_directory {
    path = "/storage"
    creation_info {
      owner_uid   = 0
      owner_gid   = 0
      permissions = "0755"
    }
  }
}

# --- Internal load balancer --------------------------------------------------

resource "aws_lb" "internal" {
  name               = "${var.name}-internal"
  internal           = true
  load_balancer_type = "application"
  subnets            = aws_subnet.private[*].id
  security_groups    = [aws_security_group.alb.id]
}

resource "aws_lb_target_group" "mpt" {
  name        = "${var.name}-mpt"
  port        = 8080
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    path                = "/ping" # the only unauthenticated route, which suits a health check
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # A render can take twenty minutes; draining a task mid-render would waste
  # paid API calls, so give connections time to finish.
  deregistration_delay = 300
}

resource "aws_lb_listener" "internal" {
  load_balancer_arn = aws_lb.internal.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.mpt.arn
  }
}

# --- Service -----------------------------------------------------------------

resource "aws_iam_role" "mpt_task" {
  name = "${var.name}-mpt-task"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "mpt_task" {
  name = "${var.name}-mpt-task"
  role = aws_iam_role.mpt_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = data.aws_secretsmanager_secret.bundle.arn
    }]
  })
}

resource "aws_cloudwatch_log_group" "mpt" {
  name              = "/ecs/${var.name}-mpt"
  retention_in_days = 30
}

resource "aws_ecs_task_definition" "mpt" {
  family                   = "${var.name}-mpt"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  # Rendering is ffmpeg-bound. max_concurrent_tasks is held at 2 below, and
  # each render gets n_threads=2, so this is sized for that rather than for the
  # upstream default of 5 concurrent renders.
  cpu                = "2048"
  memory             = "8192"
  execution_role_arn = aws_iam_role.ecs_execution.arn
  task_role_arn      = aws_iam_role.mpt_task.arn

  volume {
    name = "storage"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.mpt.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.mpt.id
        iam             = "DISABLED"
      }
    }
  }

  container_definitions = jsonencode([{
    name         = "mpt"
    image        = "${aws_ecr_repository.img["mpt"].repository_url}:${var.image_tag}"
    essential    = true
    portMappings = [{ containerPort = 8080, protocol = "tcp" }]

    mountPoints = [{
      sourceVolume  = "storage"
      containerPath = "/MoneyPrinterTurbo/storage"
      readOnly      = false
    }]

    environment = [
      # Our fork's entrypoint renders config.toml from these before starting.
      { name = "MPT__APP__ENABLE_REDIS", value = "true" },
      { name = "MPT__APP__REDIS_HOST", value = aws_elasticache_replication_group.mpt.primary_endpoint_address },
      { name = "MPT__APP__REDIS_PORT", value = "6379" },
      # Without this, GET /tasks/{id} returns relative artifact paths that the
      # caller has to resolve itself.
      { name = "MPT__APP__ENDPOINT", value = "http://${aws_lb.internal.dns_name}" },
      # Held below the upstream default of 5. Gate 1 approvals arrive as a
      # daily batch, and a queued task is state=4 with no progress -- outwardly
      # identical to a running one -- so a deep queue makes stalls unreadable.
      { name = "MPT__APP__MAX_CONCURRENT_TASKS", value = "2" },
      { name = "MPT__APP__MAX_QUEUED_TASKS", value = "20" },
      # Auto cross-posting must stay off: it mutates the task record after
      # completion, so state=1 would stop meaning "done".
      { name = "MPT__APP__UPLOAD_POST_AUTO_UPLOAD", value = "false" },
      # Everything secret -- api_key and every provider credential -- comes
      # from here, never from a plain environment variable.
      { name = "MPT_SECRETS_ARN", value = data.aws_secretsmanager_secret.bundle.arn },
      { name = "AWS_REGION", value = var.region },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.mpt.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "mpt"
      }
    }
  }])
}

resource "aws_ecs_service" "mpt" {
  name            = "${var.name}-mpt"
  cluster         = aws_ecs_cluster.pipeline.id
  task_definition = aws_ecs_task_definition.mpt.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.mpt.arn
    container_name   = "mpt"
    container_port   = 8080
  }

  # One replica, deliberately. Two would each hold their own concurrency
  # counter -- the limit is a per-process integer, not a shared one -- and a
  # fetch could land on the replica that did not run the render. EFS makes the
  # files shared, but the ALB has no way to route by task id.
  #
  # So a deploy stops the old task before starting the new one. In-flight
  # renders are lost, the reconciler parks them, and a human re-runs: correct,
  # and cheaper than the machinery to avoid it at ten videos a day.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  health_check_grace_period_seconds = 120
  depends_on                        = [aws_lb_listener.internal]
}
