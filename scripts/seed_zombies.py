"""Create cheap test zombies in the TEST account only.

Usage:
  python scripts/seed_zombies.py                  sweep zombies (volume + Elastic IP)
  python scripts/seed_zombies.py --ttl-instance   TTL test instance only

Default: creates, in ca-central-1, each tagged tripwire-seed=true:
  - a 1 GB unattached gp3 volume   (~$0.09/month)
  - one unassociated Elastic IP     (~$0.005/hour, ~$3.65/month)
--ttl-instance: creates one t3.micro (latest Amazon Linux 2023, default VPC, IMDSv2)
  tagged ttl=30m and tripwire-seed=true (~$0.012/hour plus an 8 GB root volume while
  it exists). The TTL Lambda should flag it 30 minutes after launch.
Remove everything with: python scripts/cleanup_seed.py

Refuses to run unless profile tripwire-test resolves to the test account.
Skips anything that already exists, so running it twice creates nothing new.
"""

import argparse
import sys

import boto3

PROFILE = "tripwire-test"
TEST_ACCOUNT_ID = "****4522****"
REGION = "ca-central-1"
SEED_TAG = {"Key": "tripwire-seed", "Value": "true"}
SEED_FILTER = [{"Name": "tag:tripwire-seed", "Values": ["true"]}]
# Public parameter AWS keeps pointing at the latest AL2023 AMI in each region.
AMI_PARAMETER = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
TTL_INSTANCE_TYPE = "t3.micro"
TTL_VALUE = "30m"
LIVE_STATES = ["pending", "running", "stopping", "stopped"]


class WrongAccountError(RuntimeError):
    pass


def get_test_session(profile: str = PROFILE) -> boto3.Session:
    """Session for the test account, or raise. The only way these scripts get AWS access."""
    if profile != PROFILE:
        raise WrongAccountError(f"refusing profile {profile!r}; only {PROFILE!r} is allowed")
    session = boto3.Session(profile_name=profile, region_name=REGION)
    account = session.client("sts").get_caller_identity()["Account"]
    if account != TEST_ACCOUNT_ID:
        raise WrongAccountError(
            f"profile {profile!r} is account {account}, not the test account {TEST_ACCOUNT_ID}"
        )
    return session


def _tag_spec(resource_type: str) -> list[dict]:
    return [{"ResourceType": resource_type, "Tags": [SEED_TAG]}]


def seed(session: boto3.Session) -> dict[str, list[str]]:
    """Create the seed resources that don't exist yet. Returns what was created."""
    ec2 = session.client("ec2", region_name=REGION)
    created: dict[str, list[str]] = {"volumes": [], "addresses": []}

    existing = ec2.describe_volumes(Filters=SEED_FILTER)["Volumes"]
    if existing:
        print(f"volume already seeded: {existing[0]['VolumeId']}")
    else:
        vol = ec2.create_volume(
            AvailabilityZone=f"{REGION}a",
            Size=1,
            VolumeType="gp3",
            TagSpecifications=_tag_spec("volume"),
        )
        created["volumes"].append(vol["VolumeId"])
        print(f"created 1 GB gp3 volume {vol['VolumeId']}")

    existing = ec2.describe_addresses(Filters=SEED_FILTER)["Addresses"]
    if existing:
        print(f"Elastic IP already seeded: {existing[0]['AllocationId']}")
    else:
        addr = ec2.allocate_address(Domain="vpc", TagSpecifications=_tag_spec("elastic-ip"))
        created["addresses"].append(addr["AllocationId"])
        print(f"allocated Elastic IP {addr['PublicIp']} ({addr['AllocationId']})")

    return created


def seed_ttl_instance(session: boto3.Session) -> str | None:
    """Launch the ttl=30m test instance unless one is already alive. Returns its id if new."""
    ec2 = session.client("ec2", region_name=REGION)
    live = SEED_FILTER + [{"Name": "instance-state-name", "Values": LIVE_STATES}]
    existing = [
        i["InstanceId"]
        for page in ec2.get_paginator("describe_instances").paginate(Filters=live)
        for r in page["Reservations"]
        for i in r["Instances"]
    ]
    if existing:
        print(f"TTL instance already seeded: {existing[0]}")
        return None

    ami = session.client("ssm", region_name=REGION).get_parameter(Name=AMI_PARAMETER)
    tags = [
        SEED_TAG,
        {"Key": "ttl", "Value": TTL_VALUE},
        {"Key": "Name", "Value": "tripwire-seed-ttl"},
    ]
    inst = ec2.run_instances(
        ImageId=ami["Parameter"]["Value"],
        InstanceType=TTL_INSTANCE_TYPE,
        MinCount=1,
        MaxCount=1,
        MetadataOptions={"HttpTokens": "required"},  # IMDSv2 only
        TagSpecifications=[{"ResourceType": "instance", "Tags": tags}],
    )["Instances"][0]
    print(f"launched {TTL_INSTANCE_TYPE} {inst['InstanceId']} with ttl={TTL_VALUE}")
    return inst["InstanceId"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed test resources in the test account.")
    parser.add_argument(
        "--ttl-instance",
        action="store_true",
        help=f"launch only a {TTL_INSTANCE_TYPE} tagged ttl={TTL_VALUE} (TTL Lambda test)",
    )
    args = parser.parse_args(argv)
    try:
        session = get_test_session()
    except WrongAccountError as e:
        print(f"REFUSED: {e}")
        return 2
    if args.ttl_instance:
        seed_ttl_instance(session)
    else:
        seed(session)
    print(f"Done. Remove with: python scripts/cleanup_seed.py   (region {REGION})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
