variable "name" {
  type = string
}

variable "enabled" {
  type = bool
}

variable "schedule_expression" {
  type = string
}

variable "schedule_timezone" {
  type    = string
  default = "UTC"
}

variable "cluster_arn" {
  type = string
}

variable "task_definition_arn" {
  type = string
}

variable "scheduler_role_arn" {
  type = string
}

variable "subnet_ids" {
  type = list(string)
}

variable "security_group_ids" {
  type = list(string)
}
