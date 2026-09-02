# The webhook bridge.
#
# API Gateway hands off to SQS rather than invoking Lambda directly, and that is
# not incidental. A Supabase Database Webhook is pg_net: at-most-once, no retry,
# no dead-letter queue, and a timeout that defaults to 1000ms. An API Gateway
# hop into a cold Lambda blows that routinely, and a dropped Gate 2 decision
# would otherwise be unrecoverable, because decide_production refuses to act
# once a status has moved on.
#
# Enqueuing takes ~20ms, comfortably inside the timeout, and the Lambda half
# then inherits SQS retries and a dead-letter queue. The gate reconciler
# remains the actual guarantee; this only reduces latency.

resource "aws_sqs_queue" "dlq" {
  name                      = "${var.name}-bridge-dlq"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "gate1" {
  name                       = "${var.name}-gate1"
  visibility_timeout_seconds = 330 # must exceed the consumer's 300s timeout
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 5
  })
}

resource "aws_sqs_queue" "gate2" {
  name                       = "${var.name}-gate2"
  visibility_timeout_seconds = 330
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 5
  })
}

resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "${var.name}-bridge-dlq-not-empty"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
}

resource "aws_apigatewayv2_api" "bridge" {
  name          = "${var.name}-bridge"
  protocol_type = "HTTP"
}

resource "aws_iam_role" "apigw" {
  name = "${var.name}-apigw"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "apigateway.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "apigw" {
  name = "${var.name}-apigw"
  role = aws_iam_role.apigw.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["sqs:SendMessage"], Resource = [aws_sqs_queue.gate1.arn, aws_sqs_queue.gate2.arn] }]
  })
}

locals {
  gates = {
    gate1 = { queue = aws_sqs_queue.gate1.id, activity = "start_production" }
    gate2 = { queue = aws_sqs_queue.gate2.id, activity = "gate2_bridge" }
  }
}

resource "aws_apigatewayv2_integration" "gate" {
  for_each            = local.gates
  api_id              = aws_apigatewayv2_api.bridge.id
  integration_type    = "AWS_PROXY"
  integration_subtype = "SQS-SendMessage"
  credentials_arn     = aws_iam_role.apigw.arn

  request_parameters = {
    QueueUrl    = each.value.queue
    MessageBody = "$request.body"
    # The consumer reads the activity from the message attributes, so one
    # function can serve both routes.
    MessageAttributes = jsonencode({
      activity = { DataType = "String", StringValue = each.value.activity }
    })
  }
}

resource "aws_apigatewayv2_route" "gate" {
  for_each  = local.gates
  api_id    = aws_apigatewayv2_api.bridge.id
  route_key = "POST /${each.key}"
  target    = "integrations/${aws_apigatewayv2_integration.gate[each.key].id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.bridge.id
  name        = "$default"
  auto_deploy = true

  # pg_net cannot sign a request, so this endpoint is reachable and forgeable.
  # A shared secret header is checked by the handler, but the real defence is
  # that the handler takes only a row id from the body and reads the decision
  # from Postgres -- so a forged or replayed call is an idempotent no-op.
  default_route_settings {
    throttling_burst_limit = 20
    throttling_rate_limit  = 10
  }
}
