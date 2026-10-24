import pytest

from tripwire.common.config import Config, ConfigError, load_config


def test_defaults():
    assert load_config({}) == Config()


def test_safety_defaults():
    cfg = load_config({})
    assert cfg.mode == "observe"  # sweep + TTL only report until the user opts in
    assert cfg.killswitch_mode == "enforce"  # ADR 0002: leaked keys are exploited in minutes


def test_full_env():
    cfg = load_config(
        {
            "MODE": " Enforce ",
            "KILLSWITCH_MODE": "Observe",
            "REGIONS": "us-east-1, ca-central-1,,",
            "SWEEP_SCHEDULE": "cron(0 9 * * ? *)",
            "TTL_SCHEDULE": "rate(30 minutes)",
            "LAB_MODE": "TRUE",
            "LAB_GRACE_HOURS": "12",
            "SNAPSHOT_MAX_AGE_DAYS": "30",
            "STOPPED_INSTANCE_DAYS": "3",
            "NAT_IDLE_DAYS": "14",
            "KILLSWITCH_LOOKBACK_MINUTES": "120",
            "CHANNELS": "telegram,Email",
            "ALERT_EMAIL": "me@example.com",
            "TAGS": '{"team": "me"}',
            "STATE_TABLE": "cloudtripwire-state",
            "ALERT_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:cloudtripwire-alerts",
        }
    )
    assert cfg.state_table == "cloudtripwire-state"
    assert cfg.alert_topic_arn == "arn:aws:sns:us-east-1:123456789012:cloudtripwire-alerts"
    assert cfg.mode == "enforce"
    assert cfg.killswitch_mode == "observe"
    assert cfg.regions == ("us-east-1", "ca-central-1")
    assert cfg.lab_mode is True
    assert cfg.lab_grace_hours == 12
    assert cfg.killswitch_lookback_minutes == 120
    assert cfg.channels == ("telegram", "email")
    assert cfg.tags == {"team": "me"}
    assert cfg.ttl_schedule == "rate(30 minutes)"


def test_empty_regions_means_all():
    assert load_config({"REGIONS": ""}).regions == ()


def test_config_is_frozen():
    with pytest.raises(AttributeError):
        load_config({}).mode = "enforce"


def test_all_errors_reported_at_once():
    with pytest.raises(ConfigError) as exc:
        load_config(
            {
                "MODE": "yolo",
                "KILLSWITCH_MODE": "maybe",
                "LAB_MODE": "yes",
                "LAB_GRACE_HOURS": "0",
                "NAT_IDLE_DAYS": "-1",
                "SNAPSHOT_MAX_AGE_DAYS": "ten",
                "CHANNELS": "telegram,pager,email",
                "TAGS": "[1]",
            }
        )
    msg = str(exc.value)
    for expected in (
        "MODE",
        "KILLSWITCH_MODE",
        "LAB_MODE",
        "LAB_GRACE_HOURS",
        "NAT_IDLE_DAYS",
        "SNAPSHOT_MAX_AGE_DAYS",
        "pager",
        "ALERT_EMAIL",
        "TAGS",
    ):
        assert expected in msg


def test_empty_channels_rejected():
    with pytest.raises(ConfigError, match="at least one channel"):
        load_config({"CHANNELS": " , "})


def test_slack_rejected_until_v02():
    with pytest.raises(ConfigError, match="'slack' is not available until v0.2"):
        load_config({"CHANNELS": "telegram,Slack"})


def test_email_channel_requires_address():
    with pytest.raises(ConfigError, match="ALERT_EMAIL"):
        load_config({"CHANNELS": "email"})
    assert load_config({"CHANNELS": "email", "ALERT_EMAIL": "a@b.co"}).alert_email == "a@b.co"
