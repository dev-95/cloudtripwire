from datetime import UTC, datetime, timedelta

import boto3
import pytest
from botocore.stub import Stubber

from tripwire.common.clients import RegionClients
from tripwire.common.config import Config
from tripwire.sweep.detectors import ec2_stopped_long, snapshots_old

REGION = "us-east-1"
CFG = Config()  # snapshot_max_age_days=90, stopped_instance_days=7


def clients(region: str = REGION) -> RegionClients:
    return RegionClients(
        ec2=boto3.client("ec2", region_name=region),
        elbv2=boto3.client("elbv2", region_name=region),
        cloudwatch=boto3.client("cloudwatch", region_name=region),
    )


def tag(key: str, value: str, resource: str) -> list[dict]:
    return [{"ResourceType": resource, "Tags": [{"Key": key, "Value": value}]}]


def launch_instance(ec2, **kwargs) -> str:
    ami = "ami-0123456789abcdef0"  # moto doesn't validate it when default AMIs aren't loaded
    resp = ec2.run_instances(ImageId=ami, MinCount=1, MaxCount=1, **kwargs)
    return resp["Instances"][0]["InstanceId"]


# --- snapshots_old ---


def make_snapshot(ec2, size: int = 10, tags: list[dict] | None = None) -> tuple[str, datetime]:
    vid = ec2.create_volume(AvailabilityZone="us-east-1a", Size=size)["VolumeId"]
    snap = ec2.create_snapshot(VolumeId=vid, TagSpecifications=tags or [])
    return snap["SnapshotId"], snap["StartTime"]


def test_snapshot_finds_old_snapshot(aws):
    c = clients()
    sid, started = make_snapshot(c.ec2, size=10)

    [f] = snapshots_old.detect(c, REGION, CFG, started + timedelta(days=120))

    assert f.resource_id == sid
    assert f.detector == "snapshots_old"
    assert f.monthly_cost_usd == 0.50
    assert f.age_days == 120
    assert "estimate, billed size is often smaller" in f.reason
    assert f.fix_command == f"aws ec2 delete-snapshot --snapshot-id {sid} --region {REGION}"


def test_snapshot_ignores_recent_snapshot(aws):
    c = clients()
    _, started = make_snapshot(c.ec2)

    assert snapshots_old.detect(c, REGION, CFG, started + timedelta(days=30)) == []


def test_snapshot_boundary_at_threshold(aws):
    c = clients()
    _, started = make_snapshot(c.ec2)
    at_threshold = started + timedelta(days=CFG.snapshot_max_age_days)

    assert snapshots_old.detect(c, REGION, CFG, at_threshold) == []
    assert len(snapshots_old.detect(c, REGION, CFG, at_threshold + timedelta(seconds=1))) == 1


@pytest.mark.parametrize("value", ["ignore", "IGNORE", " Ignore "])
def test_snapshot_respects_ignore_tag(aws, value):
    c = clients()
    _, started = make_snapshot(c.ec2, tags=tag("tripwire", value, "snapshot"))

    assert snapshots_old.detect(c, REGION, CFG, started + timedelta(days=120)) == []


def test_snapshot_skips_snapshot_used_by_ami(aws):
    c = clients()
    iid = launch_instance(c.ec2)
    c.ec2.create_image(InstanceId=iid, Name="my-ami")  # moto snapshots the root volume
    [snap] = c.ec2.describe_snapshots(OwnerIds=["self"])["Snapshots"]

    assert snapshots_old.detect(c, REGION, CFG, snap["StartTime"] + timedelta(days=120)) == []


def test_snapshot_skips_aws_backup_snapshot(aws):
    c = clients()
    _, started = make_snapshot(
        c.ec2, tags=tag("aws:backup:source-resource", "vol-0123456789abcdef0", "snapshot")
    )

    assert snapshots_old.detect(c, REGION, CFG, started + timedelta(days=120)) == []


def test_snapshot_empty_region(aws):
    now = datetime.now(UTC)
    assert snapshots_old.detect(clients("eu-west-3"), "eu-west-3", CFG, now) == []


# --- ec2_stopped_long ---


def stopped_instance(ec2, **kwargs) -> tuple[str, datetime]:
    iid = launch_instance(ec2, **kwargs)
    ec2.stop_instances(InstanceIds=[iid])
    inst = ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
    return iid, ec2_stopped_long.parse_stop_time(inst["StateTransitionReason"])


@pytest.mark.parametrize(
    "reason",
    ["User initiated (2026-10-01 12:00:00 GMT)", "User initiated (2026-10-01 12:00:00 UTC)"],
)
def test_parse_stop_time(reason):
    assert ec2_stopped_long.parse_stop_time(reason) == datetime(2026, 10, 1, 12, tzinfo=UTC)


@pytest.mark.parametrize("reason", [None, "", "Server.InternalError", "User initiated"])
def test_parse_stop_time_unreadable(reason):
    assert ec2_stopped_long.parse_stop_time(reason) is None


def test_ec2_finds_long_stopped_instance(aws):
    c = clients()
    iid, stopped_at = stopped_instance(c.ec2)
    vid = c.ec2.create_volume(AvailabilityZone="us-east-1a", Size=20, VolumeType="gp3")["VolumeId"]
    c.ec2.attach_volume(VolumeId=vid, InstanceId=iid, Device="/dev/sdf")
    root = c.ec2.describe_volumes(Filters=[{"Name": "attachment.instance-id", "Values": [iid]}])
    root_cost = sum(v["Size"] * 0.10 for v in root["Volumes"] if v["VolumeId"] != vid)  # gp2

    [f] = ec2_stopped_long.detect(c, REGION, CFG, stopped_at + timedelta(days=10))

    assert f.resource_id == iid
    assert f.detector == "ec2_stopped_long"
    assert f.monthly_cost_usd == pytest.approx(1.60 + root_cost)
    assert f.age_days == 10
    assert f.reason.startswith("stopped for 10 days, still paying for 2 EBS volume(s)")
    assert f.fix_command == (
        f'aws ec2 create-image --instance-id {iid} --name "backup-{iid}" --region {REGION}'
        f" && aws ec2 wait image-available --owners self"
        f" --filters Name=name,Values=backup-{iid} --region {REGION}"
        f" && aws ec2 terminate-instances --instance-ids {iid} --region {REGION}"
    )


def test_ec2_ignores_running_and_recently_stopped(aws):
    c = clients()
    launch_instance(c.ec2)
    _, stopped_at = stopped_instance(c.ec2)

    assert ec2_stopped_long.detect(c, REGION, CFG, stopped_at + timedelta(days=2)) == []


def test_ec2_boundary_at_threshold(aws):
    c = clients()
    _, stopped_at = stopped_instance(c.ec2)
    at_threshold = stopped_at + timedelta(days=CFG.stopped_instance_days)

    assert ec2_stopped_long.detect(c, REGION, CFG, at_threshold) == []
    assert len(ec2_stopped_long.detect(c, REGION, CFG, at_threshold + timedelta(seconds=1))) == 1


@pytest.mark.parametrize("value", ["ignore", "IGNORE", " Ignore "])
def test_ec2_respects_ignore_tag(aws, value):
    c = clients()
    _, stopped_at = stopped_instance(c.ec2, TagSpecifications=tag("tripwire", value, "instance"))

    assert ec2_stopped_long.detect(c, REGION, CFG, stopped_at + timedelta(days=30)) == []


def test_ec2_skips_and_logs_unparseable_reason(aws, caplog):
    c = clients()
    stub = Stubber(c.ec2)
    stub.add_response(
        "describe_instances",
        {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-0123456789abcdef0",
                            "State": {"Name": "stopped", "Code": 80},
                            "StateTransitionReason": "Server.InternalError",
                        }
                    ]
                }
            ]
        },
    )
    with stub:  # no describe_volumes response queued: the detector must not call it
        assert ec2_stopped_long.detect(c, REGION, CFG, datetime.now(UTC)) == []
    stub.assert_no_pending_responses()
    assert "unparseable_stop_reason" in caplog.text
    assert "i-0123456789abcdef0" in caplog.text


def test_ec2_empty_region(aws):
    now = datetime.now(UTC)
    assert ec2_stopped_long.detect(clients("eu-west-3"), "eu-west-3", CFG, now) == []
