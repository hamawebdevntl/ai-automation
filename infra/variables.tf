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

variable "mpt_base_url" {
  description = "Internal URL of the MoneyPrinterTurbo service. Never public: its artifacts sit behind its own API key, and Postiz could not reach it anyway."
  type        = string
}

variable "postiz_base_url" {
  type = string
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

variable "vpc_subnet_ids" {
  description = "Private subnets for the trends task."
  type        = list(string)
  default     = []
}

variable "vpc_security_group_ids" {
  type    = list(string)
  default = []
}
