# Scheduled sweepers.
#
# These are not optional extras. Three of our four dependencies lose or hide
# state: MoneyPrinterTurbo strands an interrupted render at state=4 forever and
# loses task state on restart without Redis; Postiz has no failure webhook and
# can accept a post, return 200, and never publish it; and Supabase webhooks
# can simply never arrive. Without these jobs the queue silently fills with
# dead productions.

locals {
  sweepers = {
    # The one that makes the webhook bridge safe to depend on rather than
    # trust. Cheap enough to run every minute.
    reconcile_gates = { rate = "rate(1 minute)" }

    reconcile_renders   = { rate = "rate(5 minutes)" }
    reconcile_publishes = { rate = "rate(5 minutes)" }

    # Catches a row whose state machine died -- the only way to detect a dead
    # Gate 2 token, where the row looks healthy and a decision resumes nothing.
    reconcile_executions = { rate = "rate(15 minutes)" }

    # Reclaims render-host disk. A full disk presents as a mysterious global
    # render outage rather than as a disk problem.
    reap_mpt_tasks = { rate = "rate(24 hours)" }

    # Stops a three-week-old idea being approved against a dead trend.
    expire_ideas = { rate = "rate(24 hours)" }

    # Closes the loop: what we published, and how it actually performed.
    collect_analytics = { rate = "rate(12 hours)" }
  }
}

resource "aws_scheduler_schedule" "sweeper" {
  for_each = local.sweepers

  name                = "${var.name}-${each.key}"
  schedule_expression = each.value.rate
  flexible_time_window { mode = "OFF" }

  target {
    arn      = aws_lambda_function.activities.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ activity = each.key })

    retry_policy {
      maximum_retry_attempts = 2
    }
  }
}

# Trend research. Daily rather than hourly: at ten reels a day the owner cannot
# review more than one batch, and every extra run is more browser sessions
# against a platform that fights scrapers.
resource "aws_scheduler_schedule" "trends" {
  name                = "${var.name}-trends"
  schedule_expression = "cron(0 6 * * ? *)"
  flexible_time_window { mode = "OFF" }

  target {
    arn      = aws_ecs_cluster.pipeline.arn
    role_arn = aws_iam_role.scheduler.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.trends.arn
      launch_type         = "FARGATE"
      network_configuration {
        subnets          = aws_subnet.private[*].id
        security_groups  = [aws_security_group.tasks.id]
        assign_public_ip = false # egress goes out through the NAT gateway
      }
    }
    retry_policy { maximum_retry_attempts = 1 }
  }
}
