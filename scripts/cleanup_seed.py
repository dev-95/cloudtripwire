"""Remove what seed_zombies created.

Usage: python scripts/cleanup_seed.py

Deletes only resources tagged tripwire-seed=true in ca-central-1, and only in the
test account (same guard as seed_zombies.py). Everything else is left alone.
"""

import sys

import boto3
from seed_zombies import REGION, SEED_FILTER, WrongAccountError, get_test_session


def _is_seed(tags: list[dict] | None) -> bool:
    # Re-checked here even though the API filter already matched: never trust one layer.
    return any(t["Key"] == "tripwire-seed" and t.get("Value") == "true" for t in tags or [])


def cleanup(session: boto3.Session) -> dict[str, list[str]]:
    """Delete seed volumes and release seed Elastic IPs. Returns what was removed."""
    ec2 = session.client("ec2", region_name=REGION)
    removed: dict[str, list[str]] = {"volumes": [], "addresses": []}

    for vol in ec2.describe_volumes(Filters=SEED_FILTER)["Volumes"]:
        if not _is_seed(vol.get("Tags")):
            continue
        if vol["State"] != "available":
            print(f"skipping {vol['VolumeId']}: state {vol['State']} (attached?)")
            continue
        ec2.delete_volume(VolumeId=vol["VolumeId"])
        removed["volumes"].append(vol["VolumeId"])
        print(f"deleted volume {vol['VolumeId']}")

    for addr in ec2.describe_addresses(Filters=SEED_FILTER)["Addresses"]:
        if not _is_seed(addr.get("Tags")):
            continue
        if "AssociationId" in addr:
            print(f"skipping {addr['AllocationId']}: associated with something")
            continue
        ec2.release_address(AllocationId=addr["AllocationId"])
        removed["addresses"].append(addr["AllocationId"])
        print(f"released Elastic IP {addr['PublicIp']} ({addr['AllocationId']})")

    return removed


def main() -> int:
    try:
        session = get_test_session()
    except WrongAccountError as e:
        print(f"REFUSED: {e}")
        return 2
    removed = cleanup(session)
    if not any(removed.values()):
        print("nothing to clean up")
    return 0


if __name__ == "__main__":
    sys.exit(main())
