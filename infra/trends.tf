# Fargate for the trend run.
#
# Not a Lambda: TikTok-Api signs every request inside a live Playwright page, so
# each session is a real browser process. Isolating it means a wedged browser
# cannot stall renders or publishing.

resource "aws_ecs_cluster" "pipeline" {
  name = "${var.name}-pipeline"
}

resource "aws_iam_role" "ecs_execution" {
  name = "${var.name}-ecs-execution"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "trends_task" {
  name = "${var.name}-trends-task"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "trends_task" {
  name = "${var.name}-trends-task"
  role = aws_iam_role.trends_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = data.aws_secretsmanager_secret.bundle.arn
    }]
  })
}

resource "aws_cloudwatch_log_group" "trends" {
  name              = "/ecs/${var.name}-trends"
  retention_in_days = 30
}

resource "aws_ecs_task_definition" "trends" {
  family                   = "${var.name}-trends"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  # A browser plus our own scoring. Generous on memory because Playwright
  # contexts are the single largest consumer here and an OOM kill mid-run
  # produces no ideas at all.
  cpu                = "1024"
  memory             = "4096"
  execution_role_arn = aws_iam_role.ecs_execution.arn
  task_role_arn      = aws_iam_role.trends_task.arn

  container_definitions = jsonencode([{
    name      = "trends"
    image     = "${aws_ecr_repository.img["trends"].repository_url}:${var.image_tag}"
    essential = true
    environment = [
      { name = "SUPABASE_URL", value = var.supabase_url },
      { name = "MPT_BASE_URL", value = local.mpt_base_url },
      { name = "POSTIZ_BASE_URL", value = local.postiz_base_url },
      { name = "PIPELINE_SECRETS_ARN", value = data.aws_secretsmanager_secret.bundle.arn },
      { name = "LOG_LEVEL", value = "INFO" },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.trends.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "trends"
      }
    }
  }])
}
