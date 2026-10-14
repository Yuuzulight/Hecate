resource "aws_scheduler_schedule" "this" {
  name                         = var.name
  schedule_expression          = var.schedule_expression
  schedule_expression_timezone = var.schedule_timezone
  state                        = var.enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = var.cluster_arn
    role_arn = var.scheduler_role_arn

    ecs_parameters {
      task_definition_arn = var.task_definition_arn
      launch_type         = "FARGATE"

      network_configuration {
        subnets          = var.subnet_ids
        security_groups  = var.security_group_ids
        assign_public_ip = true
      }
    }

    retry_policy {
      maximum_retry_attempts = 2
    }
  }
}
