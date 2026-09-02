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

output "mpt_internal_url" {
  description = "Reachable only from inside the VPC. Its only auth is a shared secret, and /docs and /ping are unauthenticated, so it is never exposed."
  value       = "http://${aws_lb.internal.dns_name}"
}

output "postiz_public_url" {
  description = <<-EOT
    Where the owner connects channels. Point each platform app's OAuth redirect
    URI here, and set MAIN_URL / FRONTEND_URL / NEXT_PUBLIC_BACKEND_URL in the
    Postiz secret to match.

    Over plain HTTP the UI loads but no channel can be connected: Instagram,
    TikTok, YouTube and LinkedIn all require an HTTPS redirect URI on a
    registered domain. Supply postiz_certificate_arn to fix that.
  EOT
  value       = var.postiz_certificate_arn == "" ? "http://${aws_lb.postiz.dns_name}" : "https://${aws_lb.postiz.dns_name}"
}

output "postiz_ssh" {
  description = "No SSH key or open port 22; access is via Session Manager."
  value       = "aws ssm start-session --target ${aws_instance.postiz.id} --region ${var.region}"
}

output "next_steps" {
  value = <<-EOT
    1. Create the two secrets: ${var.secrets_bundle_name} (pipeline) and ${var.postiz_secrets_name} (Postiz).
    2. terraform apply -var image_tag=<git sha from the images workflow>
    3. Open postiz_public_url, create the single owner account, connect channels.
    4. Point two Supabase Database Webhooks at gate1_webhook_url and gate2_webhook_url,
       WITH the WHEN clauses in their descriptions and a 5000ms timeout.
    5. Set NICHE_BRIEF and TREND_HASHTAGS in the pipeline secret. The trend run
       refuses to start without a brief.
  EOT
}
