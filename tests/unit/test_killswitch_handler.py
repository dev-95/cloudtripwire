import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from tests.helpers import client, create_state_table, dynamodb, launch_instance
from tripwire.common import notify, regions
from tripwire.common.tags import ASG_TAG
from tripwire.killswitch import handler
from tripwire.killswitch.handler import QUARANTINE_TAG, recent_instances

TABLE = "cloudtripwire-state"
REGION = "us-east-1"
FIXTURES = Path(__file__).parent.parent / "fixtures"
WRITE_CALLS = (("iam", "update_access_key"), ("ec2", "stop_instances"), ("ec2", "create_tags"))


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --- pure rules -------------------------------------------------------------

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def inst(iid: str, minutes_ago: int, tags: dict[str, str] | None = None, **kw) -> dict:
    return {
        "InstanceId": iid,
        "LaunchTime": NOW - timedelta(minutes=minutes_ago),
        "Tags": [{"Key": k, "Value": v} for k, v in (tags or {}).items()],
        **kw,
    }


def test_recent_instances_selects_by_launch_time_and_skips_ignored():
    found = recent_instances(
        [
            inst("i-new", 5),
            inst("i-edge", 60),
            inst("i-old", 61),
            inst("i-ignored", 5, {"Tripwire": "IGNORE"}),
            inst("i-asg", 5, {ASG_TAG: "web"}),
            inst("i-spot", 5, InstanceLifecycle="spot"),
        ],
        NOW - timedelta(minutes=60),
    )
    assert [i for i, _ in found] == ["i-new", "i-edge", "i-asg", "i-spot"]
    reasons = dict(found)
    assert reasons["i-new"] == ""
    assert "Auto Scaling group web" in reasons["i-asg"]
    assert "Spot" in reasons["i-spot"]


# --- handler with moto ------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def ks(aws, monkeypatch):
    """Handler wired to moto. Returns a namespace with sent messages, clock, spy, key."""
    monkeypatch.setenv("STATE_TABLE", TABLE)
    monkeypatch.setenv("REGIONS", REGION)
    monkeypatch.setattr(regions, "get_client", lambda s, r=None: client(s, r or REGION))
    monkeypatch.setattr(handler, "_dynamodb", dynamodb)
    clock = Clock()
    monkeypatch.setattr(handler, "_now", clock)
    create_state_table(TABLE)
    sent: list[str] = []
    monkeypatch.setattr(notify, "send", lambda msg, *a: sent.append(msg) or {"telegram": True})

    calls: list[str] = []
    for svc, name in WRITE_CALLS:
        c = client(svc, REGION)
        real = getattr(c, name)

        def spy(*a, _real=real, _name=name, **kw):
            calls.append(_name)
            return _real(*a, **kw)

        monkeypatch.setattr(c, name, spy)

    iam = client("iam", REGION)
    iam.create_user(UserName="bob")
    created = iam.create_access_key(UserName="bob")["AccessKey"]

    class NS:
        pass

    ns = NS()
    ns.sent, ns.clock, ns.calls = sent, clock, calls
    ns.key, ns.secret = created["AccessKeyId"], created["SecretAccessKey"]
    return ns


def health_event(key_id: str) -> dict:
    event = load("health_credentials_exposed.json")
    event["detail"]["affectedEntities"] = [{"entityValue": key_id}]
    return event


def key_status(key_id: str) -> str:
    keys = client("iam", REGION).list_access_keys(UserName="bob")["AccessKeyMetadata"]
    return next(k["Status"] for k in keys if k["AccessKeyId"] == key_id)


def launch(tags: dict[str, str] | None = None, **kwargs) -> tuple[str, datetime]:
    ec2 = client("ec2", REGION)
    if tags:
        tag_list = [{"Key": k, "Value": v} for k, v in tags.items()]
        kwargs["TagSpecifications"] = [{"ResourceType": "instance", "Tags": tag_list}]
    iid = launch_instance(ec2, **kwargs)
    inst = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
    return iid, inst["LaunchTime"]


def instance(iid: str) -> dict:
    resp = client("ec2", REGION).describe_instances(InstanceIds=[iid])
    return resp["Reservations"][0]["Instances"][0]


def actions() -> list[dict]:
    table = dynamodb().Table(TABLE)
    pk = f"ACTION#{datetime.now(UTC).date().isoformat()}"
    return table.query(KeyConditionExpression=Key("pk").eq(pk))["Items"]


def fail(*a, **kw):
    raise ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, "Op")


def test_enforce_deactivates_key_stops_recent_instance_logs_and_alerts(ks):
    iid, launched = launch()
    ks.clock.now = launched + timedelta(minutes=10)

    summary = handler.handler(health_event(ks.key), None)

    assert key_status(ks.key) == "Inactive"
    i = instance(iid)
    assert i["State"]["Name"] in ("stopping", "stopped")
    assert {t["Key"]: t["Value"] for t in i["Tags"]}[QUARANTINE_TAG] == "true"
    assert sorted(ks.calls) == ["create_tags", "stop_instances", "update_access_key"]
    assert summary["deactivated"] == 1 and summary["stopped"] == 1 and summary["failed"] == 0

    undo = {a["action"]: a["undo_command"] for a in actions()}
    assert undo["deactivate_key"] == (
        f"aws iam update-access-key --user-name bob --access-key-id {ks.key} --status Active"
    )
    assert (
        undo["stop_instance"] == f"aws ec2 start-instances --instance-ids {iid} --region {REGION}"
    )

    (msg,) = ks.sent
    assert msg.startswith(f"🚨 CloudTripwire kill switch: leaked key ****{ks.key[-4:]}")
    assert "Deactivated key" in msg and "(user bob)" in msg
    assert f"Stopped instance {iid} in {REGION}" in msg
    for step in ("Rotate the key", "CloudTrail", "IAM users", "AWS Support"):
        assert step in msg
    assert "Mode: enforce" in msg


def test_old_instance_untouched(ks):
    iid, launched = launch()
    ks.clock.now = launched + timedelta(minutes=61)
    summary = handler.handler(health_event(ks.key), None)
    assert instance(iid)["State"]["Name"] == "running"
    assert summary["stopped"] == 0
    assert "stop_instances" not in ks.calls
    assert "Nothing to stop" not in ks.sent[0]  # the key line is there instead


def test_ignored_asg_and_spot_instances_are_not_stopped(ks):
    ignored, launched = launch({"tripwire": "ignore"})
    asg, _ = launch({ASG_TAG: "web"})
    spot, _ = launch(InstanceMarketOptions={"MarketType": "spot"})
    ks.clock.now = launched + timedelta(minutes=5)

    summary = handler.handler(health_event(ks.key), None)

    for iid in (ignored, asg, spot):
        assert instance(iid)["State"]["Name"] == "running"
    assert "stop_instances" not in ks.calls
    assert summary["not_stoppable"] == 2
    msg = ks.sent[0]
    assert ignored not in msg  # ignored: never acted on, never flagged
    assert f"Won't stop instance {asg}" in msg and "Auto Scaling group web" in msg
    assert f"Won't stop instance {spot}" in msg and "Spot" in msg


def test_observe_mode_makes_no_mutating_calls(ks, monkeypatch):
    monkeypatch.setenv("KILLSWITCH_MODE", "observe")
    iid, launched = launch()
    ks.clock.now = launched + timedelta(minutes=5)

    summary = handler.handler(health_event(ks.key), None)

    assert ks.calls == []
    assert key_status(ks.key) == "Active"
    assert instance(iid)["State"]["Name"] == "running"
    assert actions() == []
    assert summary["would_deactivate"] == 1 and summary["would_stop"] == 1
    msg = ks.sent[0]
    assert "Would deactivate key" in msg and f"Would stop instance {iid}" in msg
    assert "Mode: observe (nothing was changed" in msg


def test_iam_failure_still_stops_instances(ks, monkeypatch):
    monkeypatch.setattr(client("iam", REGION), "update_access_key", fail)
    iid, launched = launch()
    ks.clock.now = launched + timedelta(minutes=5)

    summary = handler.handler(health_event(ks.key), None)

    assert key_status(ks.key) == "Active"
    assert instance(iid)["State"]["Name"] in ("stopping", "stopped")
    assert summary["failed"] == 1 and summary["stopped"] == 1
    assert "Couldn't deactivate key" in ks.sent[0] and "(AccessDenied)" in ks.sent[0]


def test_unknown_key_user_still_stops_instances(ks):
    iid, launched = launch()
    ks.clock.now = launched + timedelta(minutes=5)
    summary = handler.handler(health_event("AKIAEXAMPLE0000TEST"), None)
    assert summary["failed"] == 1 and summary["stopped"] == 1
    assert "Couldn't find the IAM user for key ****TEST" in ks.sent[0]


def test_instance_stop_failure_still_alerts(ks, monkeypatch):
    monkeypatch.setattr(client("ec2", REGION), "stop_instances", fail)
    iid, launched = launch()
    ks.clock.now = launched + timedelta(minutes=5)

    summary = handler.handler(health_event(ks.key), None)

    assert key_status(ks.key) == "Inactive"
    assert summary["failed"] == 1 and summary["stopped"] == 0
    msg = ks.sent[0]
    assert f"Couldn't stop instance {iid} in {REGION} (AccessDenied)" in msg
    assert "Deactivated key" in msg


def test_region_failure_does_not_block_key_or_alert(ks, monkeypatch):
    monkeypatch.setattr(regions, "enabled_regions", fail)
    summary = handler.handler(health_event(ks.key), None)
    assert key_status(ks.key) == "Inactive"
    assert summary["failed"] == 1
    assert "Couldn't list regions" in ks.sent[0]


def test_tag_and_log_failures_do_not_hide_the_stop(ks, monkeypatch):
    monkeypatch.setattr(client("ec2", REGION), "create_tags", fail)
    monkeypatch.setattr(handler.state, "log_action", fail)
    iid, launched = launch()
    ks.clock.now = launched + timedelta(minutes=5)
    summary = handler.handler(health_event(ks.key), None)
    assert summary["stopped"] == 1
    msg = ks.sent[0]
    assert f"Stopped instance {iid}" in msg and "couldn't tag it" in msg
    assert "Couldn't write the action log" in msg


def test_exfiltration_role_key_is_not_deactivated_but_instances_are_stopped(ks):
    iid, launched = launch()
    ks.clock.now = launched + timedelta(minutes=5)
    summary = handler.handler(load("guardduty_exfiltration.json"), None)
    assert "update_access_key" not in ks.calls
    assert summary["not_deactivatable"] == 1 and summary["stopped"] == 1
    assert "Revoke active sessions" in ks.sent[0]
    assert "example-instance-role" in ks.sent[0]


def test_crypto_finding_without_key_stops_recent_instances(ks):
    iid, launched = launch()
    ks.clock.now = launched + timedelta(minutes=5)
    summary = handler.handler(load("guardduty_crypto.json"), None)
    assert "update_access_key" not in ks.calls
    assert summary["stopped"] == 1
    msg = ks.sent[0]
    assert msg.startswith("🚨 CloudTripwire kill switch: GuardDuty alert")
    assert "CryptoCurrency:EC2/BitcoinTool.B!DNS, instance i-0123456789abcdef0" in msg
    assert "Find who launched the instance" in msg


def sample_with_user_key(key_id: str) -> dict:
    """A sample finding that, if it weren't a sample, would deactivate a real key."""
    event = load("guardduty_sample.json")
    event["detail"]["type"] = "UnauthorizedAccess:IAMUser/InstanceCredentialExfiltration.OutsideAWS"
    event["detail"]["resource"] = {
        "resourceType": "AccessKey",
        "accessKeyDetails": {"accessKeyId": key_id, "userName": "bob", "userType": "IAMUser"},
    }
    return event


@pytest.mark.parametrize("mode", ["enforce", "observe"])
def test_sample_findings_alert_only_with_zero_mutating_calls(ks, monkeypatch, caplog, mode):
    monkeypatch.setenv("KILLSWITCH_MODE", mode)
    iid, launched = launch()
    ks.clock.now = launched + timedelta(minutes=5)

    with caplog.at_level(logging.INFO):
        crypto = handler.handler(load("guardduty_sample.json"), None)
        keyed = handler.handler(sample_with_user_key(ks.key), None)

    assert crypto["status"] == keyed["status"] == "sample"
    assert ks.calls == []  # no update_access_key, stop_instances or create_tags
    assert key_status(ks.key) == "Active"
    assert instance(iid)["State"]["Name"] == "running"
    assert actions() == []
    assert len(ks.sent) == 2
    for msg in ks.sent:
        assert "sample finding received, no action taken" in msg
    assert caplog.text.count('"event": "sample_finding"') == 2
    assert ks.key not in caplog.text + "\n".join(ks.sent)


def test_enforce_spy_sees_a_real_finding_after_a_sample(ks):
    """Proves the spy works in the sample test: the same setup does act on a real finding."""
    handler.handler(sample_with_user_key(ks.key), None)
    event = sample_with_user_key(ks.key)
    del event["detail"]["service"]["additionalInfo"]
    handler.handler(event, None)
    assert "update_access_key" in ks.calls
    assert key_status(ks.key) == "Inactive"


def test_unusable_event_is_skipped_without_alert(ks):
    assert handler.handler({"source": "aws.health", "detail": {}}, None) == {"status": "skipped"}
    assert ks.sent == [] and ks.calls == []


def test_no_full_key_or_secret_in_logs_or_messages(ks, monkeypatch, caplog):
    iid, launched = launch()
    ks.clock.now = launched + timedelta(minutes=5)
    with caplog.at_level(logging.INFO):
        handler.handler(health_event(ks.key), None)
        # Failure paths too: IAM error messages contain the key ID.
        monkeypatch.setattr(client("iam", REGION), "update_access_key", fail)
        handler.handler(health_event(ks.key), None)
        handler.handler(health_event("AKIAEXAMPLE0000TEST"), None)
    text = caplog.text + "\n".join(ks.sent)
    for secret in (ks.key, ks.secret, "AKIAEXAMPLE0000TEST"):
        assert secret not in text
    assert f"****{ks.key[-4:]}" in text
