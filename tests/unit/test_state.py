from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.helpers import dynamodb
from tripwire.common import state
from tripwire.common.models import Action, Finding

T0 = datetime(2026, 10, 1, 13, 0, tzinfo=UTC)


@pytest.fixture
def table(aws):
    return dynamodb().create_table(
        TableName="cloudtripwire-state",
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def finding(resource_id: str = "vol-1", region: str = "us-east-1", cost: float = 1.6) -> Finding:
    return Finding(
        detector="ebs_unattached",
        resource_id=resource_id,
        region=region,
        monthly_cost_usd=cost,
        age_days=None,
        reason="r",
        fix_command="f",
    )


def get(table, f: Finding) -> dict:
    key = {"pk": f"FINDING#{f.region}#{f.resource_id}", "sk": f.detector}
    return table.get_item(Key=key)["Item"]


# --- findings ---


def test_record_new_finding(table):
    [out] = state.record_findings(table, [finding()], T0)

    assert out.age_days == 0
    item = get(table, finding())
    assert item["first_seen"] == item["last_seen"] == T0.isoformat()
    assert item["status"] == "open"
    assert item["monthly_cost_usd"] == Decimal("1.6")  # stored as Decimal, not float


def test_record_keeps_first_seen(table):
    state.record_findings(table, [finding()], T0)
    [out] = state.record_findings(table, [finding(cost=2.0)], T0 + timedelta(days=3, hours=1))

    assert out.age_days == 3
    item = get(table, finding())
    assert item["first_seen"] == T0.isoformat()
    assert item["last_seen"] == (T0 + timedelta(days=3, hours=1)).isoformat()
    assert item["monthly_cost_usd"] == Decimal("2")


def test_open_findings_reads_floats(table):
    state.record_findings(table, [finding(cost=32.85)], T0)

    [item] = state.open_findings(table)

    assert item["monthly_cost_usd"] == 32.85
    assert isinstance(item["monthly_cost_usd"], float)


def test_resolve_missing_resolves_only_unflagged(table):
    keep, gone = finding("vol-keep"), finding("vol-gone")
    state.record_findings(table, [keep, gone], T0)
    later = T0 + timedelta(days=1)

    resolved = state.resolve_missing(table, [keep], ["us-east-1"], later)

    assert [r["resource_id"] for r in resolved] == ["vol-gone"]
    assert get(table, gone)["status"] == "resolved"
    assert get(table, gone)["resolved_at"] == later.isoformat()
    assert get(table, keep)["status"] == "open"


def test_resolve_missing_never_touches_failed_region(table):
    broken = finding("vol-eu", region="eu-west-3")
    state.record_findings(table, [broken], T0)

    # eu-west-3 errored this run, so it's absent from scanned_regions and from current.
    resolved = state.resolve_missing(table, [], ["us-east-1"], T0 + timedelta(days=1))

    assert resolved == []
    assert get(table, broken)["status"] == "open"


def test_resolve_ignores_already_resolved(table):
    state.record_findings(table, [finding()], T0)
    state.resolve_missing(table, [], ["us-east-1"], T0 + timedelta(days=1))

    assert state.resolve_missing(table, [], ["us-east-1"], T0 + timedelta(days=2)) == []
    assert get(table, finding())["resolved_at"] == (T0 + timedelta(days=1)).isoformat()


def test_reopened_finding_starts_new_first_seen(table):
    state.record_findings(table, [finding()], T0)
    state.resolve_missing(table, [], ["us-east-1"], T0 + timedelta(days=1))
    back = T0 + timedelta(days=5)

    [out] = state.record_findings(table, [finding()], back)

    assert out.age_days == 0
    item = get(table, finding())
    assert item["status"] == "open"
    assert item["first_seen"] == back.isoformat()
    assert "resolved_at" not in item


# --- action log ---


def test_log_action_with_expiry(table):
    ts = datetime(2026, 10, 20, 18, 5, 30, tzinfo=UTC)
    action = Action(
        action="stop_instance",
        resource_id="i-0123456789abcdef0",
        region="us-east-1",
        mode="enforce",
        performed=True,
        timestamp=ts,
        reason="ttl 4h expired",
        undo_command="aws ec2 start-instances --instance-ids i-0123456789abcdef0",
    )

    state.log_action(table, action)

    item = table.get_item(
        Key={"pk": "ACTION#2026-10-20", "sk": f"{ts.isoformat()}#i-0123456789abcdef0"}
    )["Item"]
    assert item["performed"] is True
    assert item["undo_command"] == action.undo_command
    assert item["expires_at"] == int((ts + timedelta(days=90)).timestamp())


# --- alert dedup ---


def test_should_alert_once_per_resource_and_type(table):
    assert state.should_alert(table, "i-1", "ttl_warning", T0) is True
    assert state.should_alert(table, "i-1", "ttl_warning", T0 + timedelta(hours=1)) is False
    assert state.should_alert(table, "i-1", "ttl_unreadable", T0) is True
    assert state.should_alert(table, "i-2", "ttl_warning", T0) is True
