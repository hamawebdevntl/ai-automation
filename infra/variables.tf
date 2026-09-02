variable "region" {
  type    = string
  default = "eu-west-1" # matches the Supabase project's region
}

variable "name" {
  type    = string
  default = "reels"
}

variable "image_tag" {
  description = "Image tag to deploy. Built and pushed by CI, since there is no Docker on the dev machine."
  type        = string
}

variable "supabase_url" {
  type = string
}

# Everything genuinely secret lives in one Secrets Manager bundle, resolved at
# cold start. Lambda cannot inject Secrets Manager into env vars, and a plain
# env var is readable via GetFunctionConfiguration.
variable "secrets_bundle_name" {
  type    = string
  default = "reels/pipeline"
}

variable "postiz_secrets_name" {
  description = <<-EOT
    Secrets Manager secret holding Postiz's own environment: JWT_SECRET,
    POSTIZ_DB_PASSWORD, TEMPORAL_DB_PASSWORD, MAIN_URL, FRONTEND_URL,
    NEXT_PUBLIC_BACKEND_URL and the per-platform OAuth client ids/secrets.

    Kept separate from the pipeline bundle on purpose: the Postiz host has no
    business holding the Supabase service-role key, and the pipeline has no
    business holding platform OAuth secrets.
  EOT
  type        = string
  default     = "reels/postiz"
}

variable "postiz_certificate_arn" {
  description = <<-EOT
    ACM certificate for the Postiz endpoint. Optional, but OAuth does not work
    without it.

    Postiz is the one service that must be reachable from a browser: connecting
    an Instagram, TikTok, YouTube or LinkedIn channel is an OAuth flow the owner
    completes by hand, and those platforms require an HTTPS redirect URI on a
    real registered domain. Leave this empty and the listener is HTTP-only --
    enough to reach the UI and prove the deployment, not enough to connect a
    single channel.
  EOT
  type        = string
  default     = ""
}

variable "postiz_instance_type" {
  description = "Postiz brings Temporal and Elasticsearch; Elasticsearch alone wants around 2GB, so 8GB is the floor."
  type        = string
  default     = "t3.large"
}
