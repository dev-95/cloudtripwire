from datetime import UTC, datetime, timedelta

import pytest

from tests.helpers import clients, launch_instance, tag
from tripwire.common.config import Config
from tripwire.sweep.detectors import DETECTORS, ebs_unattached, eip_unassociated

REGION = "us-east-1"
CFG = Config()


def now() -> datetime:
    return datetime.now(UTC)


def test_registry_order():
    assert [name for name, _ in DETECTORS] == [
        "ebs_unattached",
        "eip_unassociated",
        "snapshots_old",
        "ec2_stopped_long",
    ]


# --- ebs_unattached ---


def test_ebs_finds_unattached_volume(aws):
    c = clients()
    vid = c.ec2.create_volume(AvailabilityZone="us-east-1a", Size=20, VolumeType="gp3")["VolumeId"]

    [f] = ebs_unattached.detect(c, REGION, CFG, now() + timedelta(days=12))

    assert f.resource_id == vid
    assert f.detector == "ebs_unattached"
    assert f.monthly_cost_usd == 1.60
    assert f.age_days == 12
    assert f.reason == "20 GB gp3 volume, not attached to any instance"
    assert f.fix_command == (
        f'aws ec2 create-snapshot --volume-id {vid} --description "Backup of {vid}"'
        f" --region {REGION} && aws ec2 delete-volume --volume-id {vid} --region {REGION}"
    )


def test_ebs_ignores_attached_volume(aws):
    c = clients()
    iid = launch_instance(c.ec2)  # root volume is attached
    vid = c.ec2.create_volume(AvailabilityZone="us-east-1a", Size=5)["VolumeId"]
    c.ec2.attach_volume(VolumeId=vid, InstanceId=iid, Device="/dev/sdf")

    assert ebs_unattached.detect(c, REGION, CFG, now()) == []


@pytest.mark.parametrize("value", ["ignore", "IGNORE", " Ignore "])
def test_ebs_respects_ignore_tag(aws, value):
    c = clients()
    c.ec2.create_volume(
        AvailabilityZone="us-east-1a",
        Size=5,
        TagSpecifications=tag("TripWire", value, "volume"),
    )

    assert ebs_unattached.detect(c, REGION, CFG, now()) == []


def test_ebs_empty_region(aws):
    assert ebs_unattached.detect(clients("eu-west-3"), "eu-west-3", CFG, now()) == []


# --- eip_unassociated ---


def test_eip_finds_unassociated_address(aws):
    c = clients()
    addr = c.ec2.allocate_address(Domain="vpc")

    [f] = eip_unassociated.detect(c, REGION, CFG, now())

    assert f.resource_id == addr["AllocationId"]
    assert f.detector == "eip_unassociated"
    assert f.monthly_cost_usd == 3.65
    assert f.age_days is None
    assert f.reason == f"Elastic IP {addr['PublicIp']} is not attached to anything"
    assert f.fix_command == (
        f"aws ec2 release-address --allocation-id {addr['AllocationId']} --region {REGION}"
    )


def test_eip_ignores_associated_address(aws):
    c = clients()
    iid = launch_instance(c.ec2)
    alloc = c.ec2.allocate_address(Domain="vpc")["AllocationId"]
    c.ec2.associate_address(AllocationId=alloc, InstanceId=iid)

    assert eip_unassociated.detect(c, REGION, CFG, now()) == []


@pytest.mark.parametrize("value", ["ignore", "IGNORE", " Ignore "])
def test_eip_respects_ignore_tag(aws, value):
    c = clients()
    c.ec2.allocate_address(Domain="vpc", TagSpecifications=tag("tripwire", value, "elastic-ip"))

    assert eip_unassociated.detect(c, REGION, CFG, now()) == []


def test_eip_empty_region(aws):
    assert eip_unassociated.detect(clients("eu-west-3"), "eu-west-3", CFG, now()) == []
