"""Create cheap test zombies in the TEST account only.

Usage: python scripts/seed_zombies.py

Creates, in ca-central-1, each tagged tripwire-seed=true:
  - a 1 GB unattached gp3 volume   (~$0.09/month)
  - one unassociated Elastic IP     (~$0.005/hour, ~$3.65/month)
Remove them with: python scripts/cleanup_seed.py

Refuses to run unless profile tripwire-test resolves to the test account.
Skips anything that already exists, so running it twice creates nothing new.
"""

import sys

import boto3

PROFILE = "tripwire-test"
TEST_ACCOUNT_ID = "****4522****"
REGION = "ca-central-1"
SEED_TAG = {"Key": "tripwire-seed", "Value": "true"}
SEED_FILTER = [{"Name": "tag:tripwire-seed", "Values": ["true"]}]


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


def main() -> int:
    try:
        session = get_test_session()
    except WrongAccountError as e:
        print(f"REFUSED: {e}")
        return 2
    seed(session)
    print(f"Done. Remove with: python scripts/cleanup_seed.py   (region {REGION})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
