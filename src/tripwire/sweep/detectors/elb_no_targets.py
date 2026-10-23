"""Detector: load balancers with no targets."""

from collections import defaultdict
from datetime import datetime

from tripwire.common import pricing
from tripwire.common.clients import RegionClients
from tripwire.common.config import Config
from tripwire.common.models import Finding
from tripwire.common.tags import is_ignored, tags_to_dict

NAME = "elb_no_targets"

LB_TYPES = ("application", "network")  # gateway LBs front appliances; out of scope
TAGS_BATCH = 20  # DescribeTags accepts at most 20 ARNs per call


def _ignored_arns(elbv2, arns: list[str]) -> set[str]:
    ignored = set()
    for i in range(0, len(arns), TAGS_BATCH):
        resp = elbv2.describe_tags(ResourceArns=arns[i : i + TAGS_BATCH])
        for desc in resp["TagDescriptions"]:
            if is_ignored(tags_to_dict(desc.get("Tags"))):
                ignored.add(desc["ResourceArn"])
    return ignored


def _target_groups_by_lb(elbv2) -> dict[str, list[str]]:
    by_lb = defaultdict(list)
    for page in elbv2.get_paginator("describe_target_groups").paginate():
        for tg in page["TargetGroups"]:
            for lb_arn in tg.get("LoadBalancerArns", []):
                by_lb[lb_arn].append(tg["TargetGroupArn"])
    return by_lb


def _has_targets(elbv2, tg_arn: str) -> bool:
    # Every registered target is listed, whatever its health (unused, draining, ...).
    return bool(elbv2.describe_target_health(TargetGroupArn=tg_arn)["TargetHealthDescriptions"])


def detect(clients: RegionClients, region: str, config: Config, now: datetime) -> list[Finding]:
    lbs = [
        lb
        for page in clients.elbv2.get_paginator("describe_load_balancers").paginate()
        for lb in page["LoadBalancers"]
        if lb["Type"] in LB_TYPES
    ]
    if not lbs:
        return []
    ignored = _ignored_arns(clients.elbv2, [lb["LoadBalancerArn"] for lb in lbs])
    tgs_by_lb = _target_groups_by_lb(clients.elbv2)

    findings = []
    for lb in lbs:
        arn, lb_type = lb["LoadBalancerArn"], lb["Type"]
        if arn in ignored:
            continue
        tgs = tgs_by_lb.get(arn, [])
        if any(_has_targets(clients.elbv2, tg) for tg in tgs):
            continue
        detail = "has no target groups" if not tgs else "has no registered targets"
        findings.append(
            Finding(
                detector=NAME,
                resource_id=arn,
                region=region,
                monthly_cost_usd=pricing.load_balancer_monthly(lb_type),
                age_days=(now - lb["CreatedTime"]).days,
                reason=f"{lb_type} load balancer {lb['LoadBalancerName']} {detail}",
                fix_command=(
                    f"aws elbv2 delete-load-balancer --load-balancer-arn {arn} --region {region}"
                ),
            )
        )
    return findings
