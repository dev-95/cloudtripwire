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
from tripwire.sweep.detectors import (
    ebs_unattached,
    ec2_stopped_long,
    eip_unassociated,
    elb_no_targets,
    nat_idle,
    snapshots_old,
)

Detect = Callable[[RegionClients, str, Config, datetime], list[Finding]]

DETECTORS: list[tuple[str, Detect]] = [
    ("ebs_unattached", ebs_unattached.detect),
    ("eip_unassociated", eip_unassociated.detect),
    ("nat_idle", nat_idle.detect),
    ("elb_no_targets", elb_no_targets.detect),
    ("snapshots_old", snapshots_old.detect),
    ("ec2_stopped_long", ec2_stopped_long.detect),
]
