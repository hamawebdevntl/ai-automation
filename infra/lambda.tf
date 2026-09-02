locals {
  # Constructed rather than referenced, to break a dependency cycle: the
  # functions need the state machine's ARN in their environment, and the state
  # machine needs the functions' ARNs in its definition. The name is ours to
  # set, so the ARN is deterministic.
  state_machine_arn = "arn:aws:states:${data.aws_region.here.name}:${data.aws_caller_identity.me.account_id}:stateMachine:${var.name}-production"

  # Both endpoints are ours, so they are derived rather than supplied. MPT sits
  # on the internal load balancer and is never publicly reachable; Postiz is
  # addressed by its private DNS from inside the VPC even though its own ALB is
  # public for the OAuth flow.
  mpt_base_url    = "http://${aws_lb.internal.dns_name}"
  postiz_base_url = "http://${aws_instance.postiz.private_ip}:4007"

  common_env = {
    SUPABASE_URL         = var.supabase_url
    MPT_BASE_URL         = local.mpt_base_url
    POSTIZ_BASE_URL      = local.postiz_base_url
    RENDERS_BUCKET       = "renders"
    STATE_MACHINE_ARN    = local.state_machine_arn
    PIPELINE_SECRETS_ARN = data.aws_secretsmanager_secret.bundle.arn
    LOG_LEVEL            = "INFO"
  }
}

resource "aws_lambda_function" "activities" {
  function_name = "${var.name}-activities"
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.img["activities"].repository_url}:${var.image_tag}"

  # Generous but not extravagant: the heaviest work here is four synchronous
  # LLM calls in generate_copy, and polling a render that may take 20 minutes
  # is done by the state machine, not by holding this function open.
  timeout     = 300
  memory_size = 1024

  environment { variables = local.common_env }

  # In the VPC because MoneyPrinterTurbo and Postiz are private. Egress to
  # Supabase, the platform APIs and the Anthropic API goes out via the NAT.
  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }
}

resource "aws_lambda_function" "media" {
  function_name = "${var.name}-media"
  role          = aws_iam_role.lambda.arn
  package_type  = "Image"
  image_uri     = "${aws_ecr_repository.img["media"].repository_url}:${var.image_tag}"

  # This one downloads a render, probes it with ffmpeg several times, extracts a
  # poster frame and uploads it again. A 9:16 render can be hundreds of
  # megabytes, so /tmp is raised well above the 512MB default and the memory
  # gives ffmpeg room; Lambda also scales CPU with memory.
  timeout     = 900
  memory_size = 4096
  ephemeral_storage { size = 4096 }

  environment { variables = local.common_env }

  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }
}

resource "aws_cloudwatch_log_group" "activities" {
  name              = "/aws/lambda/${aws_lambda_function.activities.function_name}"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "media" {
  name              = "/aws/lambda/${aws_lambda_function.media.function_name}"
  retention_in_days = 30
}

# --- Bridge queues feed the same function ------------------------------------

resource "aws_lambda_event_source_mapping" "gate1" {
  event_source_arn = aws_sqs_queue.gate1.arn
  function_name    = aws_lambda_function.activities.arn
  batch_size       = 1
}

resource "aws_lambda_event_source_mapping" "gate2" {
  event_source_arn = aws_sqs_queue.gate2.arn
  function_name    = aws_lambda_function.activities.arn
  batch_size       = 1
}
