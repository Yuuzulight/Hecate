locals {
  container_name = replace(var.family, "/[^A-Za-z0-9_-]/", "-")

  container = merge({
    name      = local.container_name
    image     = var.image
    essential = true

    environment  = var.environment
    secrets      = var.secrets
    portMappings = var.port_mappings

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = var.log_group_name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = local.container_name
      }
    }
    },
    var.command == null ? {} : {
      command = var.command
  })
}

output "family" {
  value = var.family
}

output "cpu" {
  value = tostring(var.cpu)
}

output "memory" {
  value = tostring(var.memory)
}

output "execution_role_arn" {
  value = var.execution_role_arn
}

output "task_role_arn" {
  value = var.task_role_arn
}

output "container_name" {
  value = local.container_name
}

output "container_definitions" {
  value = jsonencode([local.container])
}
