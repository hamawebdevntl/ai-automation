resource "aws_cloudwatch_log_group" "sfn" {
  name              = "/aws/vendedlogs/states/${var.name}-production"
  retention_in_days = 30
}

resource "aws_sfn_state_machine" "production" {
  name     = "${var.name}-production"
  role_arn = aws_iam_role.sfn.arn

  # Standard, not Express. Express does not support the waitForTaskToken
  # callback pattern at all and caps at five minutes, while a Gate 2 wait can
  # last days. Cost is a rounding error here: ~10 videos a day at a few hundred
  # state transitions each is a couple of dollars a month against render spend.
  type = "STANDARD"

  definition = templatefile("${path.module}/statemachine.asl.json", {
    activities_lambda_arn = aws_lambda_function.activities.arn
    media_lambda_arn      = aws_lambda_function.media.arn
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.sfn.arn}:*"
    include_execution_data = true
    level                  = "ERROR"
  }
}

# A rejection and a park are modelled as Succeed, so a FAILED execution always
# means something is genuinely broken and this alarm means something.
resource "aws_cloudwatch_metric_alarm" "executions_failed" {
  alarm_name          = "${var.name}-production-failed"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ExecutionsFailed"
  namespace           = "AWS/States"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  dimensions          = { StateMachineArn = aws_sfn_state_machine.production.arn }
}

resource "aws_cloudwatch_metric_alarm" "executions_timed_out" {
  alarm_name          = "${var.name}-production-timed-out"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ExecutionsTimedOut"
  namespace           = "AWS/States"
  period              = 3600
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  dimensions          = { StateMachineArn = aws_sfn_state_machine.production.arn }
}
