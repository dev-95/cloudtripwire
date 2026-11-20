"""Parse kill switch trigger events into what to act on. Pure: no AWS calls.

Event shapes (checked 2026-11-19; details in docs/killswitch.md):
- AWS Health envelope: https://docs.aws.amazon.com/health/latest/ug/aws-health-events-eventbridge-schema.html
  AWS_RISK_CREDENTIALS_EXPOSED puts the key ID in detail.affectedEntities[].entityValue and
  carries no user name (AWS sample: github.com/aws/aws-health-tools, automated-actions/
  AWS_RISK_CREDENTIALS_EXPOSED). The handler looks the user up from the key.
- GuardDuty envelope: https://docs.aws.amazon.com/guardduty/latest/ug/guardduty_findings_eventbridge.html
  Finding fields: https://docs.aws.amazon.com/guardduty/latest/APIReference/API_Finding.html,
  API_Resource.html, API_AccessKeyDetails.html, API_InstanceDetails.html
"""

import json
import logging
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

HEALTH_EVENT_CODE = "AWS_RISK_CREDENTIALS_EXPOSED"
GUARDDUTY_PREFIXES = (
    "CryptoCurrency:EC2/",
    "UnauthorizedAccess:IAMUser/InstanceCredentialExfiltration",
)


@dataclass(frozen=True)
class KillTarget:
    source: Literal["health", "guardduty"]
    finding_type: str  # Health eventTypeCode or GuardDuty finding type
    account_id: str | None
    region: str | None  # where the event/finding was generated
    access_key_id: str | None  # None for EC2 findings (no key in the finding)
    user_name: str | None  # None for Health (look it up); role name for AssumedRole keys
    user_type: str | None  # GuardDuty userType, e.g. "IAMUser" or "AssumedRole"
    instance_id: str | None  # GuardDuty EC2 findings only
    sample: bool = False  # GuardDuty sample finding: alert only, never act


def mask_key(key_id: str | None) -> str:
    """Show only the last 4 characters of an access key ID."""
    return f"****{key_id[-4:]}" if key_id else "none"


def parse_event(event: Any) -> KillTarget | None:
    """Return what to act on, or None (logged) for unknown or malformed events. Never raises."""
    try:
        source = event.get("source")
        if source == "aws.health":
            return _parse_health(event)
        if source == "aws.guardduty":
            return _parse_guardduty(event)
        _skip("unknown_source", source=source)
    except (AttributeError, TypeError, KeyError, IndexError) as e:
        _skip("malformed", error=type(e).__name__)
    return None


def _parse_health(event: dict) -> KillTarget | None:
    detail = event["detail"]
    code = detail.get("eventTypeCode")
    if code != HEALTH_EVENT_CODE:
        _skip("unhandled_health_event", event_type=code)
        return None
    keys = [e.get("entityValue") for e in detail.get("affectedEntities") or []]
    key_id = next((k for k in keys if isinstance(k, str) and k), None)
    if key_id is None:
        _skip("no_access_key", event_type=code)
        return None
    return KillTarget(
        source="health",
        finding_type=code,
        account_id=event.get("account"),
        region=event.get("region"),
        access_key_id=key_id,
        user_name=None,
        user_type=None,
        instance_id=None,
    )


def _parse_guardduty(event: dict) -> KillTarget | None:
    detail = event["detail"]
    finding_type = detail.get("type")
    if not isinstance(finding_type, str) or not finding_type.startswith(GUARDDUTY_PREFIXES):
        _skip("unhandled_guardduty_finding", event_type=finding_type)
        return None
    resource = detail.get("resource") or {}
    key = resource.get("accessKeyDetails") or {}
    instance = resource.get("instanceDetails") or {}
    target = KillTarget(
        source="guardduty",
        finding_type=finding_type,
        account_id=detail.get("accountId") or event.get("account"),
        region=detail.get("region") or event.get("region"),
        access_key_id=key.get("accessKeyId") or None,
        user_name=key.get("userName") or None,
        user_type=key.get("userType") or None,
        instance_id=instance.get("instanceId") or None,
        sample=is_sample(detail),
    )
    if target.sample:
        return target  # always alerted, even without a key or instance
    if target.access_key_id is None and target.instance_id is None:
        _skip("nothing_to_act_on", event_type=finding_type)
        return None
    return target


def is_sample(detail: dict) -> bool:
    """GuardDuty sample findings (CreateSampleFindings, console "Generate sample findings")
    carry detail.service.additionalInfo.sample = true. Their key and instance are fake or
    the user's own, so acting on them would stop real things for a test."""
    info = (detail.get("service") or {}).get("additionalInfo") or {}
    return info.get("sample") in (True, "true", "True")


def _skip(reason: str, **fields: Any) -> None:
    logger.warning(
        json.dumps({"event": "killswitch_event_skipped", "reason": reason, **fields}, default=str)
    )
