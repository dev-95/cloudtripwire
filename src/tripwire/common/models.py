"""Dataclasses: Finding, Action."""

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal


@dataclass
class Finding:
    detector: str  # "ebs_unattached"
    resource_id: str  # "vol-0a1b..."
    region: str
    monthly_cost_usd: float
    age_days: int | None  # days since first seen by CloudTripwire (or creation time)
    reason: str  # human sentence
    fix_command: str  # suggested AWS CLI command the USER can run (never run by us)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Action:
    action: Literal["stop_instance", "stop_db", "deactivate_key"]
    resource_id: str
    region: str
    mode: Literal["observe", "enforce"]
    performed: bool
    timestamp: datetime  # UTC
    reason: str
    undo_command: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"timestamp": self.timestamp.isoformat()}
