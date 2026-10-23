variable "mode" {
  description = "observe (only report what would be done) or enforce (stop expired resources). Applies to sweep and TTL."
  type        = string
  default     = "observe"

  validation {
    condition     = contains(["observe", "enforce"], var.mode)
    error_message = "mode must be \"observe\" or \"enforce\"."
  }
}

variable "killswitch_mode" {
  description = "observe or enforce for the leaked-key kill switch (used from M2)."
  type        = string
  default     = "observe"

  validation {
    condition     = contains(["observe", "enforce"], var.killswitch_mode)
    error_message = "killswitch_mode must be \"observe\" or \"enforce\"."
  }
}

variable "regions" {
  description = "Limit scanning to these regions. Empty list = every region enabled in the account."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for r in var.regions : can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", r))])
    error_message = "regions must be AWS region names like \"us-east-1\"."
  }
}

variable "sweep_schedule" {
  description = "EventBridge schedule for the daily zombie sweep (UTC)."
  type        = string
  default     = "cron(0 13 * * ? *)"

  validation {
    condition     = can(regex("^(cron|rate)\\(.+\\)$", var.sweep_schedule))
    error_message = "sweep_schedule must be an EventBridge cron(...) or rate(...) expression."
  }
}

variable "ttl_schedule" {
  description = "EventBridge schedule for TTL enforcement (used from M2)."
  type        = string
  default     = "rate(1 hour)"

  validation {
    condition     = can(regex("^(cron|rate)\\(.+\\)$", var.ttl_schedule))
    error_message = "ttl_schedule must be an EventBridge cron(...) or rate(...) expression."
  }
}

variable "lab_mode" {
  description = "Treat running EC2/RDS with no ttl tag as expired after lab_grace_hours (used from M2). For dedicated learning accounts."
  type        = bool
  default     = false
}

variable "lab_grace_hours" {
  description = "Grace period in hours for lab mode."
  type        = number
  default     = 24

  validation {
    condition     = var.lab_grace_hours > 0 && floor(var.lab_grace_hours) == var.lab_grace_hours
    error_message = "lab_grace_hours must be a positive whole number."
  }
}

variable "snapshot_max_age_days" {
  description = "Snapshots older than this many days are flagged."
  type        = number
  default     = 90

  validation {
    condition     = var.snapshot_max_age_days > 0 && floor(var.snapshot_max_age_days) == var.snapshot_max_age_days
    error_message = "snapshot_max_age_days must be a positive whole number."
  }
}

variable "stopped_instance_days" {
  description = "Instances stopped longer than this many days are flagged."
  type        = number
  default     = 7

  validation {
    condition     = var.stopped_instance_days > 0 && floor(var.stopped_instance_days) == var.stopped_instance_days
    error_message = "stopped_instance_days must be a positive whole number."
  }
}

variable "nat_idle_days" {
  description = "NAT gateway traffic lookback in days."
  type        = number
  default     = 7

  validation {
    condition     = var.nat_idle_days > 0 && floor(var.nat_idle_days) == var.nat_idle_days
    error_message = "nat_idle_days must be a positive whole number."
  }
}

variable "killswitch_lookback_minutes" {
  description = "Instances launched within this many minutes before a leak alert get stopped (used from M2)."
  type        = number
  default     = 60

  validation {
    condition     = var.killswitch_lookback_minutes > 0 && floor(var.killswitch_lookback_minutes) == var.killswitch_lookback_minutes
    error_message = "killswitch_lookback_minutes must be a positive whole number."
  }
}

variable "channels" {
  description = "Alert channels: any of \"telegram\", \"email\". Slack arrives in v0.2."
  type        = list(string)
  default     = ["telegram"]

  validation {
    condition     = length(var.channels) > 0 && alltrue([for c in var.channels : contains(["telegram", "email"], c)])
    error_message = "channels must be a non-empty list of \"telegram\" and/or \"email\" (\"slack\" is not available until v0.2)."
  }
}

variable "alert_email" {
  description = "Email address subscribed to the alerts topic. AWS sends a confirmation email you must accept. Required when channels includes \"email\"."
  type        = string
  default     = ""

  validation {
    condition     = var.alert_email == "" || can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "alert_email must be empty or a valid email address."
  }
}

variable "cli_profile" {
  description = "AWS CLI profile for the account you deploy to. Only used to add --profile to the ssm_setup_commands output; leave empty if you use the default profile."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to every CloudTripwire resource."
  type        = map(string)
  default     = {}
}
