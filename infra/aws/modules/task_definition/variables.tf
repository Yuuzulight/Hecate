variable "family" {
  type = string
}

variable "image" {
  type = string
}

variable "command" {
  type    = list(string)
  default = null
}

variable "cpu" {
  type = number
}

variable "memory" {
  type = number
}

variable "execution_role_arn" {
  type = string
}

variable "task_role_arn" {
  type = string
}

variable "log_group_name" {
  type = string
}

variable "aws_region" {
  type = string
}

variable "environment" {
  type    = list(object({ name = string, value = string }))
  default = []
}

variable "secrets" {
  type    = list(object({ name = string, valueFrom = string }))
  default = []
}

variable "port_mappings" {
  type = list(object({
    containerPort = number
    protocol      = string
  }))
  default = []
}
