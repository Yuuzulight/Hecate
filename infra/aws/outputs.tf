output "cluster_arn" {
  description = "ECS cluster ARN."
  value       = aws_ecs_cluster.main.arn
}

output "subnet_ids" {
  description = "Subnets used by ECS tasks."
  value       = aws_subnet.public[*].id
}

output "ecs_security_group_id" {
  description = "Security group for ECS tasks."
  value       = aws_security_group.ecs.id
}

output "database_endpoint" {
  description = "Aurora PostgreSQL cluster endpoint."
  value       = data.aws_rds_cluster.postgres.endpoint
}

output "database_secret_arn" {
  description = "Express Aurora uses IAM authentication rather than a stored password."
  value       = null
}

output "exports_bucket" {
  description = "S3 bucket for Hecate exported artifacts and backups."
  value       = aws_s3_bucket.exports.bucket
}

output "api_credentials_secret_arn" {
  description = "Secrets Manager ARN for source and RAG API credentials."
  value       = aws_secretsmanager_secret.api_credentials.arn
}

output "daily_task_definition_arn" {
  value = aws_ecs_task_definition.daily.arn
}

output "dbt_task_definition_arn" {
  value = aws_ecs_task_definition.dbt.arn
}

output "embed_task_definition_arn" {
  value = aws_ecs_task_definition.embed.arn
}

output "forecast_task_definition_arn" {
  value = aws_ecs_task_definition.forecast.arn
}

output "rag_url" {
  description = "RAG API URL when enable_rag_api=true."
  value       = var.enable_rag_api ? "http://${aws_lb.rag[0].dns_name}" : null
}
