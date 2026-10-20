"""List enabled regions."""

import json
import logging
from collections.abc import Sequence
from functools import cache

import boto3

logger = logging.getLogger(__name__)

_ENABLED = ("opt-in-not-required", "opted-in")


@cache
def get_client(service: str, region: str | None = None):
    """Cached boto3 client. None region = the Lambda's own region (AWS_REGION)."""
    # ponytail: cache lives for the warm container, not one invocation; clients are safe to reuse.
    return boto3.client(service, region_name=region)


def enabled_regions(configured: Sequence[str]) -> list[str]:
    """Regions enabled for this account, limited to `configured` when it is non-empty."""
    resp = get_client("ec2").describe_regions(AllRegions=True)  # not paginated
    enabled = {r["RegionName"] for r in resp["Regions"] if r.get("OptInStatus") in _ENABLED}
    if not configured:
        return sorted(enabled)
    if missing := sorted(set(configured) - enabled):
        logger.warning(json.dumps({"event": "configured_region_not_enabled", "regions": missing}))
    return sorted(enabled.intersection(configured))
