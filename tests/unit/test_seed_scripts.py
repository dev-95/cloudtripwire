import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import cleanup_seed  # noqa: E402
import seed_zombies  # noqa: E402

REGION = seed_zombies.REGION


@pytest.fixture
def test_profile(aws, monkeypatch, tmp_path):
    """A fake tripwire-test profile; moto answers as the given account."""
    config = tmp_path / "config"
    config.write_text(
        "[profile tripwire-test]\n"
        "region = ca-central-1\n"
        "aws_access_key_id = testing\n"
        "aws_secret_access_key = testing\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))

    def as_account(account_id: str):
        monkeypatch.setenv("MOTO_ACCOUNT_ID", account_id)
        return seed_zombies.get_test_session

    return as_account


# --- guard ---


def test_guard_accepts_test_account(test_profile):
    session = test_profile(seed_zombies.TEST_ACCOUNT_ID)()
    assert session.region_name == REGION


def test_guard_refuses_other_account(test_profile):
    get_session = test_profile("123456789012")
    with pytest.raises(seed_zombies.WrongAccountError, match="not the test account"):
        get_session()


def test_guard_refuses_other_profile(test_profile):
    get_session = test_profile(seed_zombies.TEST_ACCOUNT_ID)
    with pytest.raises(seed_zombies.WrongAccountError, match="only 'tripwire-test'"):
        get_session("default")


@pytest.mark.parametrize("script", [seed_zombies, cleanup_seed])
def test_main_refuses_wrong_account_without_touching_ec2(test_profile, monkeypatch, script):
    test_profile("123456789012")
    monkeypatch.setattr(seed_zombies, "seed", lambda s: pytest.fail("seed must not run"))
    monkeypatch.setattr(cleanup_seed, "cleanup", lambda s: pytest.fail("cleanup must not run"))

    assert script.main() == 2


# --- seed / cleanup ---


def test_seed_creates_tagged_volume_and_eip_once(test_profile):
    session = test_profile(seed_zombies.TEST_ACCOUNT_ID)()
    ec2 = session.client("ec2", region_name=REGION)

    created = seed_zombies.seed(session)
    again = seed_zombies.seed(session)

    [vol] = ec2.describe_volumes()["Volumes"]
    assert vol["Size"] == 1 and vol["VolumeType"] == "gp3" and vol["State"] == "available"
    assert {"Key": "tripwire-seed", "Value": "true"} in vol["Tags"]
    [addr] = ec2.describe_addresses()["Addresses"]
    assert "AssociationId" not in addr
    assert {"Key": "tripwire-seed", "Value": "true"} in addr["Tags"]
    assert created == {"volumes": [vol["VolumeId"]], "addresses": [addr["AllocationId"]]}
    assert again == {"volumes": [], "addresses": []}  # idempotent


def test_cleanup_removes_only_seed_resources(test_profile):
    session = test_profile(seed_zombies.TEST_ACCOUNT_ID)()
    ec2 = session.client("ec2", region_name=REGION)
    seed_zombies.seed(session)
    keep_vol = ec2.create_volume(AvailabilityZone=f"{REGION}a", Size=1)["VolumeId"]
    keep_eip = ec2.allocate_address(Domain="vpc")["AllocationId"]
    other_tag = [{"ResourceType": "volume", "Tags": [{"Key": "tripwire-seed", "Value": "no"}]}]
    keep_tagged = ec2.create_volume(
        AvailabilityZone=f"{REGION}a", Size=1, TagSpecifications=other_tag
    )["VolumeId"]

    removed = cleanup_seed.cleanup(session)

    assert len(removed["volumes"]) == 1 and len(removed["addresses"]) == 1
    left_vols = {v["VolumeId"] for v in ec2.describe_volumes()["Volumes"]}
    assert left_vols == {keep_vol, keep_tagged}
    assert [a["AllocationId"] for a in ec2.describe_addresses()["Addresses"]] == [keep_eip]


def test_cleanup_with_nothing_seeded(test_profile):
    session = test_profile(seed_zombies.TEST_ACCOUNT_ID)()

    assert cleanup_seed.cleanup(session) == {"volumes": [], "addresses": []}
