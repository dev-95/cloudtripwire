from datetime import UTC, datetime

from tripwire.common.models import Action, Finding


def test_finding_to_dict():
    f = Finding("ebs_unattached", "vol-1", "us-east-1", 1.6, 12, "unattached", "aws ec2 ...")
    assert f.to_dict() == {
        "detector": "ebs_unattached",
        "resource_id": "vol-1",
        "region": "us-east-1",
        "monthly_cost_usd": 1.6,
        "age_days": 12,
        "reason": "unattached",
        "fix_command": "aws ec2 ...",
    }


def test_action_to_dict_serialises_timestamp():
    a = Action(
        action="stop_instance",
        resource_id="i-1",
        region="us-east-1",
        mode="observe",
        performed=False,
        timestamp=datetime(2026, 10, 19, 12, 0, tzinfo=UTC),
        reason="ttl 4h expired",
        undo_command="aws ec2 start-instances --instance-ids i-1",
    )
    d = a.to_dict()
    assert d["timestamp"] == "2026-10-19T12:00:00+00:00"
    assert d["performed"] is False
    assert a.timestamp.tzinfo is UTC  # original untouched
