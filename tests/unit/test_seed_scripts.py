import sys
from pathlib import Path

import pytest

from tests.helpers import launch_instance, tag

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import cleanup_seed  # noqa: E402
import seed_zombies  # noqa: E402

REGION = seed_zombies.REGION
TEST_ACCOUNT = "111122223333"  # fake; the real ID only lives in the maintainer's env


@pytest.fixture
def test_profile(aws, monkeypatch, tmp_path):
    """A fake tripwire-test profile; moto answers as the given account.

    TRIPWIRE_TEST_ACCOUNT_ID is set to TEST_ACCOUNT, so as_account(TEST_ACCOUNT) passes.
    """
    monkeypatch.setenv(seed_zombies.ACCOUNT_ENV, TEST_ACCOUNT)
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
    session = test_profile(TEST_ACCOUNT)()
    assert session.region_name == REGION


def test_guard_refuses_other_account(test_profile):
    get_session = test_profile("123456789012")
    with pytest.raises(seed_zombies.WrongAccountError, match="not the test account"):
        get_session()


@pytest.mark.parametrize(
    ("value", "why"), [(None, "not set"), ("", "not set"), ("12345", "not 12 digits")]
)
def test_guard_refuses_without_a_valid_account_env_before_any_aws_call(
    test_profile, monkeypatch, value, why
):
    get_session = test_profile(TEST_ACCOUNT)
    if value is None:
        monkeypatch.delenv(seed_zombies.ACCOUNT_ENV)
    else:
        monkeypatch.setenv(seed_zombies.ACCOUNT_ENV, value)
    monkeypatch.setattr(seed_zombies.boto3, "Session", lambda **kw: pytest.fail("no AWS call"))
    with pytest.raises(seed_zombies.WrongAccountError, match=why):
        get_session()


def test_main_refuses_without_account_env(test_profile, monkeypatch, capsys):
    test_profile(TEST_ACCOUNT)
    monkeypatch.delenv(seed_zombies.ACCOUNT_ENV)
    monkeypatch.setattr(seed_zombies, "seed", lambda s: pytest.fail("seed must not run"))
    assert seed_zombies.main([]) == 2
    assert f"set {seed_zombies.ACCOUNT_ENV}" in capsys.readouterr().out


def test_guard_refuses_other_profile(test_profile):
    get_session = test_profile(TEST_ACCOUNT)
    with pytest.raises(seed_zombies.WrongAccountError, match="only 'tripwire-test'"):
        get_session("default")


@pytest.mark.parametrize(
    "run",
    [
        lambda: seed_zombies.main([]),
        lambda: seed_zombies.main(["--ttl-instance"]),
        cleanup_seed.main,
    ],
    ids=["seed", "seed-ttl", "cleanup"],
)
def test_main_refuses_wrong_account_without_touching_ec2(test_profile, monkeypatch, run):
    test_profile("123456789012")
    guarded = [
        (seed_zombies, "seed"),
        (seed_zombies, "seed_ttl_instance"),
        (cleanup_seed, "cleanup"),
    ]
    for module, name in guarded:
        monkeypatch.setattr(module, name, lambda s, n=name: pytest.fail(f"{n} must not run"))

    assert run() == 2


def test_ttl_flag_seeds_only_the_instance(test_profile, monkeypatch):
    test_profile(TEST_ACCOUNT)
    called = []
    monkeypatch.setattr(seed_zombies, "seed", lambda s: called.append("seed"))
    monkeypatch.setattr(seed_zombies, "seed_ttl_instance", lambda s: called.append("ttl"))

    assert seed_zombies.main(["--ttl-instance"]) == 0
    assert seed_zombies.main([]) == 0
    assert called == ["ttl", "seed"]


# --- seed / cleanup ---


def test_seed_creates_tagged_volume_and_eip_once(test_profile):
    session = test_profile(TEST_ACCOUNT)()
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
    session = test_profile(TEST_ACCOUNT)()
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


def instance(ec2, iid: str) -> dict:
    return ec2.describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]


def test_seed_ttl_instance_launches_tagged_t3_micro_once(test_profile):
    session = test_profile(TEST_ACCOUNT)()
    ec2 = session.client("ec2", region_name=REGION)
    ssm = session.client("ssm", region_name=REGION)
    ami = ssm.get_parameter(Name=seed_zombies.AMI_PARAMETER)["Parameter"]["Value"]

    iid = seed_zombies.seed_ttl_instance(session)
    again = seed_zombies.seed_ttl_instance(session)

    inst = instance(ec2, iid)
    tags = {t["Key"]: t["Value"] for t in inst["Tags"]}
    assert inst["InstanceType"] == "t3.micro"
    assert inst["ImageId"] == ami  # latest AL2023 from the public SSM parameter
    assert inst["MetadataOptions"]["HttpTokens"] == "required"
    assert tags["ttl"] == "30m" and tags["tripwire-seed"] == "true"
    assert again is None  # idempotent while the instance is alive
    assert ec2.describe_addresses()["Addresses"] == []  # no sweep zombies seeded


def test_seed_ttl_instance_again_after_termination(test_profile):
    session = test_profile(TEST_ACCOUNT)()
    ec2 = session.client("ec2", region_name=REGION)
    first = seed_zombies.seed_ttl_instance(session)
    ec2.terminate_instances(InstanceIds=[first])

    second = seed_zombies.seed_ttl_instance(session)

    assert second is not None and second != first


def test_cleanup_terminates_only_seed_instances(test_profile):
    session = test_profile(TEST_ACCOUNT)()
    ec2 = session.client("ec2", region_name=REGION)
    seed_iid = seed_zombies.seed_ttl_instance(session)
    keep_tagged = launch_instance(ec2, TagSpecifications=tag("tripwire-seed", "no", "instance"))
    keep = launch_instance(ec2)

    removed = cleanup_seed.cleanup(session)

    assert removed["instances"] == [seed_iid]
    assert instance(ec2, seed_iid)["State"]["Name"] in ("shutting-down", "terminated")
    assert instance(ec2, keep)["State"]["Name"] == "running"
    assert instance(ec2, keep_tagged)["State"]["Name"] == "running"
    assert cleanup_seed.cleanup(session)["instances"] == []  # already terminated: skipped


def test_cleanup_with_nothing_seeded(test_profile):
    session = test_profile(TEST_ACCOUNT)()

    assert cleanup_seed.cleanup(session) == {"instances": [], "volumes": [], "addresses": []}
