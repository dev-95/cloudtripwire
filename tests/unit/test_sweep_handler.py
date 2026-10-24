import json
import logging
from datetime import UTC, datetime

import pytest

from tests.helpers import client, clients, create_state_table, dynamodb
from tripwire.common import notify, regions
from tripwire.sweep import handler

TABLE = "cloudtripwire-state"
MONDAY = datetime(2026, 10, 19, 13, 0, tzinfo=UTC)
TUESDAY = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)


def failing_detector(*args):
    raise RuntimeError("simulated AccessDenied")


@pytest.fixture
def sweep(aws, monkeypatch):
    """Handler wired to moto, with cached test clients and notify.send captured."""
    monkeypatch.setenv("STATE_TABLE", TABLE)
    monkeypatch.setenv("REGIONS", "us-east-1,ca-central-1")
    monkeypatch.setattr(regions, "get_client", lambda s, r=None: client(s, r or "us-east-1"))
    monkeypatch.setattr(handler, "_region_clients", clients)
    monkeypatch.setattr(handler, "_dynamodb", dynamodb)
    monkeypatch.setattr(handler, "_now", lambda: TUESDAY)
    create_state_table(TABLE)
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        notify, "send", lambda msg, subject, *a: sent.append((msg, subject)) or {"telegram": True}
    )
    return sent


def fail_in(monkeypatch, region: str, detector: str) -> None:
    """Make one (region, detector) pair raise; everything else runs normally."""
    wrapped = []
    for name, detect in handler.DETECTORS:
        if name == detector:

            def detect(c, r, cfg, now, _real=detect):
                return failing_detector() if r == region else _real(c, r, cfg, now)

        wrapped.append((name, detect))
    monkeypatch.setattr(handler, "DETECTORS", wrapped)


def finding_item(region: str, resource_id: str, detector: str) -> dict:
    key = {"pk": f"FINDING#{region}#{resource_id}", "sk": detector}
    return dynamodb().Table(TABLE).get_item(Key=key)["Item"]


def test_zombies_in_two_regions_produce_one_digest(sweep):
    vol = clients().ec2.create_volume(AvailabilityZone="us-east-1a", Size=20, VolumeType="gp3")
    eip = clients("ca-central-1").ec2.allocate_address(Domain="vpc")

    summary = handler.handler({}, None)

    [(msg, subject)] = sweep
    assert subject == "🧟 CloudTripwire: 2 zombies found, about $5/month"
    assert vol["VolumeId"] in msg and eip["AllocationId"] in msg
    assert "Warning" not in msg
    assert summary["regions_scanned"] == 2
    assert summary["checks_failed"] == 0
    assert summary["findings_count"] == 2
    assert summary["total_monthly_cost_usd"] == 5.25
    assert summary["duration_s"] >= 0
    assert finding_item("us-east-1", vol["VolumeId"], "ebs_unattached")["status"] == "open"


def test_failing_detector_does_not_block_others_or_resolve_its_findings(sweep, monkeypatch):
    ca = clients("ca-central-1").ec2
    vol = ca.create_volume(AvailabilityZone="ca-central-1a", Size=10)["VolumeId"]
    eip = ca.allocate_address(Domain="vpc")["AllocationId"]
    handler.handler({}, None)  # both recorded as open

    # Both resources go away, but ebs_unattached fails in ca-central-1 this time.
    ca.delete_volume(VolumeId=vol)
    ca.release_address(AllocationId=eip)
    us_vol = clients().ec2.create_volume(AvailabilityZone="us-east-1a", Size=1)["VolumeId"]
    fail_in(monkeypatch, "ca-central-1", "ebs_unattached")

    summary = handler.handler({}, None)

    assert summary["checks_failed"] == 1
    assert summary["findings_count"] == 1  # us-east-1 EBS still detected
    assert finding_item("ca-central-1", vol, "ebs_unattached")["status"] == "open"
    assert finding_item("ca-central-1", eip, "eip_unassociated")["status"] == "resolved"
    assert finding_item("us-east-1", us_vol, "ebs_unattached")["status"] == "open"
    msg, _ = sweep[-1]
    assert us_vol in msg
    assert "Warning: 1 check failed, see logs" in msg


def test_no_findings_sends_nothing_except_monday(sweep, monkeypatch):
    handler.handler({}, None)
    assert sweep == []  # Tuesday: silent

    monkeypatch.setattr(handler, "_now", lambda: MONDAY)
    handler.handler({}, None)

    [(msg, _)] = sweep
    assert msg.startswith("✅ CloudTripwire: no zombies found")


def test_failure_warning_always_sent(sweep, monkeypatch):
    fail_in(monkeypatch, "us-east-1", "nat_idle")

    summary = handler.handler({}, None)  # Tuesday, nothing found

    [(msg, _)] = sweep
    assert "✅" not in msg
    assert "Warning: 1 check failed, see logs" in msg
    assert summary["checks_failed"] == 1
    assert summary["findings_count"] == 0
    assert summary["total_monthly_cost_usd"] == 0.0
    assert isinstance(summary["total_monthly_cost_usd"], float)


def test_failure_and_summary_are_logged(sweep, monkeypatch, caplog):
    fail_in(monkeypatch, "ca-central-1", "nat_idle")
    caplog.set_level(logging.INFO)

    summary = handler.handler({}, None)

    events = [json.loads(r.getMessage()) for r in caplog.records if r.getMessage().startswith("{")]
    [failure] = [e for e in events if e["event"] == "check_failed"]
    assert failure["region"] == "ca-central-1"
    assert failure["detector"] == "nat_idle"
    assert failure["error"] == "RuntimeError"
    [logged] = [e for e in events if e["event"] == "sweep_summary"]
    assert logged == {"event": "sweep_summary"} | summary
