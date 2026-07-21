variable "environment" {
  description = "Deployment environment."
  type        = string
}

variable "region" {
  description = "AWS region to use."
  type        = string
}

variable "docker_tag" {
  description = "Image tag to deploy."
  type        = string
}

variable "service_count" {
  description = "Number of ECS tasks to run."
  type        = number
  default     = 1
}

variable "cpu" {
  description = "CPU units to allocate."
  type        = number
  default     = 256
}

variable "memory" {
  description = "Memory to allocate in MB."
  type        = number
  default     = 512
}

variable "enable_alarms" {
  description = "Whether to enable CloudWatch alarms."
  type        = bool
  default     = false
}

variable "enable_observability_alerts" {
  description = "Whether to send alarms to the observability topic."
  type        = bool
  default     = false
}
