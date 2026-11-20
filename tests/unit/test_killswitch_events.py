import copy
import json
import logging
from pathlib import Path

import pytest

from tripwire.killswitch.events import KillTarget, mask_key, parse_event

FIXTURES = Path(__file__).parent.parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_health_credentials_exposed():
    assert parse_event(load("health_credentials_exposed.json")) == KillTarget(
        source="health",
        finding_type="AWS_RISK_CREDENTIALS_EXPOSED",
        account_id="123456789012",
        region="us-east-1",
        access_key_id="AKIAEXAMPLE0000TEST",
        user_name=None,
        user_type=None,
        instance_id=None,
    )


def test_guardduty_crypto_has_instance_no_key():
    assert parse_event(load("guardduty_crypto.json")) == KillTarget(
        source="guardduty",
        finding_type="CryptoCurrency:EC2/BitcoinTool.B!DNS",
        account_id="123456789012",
        region="us-east-1",
        access_key_id=None,
        user_name=None,
        user_type=None,
        instance_id="i-0123456789abcdef0",
    )


def test_guardduty_exfiltration_has_role_session_key():
    assert parse_event(load("guardduty_exfiltration.json")) == KillTarget(
        source="guardduty",
        finding_type="UnauthorizedAccess:IAMUser/InstanceCredentialExfiltration.OutsideAWS",
        account_id="123456789012",
        region="us-east-1",
        access_key_id="ASIAEXAMPLE0000TEST",
        user_name="example-instance-role",
        user_type="AssumedRole",
        instance_id=None,
    )


def test_health_event_without_key_id_is_skipped(caplog):
    event = load("health_credentials_exposed.json")
    event["detail"]["affectedEntities"] = []
    with caplog.at_level(logging.WARNING):
        assert parse_event(event) is None
    assert "no_access_key" in caplog.text


def test_guardduty_exfiltration_without_key_is_skipped():
    event = load("guardduty_exfiltration.json")
    del event["detail"]["resource"]["accessKeyDetails"]
    assert parse_event(event) is None


@pytest.mark.parametrize(
    "event",
    [
        None,
        "not an event",
        [],
        {},
        {"source": "aws.health"},  # no detail
        {"source": "aws.health", "detail": "oops"},
        {"source": "aws.guardduty", "detail": None},
        {"source": "aws.guardduty", "detail": {"type": "CryptoCurrency:EC2/X", "resource": []}},
        {
            "source": "aws.health",
            "detail": {
                "eventTypeCode": "AWS_RISK_CREDENTIALS_EXPOSED",
                "affectedEntities": ["AKIAEXAMPLE0000TEST"],
            },
        },
    ],
)
def test_malformed_events_return_none(event, caplog):
    with caplog.at_level(logging.WARNING):
        assert parse_event(event) is None
    assert "killswitch_event_skipped" in caplog.text


def test_unknown_source_and_types_are_skipped():
    health = load("health_credentials_exposed.json")
    health["detail"]["eventTypeCode"] = "AWS_EC2_INSTANCE_RETIREMENT_SCHEDULED"
    guardduty = load("guardduty_crypto.json")
    guardduty["detail"]["type"] = "Recon:EC2/PortProbeUnprotectedPort"
    other = copy.deepcopy(health) | {"source": "aws.ec2"}
    assert parse_event(health) is None
    assert parse_event(guardduty) is None
    assert parse_event(other) is None


def test_logs_never_contain_the_key(caplog):
    event = load("health_credentials_exposed.json")
    event["detail"]["eventTypeCode"] = "SOMETHING_ELSE"
    with caplog.at_level(logging.DEBUG):
        parse_event(event)
        parse_event(load("health_credentials_exposed.json"))
    assert "AKIAEXAMPLE0000TEST" not in caplog.text


def test_mask_key():
    assert mask_key("AKIAEXAMPLE0000TEST") == "****TEST"
    assert mask_key(None) == "none"
