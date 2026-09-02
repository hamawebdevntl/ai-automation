output "state_machine_arn" {
  value = aws_sfn_state_machine.production.arn
}

output "gate1_webhook_url" {
  description = "Point a Supabase Database Webhook on `ideas` here, with WHEN (old.status <> 'approved' AND new.status = 'approved'), timeout 5000ms."
  value       = "${aws_apigatewayv2_api.bridge.api_endpoint}/gate1"
}

output "gate2_webhook_url" {
  description = "Point a Supabase Database Webhook on `productions` here, with WHEN (old.status IS DISTINCT FROM new.status AND new.status IN ('approved','rejected')), timeout 5000ms. Without the WHEN clause the pipeline's own writes re-invoke this dozens of times per production."
  value       = "${aws_apigatewayv2_api.bridge.api_endpoint}/gate2"
}

output "ecr_repositories" {
  description = "Push targets for CI, which builds the images because there is no Docker on the dev machine."
  value       = { for k, v in aws_ecr_repository.img : k => v.repository_url }
}
