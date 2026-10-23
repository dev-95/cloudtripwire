"""Detector interface and registry.

Every detector is a read-only function:
    detect(clients: RegionClients, region: str, config: Config, now: datetime) -> list[Finding]
It skips resources tagged tripwire=ignore and never calls stop/delete APIs.
"""

from collections.abc import Callable
from datetime import datetime

from tripwire.common.clients import RegionClients
from tripwire.common.config import Config
from tripwire.common.models import Finding
from tripwire.sweep.detectors import ebs_unattached, eip_unassociated

Detect = Callable[[RegionClients, str, Config, datetime], list[Finding]]

DETECTORS: list[tuple[str, Detect]] = [
    ("ebs_unattached", ebs_unattached.detect),
    ("eip_unassociated", eip_unassociated.detect),
]
