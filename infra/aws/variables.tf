variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "ap-southeast-2"
}

variable "project" {
  description = "Name prefix for AWS resources."
  type        = string
  default     = "hecate"
}

variable "environment" {
  description = "Environment label."
  type        = string
  default     = "prod"
}

variable "db_name" {
  description = "Hecate database name."
  type        = string
  default     = "hecate"
}

variable "db_username" {
  description = "Aurora PostgreSQL master username."
  type        = string
  default     = "dataflow"
}

variable "aurora_engine_version" {
  description = "Aurora PostgreSQL engine version."
  type        = string
  default     = "16.15"
}

variable "aurora_min_capacity" {
  description = "Minimum Aurora Serverless v2 ACUs. Use 0 for auto-pause/scale-to-zero."
  type        = number
  default     = 0
}

variable "aurora_max_capacity" {
  description = "Maximum Aurora Serverless v2 ACUs."
  type        = number
  default     = 1
}

variable "aurora_seconds_until_auto_pause" {
  description = "Seconds of inactivity before Aurora pauses when min capacity is 0."
  type        = number
  default     = 900
}

variable "db_backup_retention_days" {
  description = "Aurora automated backup retention."
  type        = number
  default     = 7
}

variable "export_retention_days" {
  description = "Days to keep exported artifacts in S3 before moving them to cheaper storage."
  type        = number
  default     = 30
}

variable "pipeline_image" {
  description = "Container image for the extraction pipeline and metrics exporter."
  type        = string
  default     = "ghcr.io/yuuzulight/hecate:2.0.0"
}

variable "dbt_image" {
  description = "Container image for dbt jobs."
  type        = string
  default     = "ghcr.io/yuuzulight/hecate-dbt:2.1.0"
}

variable "rag_image" {
  description = "Container image for RAG API and embeddings."
  type        = string
  default     = "ghcr.io/yuuzulight/hecate-rag:2.0.0"
}

variable "forecast_image" {
  description = "Container image for forecast jobs."
  type        = string
  default     = "ghcr.io/yuuzulight/hecate-forecast:2.0.0"
}

variable "enable_schedules" {
  description = "Whether EventBridge schedules should run ECS jobs."
  type        = bool
  default     = true
}

variable "enable_embed_schedule" {
  description = "Whether to schedule the optional embedding job."
  type        = bool
  default     = false
}

variable "enable_forecast_schedule" {
  description = "Whether to schedule the optional forecast job."
  type        = bool
  default     = false
}

variable "enable_rag_api" {
  description = "Whether to run the RAG API behind a public ALB."
  type        = bool
  default     = false
}

variable "schedule_timezone" {
  description = "Time zone for EventBridge schedules."
  type        = string
  default     = "Asia/Singapore"
}

variable "rag_provider" {
  description = "RAG provider: gemini, anthropic, or openai."
  type        = string
  default     = "gemini"

  validation {
    condition     = contains(["gemini", "anthropic", "openai"], lower(var.rag_provider))
    error_message = "rag_provider must be gemini, anthropic, or openai."
  }
}

variable "rag_allowed_cidr_blocks" {
  description = "CIDR blocks allowed to reach the RAG ALB."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "github_token" {
  description = "Optional GitHub token for higher extraction rate limits."
  type        = string
  default     = ""
  sensitive   = true
}

variable "gitlab_token" {
  description = "Optional GitLab token."
  type        = string
  default     = ""
  sensitive   = true
}

variable "google_api_key" {
  description = "Google AI Studio API key for RAG_PROVIDER=gemini."
  type        = string
  default     = ""
  sensitive   = true
}

variable "anthropic_api_key" {
  description = "Anthropic API key for RAG_PROVIDER=anthropic."
  type        = string
  default     = ""
  sensitive   = true
}

variable "openai_api_key" {
  description = "OpenAI API key for embeddings and RAG_PROVIDER=openai."
  type        = string
  default     = ""
  sensitive   = true
}

variable "batch_size" {
  description = "Records pulled per source per run."
  type        = number
  default     = 500
}
