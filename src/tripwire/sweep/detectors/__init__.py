"""Detector interface and registry.

Every detector is a read-only function:
    detect(clients: RegionClients, region: str, config: Config, now: datetime) -> list[Finding]
It skips resources tagged tripwire=ignore and never calls stop/delete APIs.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from tripwire.common.config import Config
from tripwire.common.models import Finding


@dataclass
class RegionClients:
    """boto3 clients for one region; tests inject moto clients."""

    ec2: Any
    elbv2: Any
    cloudwatch: Any


Detect = Callable[[RegionClients, str, Config, datetime], list[Finding]]

# Imported after RegionClients so detector modules can import it from here.
from tripwire.sweep.detectors import ebs_unattached, eip_unassociated  # noqa: E402

DETECTORS: list[tuple[str, Detect]] = [
    ("ebs_unattached", ebs_unattached.detect),
    ("eip_unassociated", eip_unassociated.detect),
]
