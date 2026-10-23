"""Sweep Lambda entry point.

Daily: run every detector in every enabled region, record findings in DynamoDB,
resolve what's gone, send one digest. Regions run sequentially in v1; the logged
duration_s tells us whether that needs to change.
"""

import json
import logging
import time
from datetime import UTC, datetime
from functools import cache
from typing import Any

import boto3

from tripwire.common import notify, regions, state
from tripwire.common.clients import RegionClients
from tripwire.common.config import load_config
from tripwire.common.models import Finding
from tripwire.sweep.detectors import DETECTORS

logger = logging.getLogger()
logger.setLevel(logging.INFO)

MONDAY = 0  # weekly all-clear day (UTC)


@cache
def _dynamodb():
    return boto3.resource("dynamodb")


def _now() -> datetime:
    return datetime.now(UTC)


def _region_clients(region: str) -> RegionClients:
    return RegionClients(
        ec2=regions.get_client("ec2", region),
        elbv2=regions.get_client("elbv2", region),
        cloudwatch=regions.get_client("cloudwatch", region),
    )


def run_detectors(
    region_names: list[str], config, now: datetime
) -> tuple[list[Finding], list[tuple[str, str]], list[tuple[str, str]]]:
    """Return (findings, succeeded pairs, failed pairs); pairs are (region, detector)."""
    findings: list[Finding] = []
    ok: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    for region in region_names:
        clients = _region_clients(region)
        for name, detect in DETECTORS:
            try:
                found = detect(clients, region, config, now)
            except Exception as e:  # one broken check must not stop the others
                failed.append((region, name))
                logger.error(
                    json.dumps(
                        {
                            "event": "check_failed",
                            "region": region,
                            "detector": name,
                            "error": type(e).__name__,
                            # Detectors only call read-only AWS APIs; no secrets in scope.
                            "message": str(e)[:500],
                        }
                    )
                )
                continue
            ok.append((region, name))
            findings.extend(found)
    return findings, ok, failed


def build_message(
    findings: list[Finding], mode: str, checks_failed: int, now: datetime
) -> str | None:
    """Digest if anything was found or failed; weekly all-clear on Mondays; else nothing."""
    if findings or checks_failed or now.weekday() == MONDAY:
        return notify.format_digest(findings, mode, checks_failed)
    return None


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    started = time.monotonic()
    now = _now()
    config = load_config()
    region_names = regions.enabled_regions(config.regions)

    findings, ok, failed = run_detectors(region_names, config, now)

    table = _dynamodb().Table(config.state_table)
    findings = state.record_findings(table, findings, now)
    resolved = state.resolve_missing(table, findings, ok, now)

    for f in findings:
        logger.info(json.dumps({"event": "finding"} | f.to_dict()))  # fix commands live here
    for item in resolved:
        logger.info(
            json.dumps(
                {
                    "event": "resolved",
                    "detector": item["sk"],
                    "resource": item["resource_id"],
                    "region": item["region"],
                }
            )
        )

    message = build_message(findings, config.mode, len(failed), now)
    if message:
        sent = notify.send(
            message,
            message.splitlines()[0],
            config,
            regions.get_client("ssm"),
            regions.get_client("sns"),
        )
        logger.info(json.dumps({"event": "notify_result", "channels": sent}))

    summary = {
        "regions_scanned": len(region_names),
        "checks_failed": len(failed),
        "findings_count": len(findings),
        "total_monthly_cost_usd": round(sum((f.monthly_cost_usd for f in findings), 0.0), 2),
        "duration_s": round(time.monotonic() - started, 2),
    }
    logger.info(json.dumps({"event": "sweep_summary"} | summary))
    return summary
