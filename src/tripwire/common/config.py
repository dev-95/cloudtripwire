"""Read env vars, validate, defaults."""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

MODES = ("observe", "enforce")
CHANNELS = ("telegram", "slack", "email")
NOT_YET_AVAILABLE = {"slack": "v0.2"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    mode: str = "observe"
    killswitch_mode: str = "observe"
    regions: tuple[str, ...] = ()  # empty = all enabled regions
    sweep_schedule: str = "cron(0 13 * * ? *)"
    ttl_schedule: str = "rate(1 hour)"
    lab_mode: bool = False
    lab_grace_hours: int = 24
    snapshot_max_age_days: int = 90
    stopped_instance_days: int = 7
    nat_idle_days: int = 7
    killswitch_lookback_minutes: int = 60
    channels: tuple[str, ...] = ("telegram",)
    alert_email: str = ""
    tags: Mapping[str, str] = field(default_factory=dict)
    # Set by Terraform on every Lambda; not user-facing variables.
    state_table: str = ""
    alert_topic_arn: str = ""


_INT_FIELDS = (
    "lab_grace_hours",
    "snapshot_max_age_days",
    "stopped_instance_days",
    "nat_idle_days",
    "killswitch_lookback_minutes",
)


def _csv(value: str) -> tuple[str, ...]:
    return tuple(p.strip().lower() for p in value.split(",") if p.strip())


def load_config(env: Mapping[str, str] = os.environ) -> Config:
    """Build Config from uppercase env vars. Raises ConfigError listing every problem."""
    defaults = Config()
    errors: list[str] = []
    values: dict = {}

    def get(name: str) -> str | None:
        v = env.get(name.upper())
        return None if v is None else v.strip()

    for name in ("mode", "killswitch_mode"):
        v = (get(name) or getattr(defaults, name)).lower()
        if v not in MODES:
            errors.append(f"{name.upper()} must be one of {MODES}, got {v!r}")
        values[name] = v

    for name in ("sweep_schedule", "ttl_schedule", "alert_email", "state_table", "alert_topic_arn"):
        if (v := get(name)) is not None:
            values[name] = v

    if (v := get("regions")) is not None:
        values["regions"] = _csv(v)

    if (v := get("channels")) is not None:
        values["channels"] = _csv(v)
    channels = values.get("channels", defaults.channels)
    if not channels:
        errors.append("CHANNELS must list at least one channel")
    if unknown := [c for c in channels if c not in CHANNELS]:
        errors.append(f"CHANNELS has unknown values {unknown}; allowed {CHANNELS}")
    for c in channels:
        if c in NOT_YET_AVAILABLE:
            errors.append(f"CHANNELS: {c!r} is not available until {NOT_YET_AVAILABLE[c]}")
    if "email" in channels and "@" not in values.get("alert_email", ""):
        errors.append("ALERT_EMAIL is required when CHANNELS includes 'email'")

    if (v := get("lab_mode")) is not None:
        if v.lower() not in ("true", "false"):
            errors.append(f"LAB_MODE must be 'true' or 'false', got {v!r}")
        values["lab_mode"] = v.lower() == "true"

    for name in _INT_FIELDS:
        if (v := get(name)) is None:
            continue
        try:
            n = int(v)
        except ValueError:
            n = 0
        if n <= 0:
            errors.append(f"{name.upper()} must be a positive integer, got {v!r}")
        values[name] = n

    if v := get("tags"):
        try:
            tags = json.loads(v)
            if not isinstance(tags, dict):
                raise ValueError
            values["tags"] = tags
        except ValueError:
            errors.append(f"TAGS must be a JSON object, got {v!r}")

    if errors:
        raise ConfigError("Invalid CloudTripwire config:\n- " + "\n- ".join(errors))
    return Config(**values)
