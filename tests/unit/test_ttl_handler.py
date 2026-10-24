import json
import logging
from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ClientError

from tests.helpers import FAKE_AMI, client, create_state_table, dynamodb, launch_instance
from tripwire.common import notify, regions
from tripwire.common.config import Config
from tripwire.ttl import handler
from tripwire.ttl.handler import Resource, evaluate

TABLE = "cloudtripwire-state"
REGION = "us-east-1"
CREATED = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
WRITE_CALLS = ("stop_instances", "create_tags", "stop_db_instance", "add_tags_to_resource")


# --- pure rules -------------------------------------------------------------


def res(tags: dict[str, str] | None = None, **kw) -> Resource:
    return Resource("ec2", "i-1", REGION, CREATED, tags or {}, **kw)


@pytest.mark.parametrize(
    ("tags", "hours", "status"),
    [
        ({}, 100, "ok"),  # no ttl, lab mode off
        ({"ttl": "4h"}, 2, "ok"),
        ({"ttl": "4h"}, 3.5, "warning"),
        ({"TTL": "4h"}, 4, "expired"),
        ({"ttl": "4h", "tripwire": "ignore"}, 100, "ok"),
        ({"ttl": "tomorrow"}, 1, "unreadable"),
        ({"ttl": ""}, 1, "unreadable"),
    ],
)
def test_evaluate(tags, hours, status):
    assert evaluate(res(tags), Config(), CREATED + timedelta(hours=hours)).status == status


def test_evaluate_lab_mode():
    lab = Config(lab_mode=True, lab_grace_hours=24)
    assert evaluate(res(), lab, CREATED + timedelta(hours=23)).status == "warning"
    verdict = evaluate(res(), lab, CREATED + timedelta(hours=25))
    assert verdict.status == "expired"
    assert verdict.rule == "lab mode: no ttl tag, 24h grace"
    assert verdict.expiry == CREATED + timedelta(hours=24)
    # An explicit ttl wins over lab mode, and ignore still wins over both.
    assert evaluate(res({"ttl": "1w"}), lab, CREATED + timedelta(hours=25)).status == "ok"
    ignored = res({"tripwire": "ignore"})
    assert evaluate(ignored, lab, CREATED + timedelta(days=30)).status == "ok"


def test_humanize():
    assert handler.humanize(timedelta(minutes=20)) == "20 min"
    assert handler.humanize(timedelta(hours=5)) == "5 h"
    assert handler.humanize(timedelta(days=3, hours=2)) == "3 days"


@pytest.mark.parametrize(
    ("db", "expected"),
    [
        ({"Engine": "postgres"}, ""),
        ({"DBClusterIdentifier": "cl1"}, "Aurora cluster cl1"),
        ({"ReadReplicaSourceDBInstanceIdentifier": "db1"}, "is a read replica"),
        ({"ReadReplicaSourceDBClusterIdentifier": "cl1"}, "is a read replica"),
        ({"ReadReplicaDBInstanceIdentifiers": ["db1-r"]}, "has read replicas"),
        ({"Engine": "sqlserver-se", "MultiAZ": True}, "Multi-AZ SQL Server"),
        ({"Engine": "sqlserver-se", "MultiAZ": False}, ""),
    ],
)
def test_rds_blocked_reason(db, expected):
    reason = handler.rds_blocked_reason(db)
    assert expected in reason if expected else reason == ""


def test_ec2_blocked_reason():
    assert "Auto Scaling group web" in handler.ec2_blocked_reason({}, {handler.ASG_TAG: "web"})
    assert "Spot" in handler.ec2_blocked_reason({"InstanceLifecycle": "spot"}, {})
    assert handler.ec2_blocked_reason({}, {}) == ""


# --- handler with moto ------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def ttl(aws, monkeypatch):
    """Handler wired to moto; returns (sent messages, clock, write-call spy)."""
    monkeypatch.setenv("STATE_TABLE", TABLE)
    monkeypatch.setenv("REGIONS", REGION)
    monkeypatch.setattr(regions, "get_client", lambda s, r=None: client(s, r or REGION))
    monkeypatch.setattr(handler, "_dynamodb", dynamodb)
    clock = Clock()
    monkeypatch.setattr(handler, "_now", clock)
    create_state_table(TABLE)
    sent: list[str] = []
    monkeypatch.setattr(notify, "send", lambda msg, *a: sent.append(msg) or {"telegram": True})

    # Spy on every write call, then pass it through to moto.
    calls: list[str] = []
    for svc, names in (("ec2", WRITE_CALLS[:2]), ("rds", WRITE_CALLS[2:])):
        c = client(svc, REGION)
        for name in names:
            real = getattr(c, name)

            def spy(*a, _real=real, _name=name, **kw):
                calls.append(_name)
                return _real(*a, **kw)

            monkeypatch.setattr(c, name, spy)
    return sent, clock, calls


def ec2_with(tags: dict[str, str] | None = None, **kwargs) -> tuple[str, datetime]:
    ec2 = client("ec2", REGION)
    tag_list = [{"Key": k, "Value": v} for k, v in (tags or {}).items()]
    if tag_list:
        kwargs["TagSpecifications"] = [{"ResourceType": "instance", "Tags": tag_list}]
    iid = launch_instance(ec2, **kwargs)
    inst = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
    return iid, inst["LaunchTime"]


def ec2_state(iid: str) -> str:
    resp = client("ec2", REGION).describe_instances(InstanceIds=[iid])
    return resp["Reservations"][0]["Instances"][0]["State"]["Name"]


def ec2_tags(iid: str) -> dict[str, str]:
    resp = client("ec2", REGION).describe_instances(InstanceIds=[iid])
    return {t["Key"]: t["Value"] for t in resp["Reservations"][0]["Instances"][0].get("Tags", [])}


def rds_with(ident: str, tags: dict[str, str] | None = None, **kwargs) -> datetime:
    kwargs.setdefault("Engine", "postgres")
    db = client("rds", REGION).create_db_instance(
        DBInstanceIdentifier=ident,
        DBInstanceClass="db.t3.micro",
        MasterUsername="admin1",
        MasterUserPassword="not-a-real-password",
        AllocatedStorage=20,
        Tags=[{"Key": k, "Value": v} for k, v in (tags or {}).items()],
        **kwargs,
    )["DBInstance"]
    return db["InstanceCreateTime"]


def rds_db(ident: str) -> dict:
    return client("rds", REGION).describe_db_instances(DBInstanceIdentifier=ident)["DBInstances"][0]


def actions() -> list[dict]:
    items = dynamodb().Table(TABLE).scan()["Items"]
    return [i for i in items if i["pk"].startswith("ACTION#")]


def test_observe_expired_reports_once_and_never_stops(ttl, monkeypatch):
    sent, clock, calls = ttl
    iid, launched = ec2_with({"ttl": "4h"})
    created = rds_with("lab-db", {"ttl": "4h"})
    clock.now = max(launched, created) + timedelta(hours=4, minutes=20)

    summary = handler.handler({}, None)

    [msg] = sent
    assert f"• Would stop EC2 instance {iid} in us-east-1 (ttl 4h, expired 20 min ago)" in msg
    assert "• Would stop RDS DB instance lab-db in us-east-1" in msg
    assert "Mode: observe (nothing was changed)" in msg
    assert calls == []  # observe never calls a stop or tag API
    assert ec2_state(iid) == "running"
    assert rds_db("lab-db")["DBInstanceStatus"] == "available"
    assert actions() == []
    assert summary["would_stop"] == 2 and summary["stopped"] == 0

    clock.now += timedelta(hours=1)
    handler.handler({}, None)
    assert len(sent) == 1  # alerted once per resource


def test_enforce_stops_tags_logs_and_alerts_with_undo(ttl, monkeypatch):
    sent, clock, calls = ttl
    monkeypatch.setenv("MODE", "enforce")
    iid, launched = ec2_with({"ttl": "4h"})
    keep, _ = ec2_with({"ttl": "2d"})
    created = rds_with("lab-db", {"ttl": "4h"})
    clock.now = max(launched, created) + timedelta(hours=5)

    summary = handler.handler({}, None)

    assert sorted(calls) == sorted(WRITE_CALLS)  # the spy sees writes, so [] elsewhere is real
    assert ec2_state(iid) in ("stopping", "stopped")
    assert ec2_state(keep) == "running"
    assert ec2_tags(iid)[handler.STOPPED_AT_TAG] == clock.now.isoformat(timespec="seconds")
    db = rds_db("lab-db")
    assert db["DBInstanceStatus"] == "stopped"
    assert {"Key": handler.STOPPED_AT_TAG, "Value": clock.now.isoformat(timespec="seconds")} in (
        db["TagList"]
    )

    [msg] = sent
    assert f"aws ec2 start-instances --instance-ids {iid} --region us-east-1" in msg
    assert "aws rds start-db-instance --db-instance-identifier lab-db --region us-east-1" in msg
    assert "Change or remove the ttl tag first" in msg
    assert "Mode: enforce" in msg

    logged = {a["resource_id"]: a for a in actions()}
    assert logged[iid]["action"] == "stop_instance"
    assert logged["lab-db"]["action"] == "stop_db"
    assert logged[iid]["performed"] is True
    assert logged[iid]["undo_command"].startswith("aws ec2 start-instances")
    assert summary["stopped"] == 2
    assert summary["resources_checked"] == 3


def test_warning_sent_once_an_hour_before_expiry(ttl, monkeypatch):
    sent, clock, calls = ttl
    monkeypatch.setenv("MODE", "enforce")
    iid, launched = ec2_with({"ttl": "4h"})
    clock.now = launched + timedelta(hours=3, minutes=25)

    handler.handler({}, None)
    clock.now += timedelta(minutes=10)
    handler.handler({}, None)

    [msg] = sent
    assert f"• Expires in 35 min: EC2 instance {iid} in us-east-1 (ttl 4h)" in msg
    assert calls == []
    assert ec2_state(iid) == "running"


def test_unreadable_ttl_alerts_once_and_does_nothing(ttl, monkeypatch):
    sent, clock, calls = ttl
    monkeypatch.setenv("MODE", "enforce")
    iid, launched = ec2_with({"ttl": "tomorrow"})
    clock.now = launched + timedelta(days=30)

    summary = handler.handler({}, None)
    handler.handler({}, None)

    [msg] = sent
    assert f"Unreadable ttl tag 'tomorrow' on EC2 instance {iid} in us-east-1" in msg
    assert calls == []
    assert summary["unreadable"] == 1


def test_ignored_resources_are_never_touched_or_reported(ttl, monkeypatch):
    sent, clock, calls = ttl
    monkeypatch.setenv("MODE", "enforce")
    monkeypatch.setenv("LAB_MODE", "true")
    _, launched = ec2_with({"ttl": "30m", "tripwire": "ignore"})
    rds_with("keep-db", {"tripwire": "IGNORE"})
    clock.now = launched + timedelta(days=30)

    handler.handler({}, None)

    assert sent == [] and calls == []


def test_lab_mode_stops_untagged_resources_after_grace(ttl, monkeypatch):
    sent, clock, calls = ttl
    monkeypatch.setenv("MODE", "enforce")
    iid, launched = ec2_with()
    clock.now = launched + timedelta(hours=25)

    handler.handler({}, None)  # lab mode off: untagged is fine
    assert sent == [] and ec2_state(iid) == "running"

    monkeypatch.setenv("LAB_MODE", "true")
    handler.handler({}, None)

    [msg] = sent
    assert f"Stopped EC2 instance {iid}" in msg
    assert "lab mode: no ttl tag, 24h grace, expired 60 min ago" in msg
    assert ec2_state(iid) in ("stopping", "stopped")


def test_unstoppable_resources_alert_once_with_reason(ttl, monkeypatch):
    sent, clock, calls = ttl
    monkeypatch.setenv("MODE", "enforce")
    asg, launched = ec2_with({"ttl": "1h", handler.ASG_TAG: "web-asg"})
    spot, _ = ec2_with({"ttl": "1h"}, InstanceMarketOptions={"MarketType": "spot"})
    rds = client("rds", REGION)
    rds_with("source-db", {"ttl": "1h"})
    rds.create_db_instance_read_replica(
        DBInstanceIdentifier="replica-db",
        SourceDBInstanceIdentifier="source-db",
        Tags=[{"Key": "ttl", "Value": "1h"}],
    )
    rds.create_db_cluster(
        DBClusterIdentifier="aurora-cl",
        Engine="aurora-postgresql",
        MasterUsername="admin1",
        MasterUserPassword="not-a-real-password",
    )
    rds_with("aurora-a", {"ttl": "1h"}, Engine="aurora-postgresql", DBClusterIdentifier="aurora-cl")
    clock.now = launched + timedelta(hours=3)

    summary = handler.handler({}, None)
    handler.handler({}, None)

    [msg] = sent
    assert f"Won't stop EC2 instance {asg}" in msg and "Auto Scaling group web-asg" in msg
    assert f"Won't stop EC2 instance {spot}" in msg and "Spot Instance" in msg
    assert "Won't stop RDS DB instance replica-db" in msg and "is a read replica" in msg
    assert "Won't stop RDS DB instance source-db" in msg and "has read replicas" in msg
    assert "Won't stop RDS DB instance aurora-a" in msg and "Aurora cluster aurora-cl" in msg
    assert calls == []
    assert summary["not_stoppable"] == 5
    assert ec2_state(asg) == ec2_state(spot) == "running"


def test_failing_resource_does_not_stop_the_run(ttl, monkeypatch):
    sent, clock, calls = ttl
    monkeypatch.setenv("MODE", "enforce")
    bad, launched = ec2_with({"ttl": "1h"})
    good, _ = ec2_with({"ttl": "1h"})
    rds_with("lab-db", {"ttl": "1h"})
    clock.now = launched + timedelta(hours=2)
    ec2 = client("ec2", REGION)
    real_stop = ec2.stop_instances

    def flaky_stop(InstanceIds):
        if InstanceIds == [bad]:
            raise RuntimeError("simulated UnauthorizedOperation")
        return real_stop(InstanceIds=InstanceIds)

    monkeypatch.setattr(ec2, "stop_instances", flaky_stop)

    summary = handler.handler({}, None)

    assert ec2_state(bad) == "running"
    assert ec2_state(good) in ("stopping", "stopped")
    assert rds_db("lab-db")["DBInstanceStatus"] == "stopped"
    assert summary["checks_failed"] == 1 and summary["stopped"] == 2
    [msg] = sent
    assert "Warning: 1 check failed, see logs" in msg


def test_failing_listing_still_alerts_with_warning(ttl, monkeypatch):
    sent, clock, calls = ttl
    rds = client("rds", REGION)

    def broken(*a, **kw):
        raise RuntimeError("simulated AccessDenied")

    monkeypatch.setattr(rds, "get_paginator", broken)

    summary = handler.handler({}, None)  # nothing tagged; only the failure to report

    [msg] = sent
    assert msg.startswith("⚠️ CloudTripwire TTL: nothing to report in the checks that ran")
    assert "Warning: 1 check failed, see logs" in msg
    assert summary["checks_failed"] == 1


def test_failure_warning_sent_once_per_day_per_region_and_error_code(ttl, monkeypatch):
    sent, clock, calls = ttl
    rds = client("rds", REGION)
    error = {"code": "AccessDenied"}

    def broken(*a, **kw):
        raise ClientError({"Error": {"Code": error["code"], "Message": "x"}}, "DescribeDBInstances")

    monkeypatch.setattr(rds, "get_paginator", broken)
    clock.now = datetime(2026, 10, 24, 1, 0, tzinfo=UTC)

    handler.handler({}, None)
    clock.now += timedelta(hours=5)
    summary = handler.handler({}, None)  # same day, same error: silent

    assert len(sent) == 1
    assert summary["checks_failed"] == 1 and summary["failures_reported"] == 0

    error["code"] = "Throttling"  # a different error the same day is new
    handler.handler({}, None)
    assert len(sent) == 2

    clock.now = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # next UTC day
    error["code"] = "AccessDenied"
    handler.handler({}, None)
    assert len(sent) == 3
    assert "Warning: 1 check failed, see logs" in sent[-1]


def test_repeated_failure_does_not_hide_new_alerts(ttl, monkeypatch):
    sent, clock, calls = ttl
    rds = client("rds", REGION)

    def broken(*a, **kw):
        raise RuntimeError("simulated AccessDenied")

    monkeypatch.setattr(rds, "get_paginator", broken)
    handler.handler({}, None)  # failure reported
    iid, launched = ec2_with({"ttl": "tomorrow"})
    clock.now = launched + timedelta(hours=1)

    handler.handler({}, None)

    msg = sent[-1]
    assert len(sent) == 2
    assert f"Unreadable ttl tag 'tomorrow' on EC2 instance {iid}" in msg
    assert "Warning" not in msg  # already reported today


def test_tag_failure_after_stop_is_still_alerted(ttl, monkeypatch, caplog):
    sent, clock, calls = ttl
    monkeypatch.setenv("MODE", "enforce")
    iid, launched = ec2_with({"ttl": "1h"})
    clock.now = launched + timedelta(hours=2)

    def broken_tags(**kw):
        raise RuntimeError("simulated tag failure")

    monkeypatch.setattr(client("ec2", REGION), "create_tags", broken_tags)
    caplog.set_level(logging.INFO)

    summary = handler.handler({}, None)

    assert ec2_state(iid) in ("stopping", "stopped")
    assert summary["stopped"] == 1
    assert f"Stopped EC2 instance {iid}" in sent[0]
    assert len(actions()) == 1
    stopped = [r for r in caplog.records if '"event": "stopped"' in r.getMessage()]
    assert stopped[0].levelno == logging.ERROR
    assert json.loads(stopped[0].getMessage())["problems"] == ["tag_failed: RuntimeError"]


def test_nothing_to_report_sends_nothing_and_logs_summary(ttl, caplog):
    sent, clock, calls = ttl
    ec2_with({"ttl": "2d"})
    client("ec2", REGION).run_instances(ImageId=FAKE_AMI, MinCount=1, MaxCount=1)
    caplog.set_level(logging.INFO)

    summary = handler.handler({}, None)

    assert sent == []
    assert summary["resources_checked"] == 2
    events = [json.loads(r.getMessage()) for r in caplog.records if r.getMessage().startswith("{")]
    [logged] = [e for e in events if e["event"] == "ttl_summary"]
    assert logged == {"event": "ttl_summary"} | summary
