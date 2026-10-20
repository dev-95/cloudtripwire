import logging

from tripwire.common import regions


def test_get_client_is_cached(aws):
    assert regions.get_client("ec2", "us-east-1") is regions.get_client("ec2", "us-east-1")
    assert regions.get_client("ec2", "us-east-1") is not regions.get_client("ec2", "eu-west-1")


def test_all_enabled_regions_sorted(aws):
    result = regions.enabled_regions([])
    assert "us-east-1" in result
    assert result == sorted(result)


def test_configured_regions_intersected(aws, caplog):
    with caplog.at_level(logging.WARNING):
        result = regions.enabled_regions(["us-west-2", "us-east-1", "xx-fake-1"])
    assert result == ["us-east-1", "us-west-2"]
    assert "xx-fake-1" in caplog.text


def test_opted_out_regions_excluded(aws, monkeypatch):
    fake = {
        "Regions": [
            {"RegionName": "us-east-1", "OptInStatus": "opt-in-not-required"},
            {"RegionName": "af-south-1", "OptInStatus": "opted-in"},
            {"RegionName": "me-south-1", "OptInStatus": "not-opted-in"},
        ]
    }
    monkeypatch.setattr(
        regions.get_client("ec2"), "describe_regions", lambda **_: fake, raising=False
    )
    assert regions.enabled_regions([]) == ["af-south-1", "us-east-1"]
    assert regions.enabled_regions(["me-south-1"]) == []
