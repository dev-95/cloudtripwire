"""Per-region boto3 client bundle passed to detectors."""

from dataclasses import dataclass
from typing import Any


@dataclass
class RegionClients:
    """boto3 clients for one region; tests inject moto clients."""

    ec2: Any
    elbv2: Any
    cloudwatch: Any
