import io
import json
import logging
import urllib.error
import urllib.request

import pytest

from tests.helpers import client
from tripwire.common import notify
from tripwire.common.config import Config
from tripwire.common.models import Finding

FAKE_TOKEN = "123456:FAKE-TELEGRAM-TOKEN-for-tests"
FAKE_CHAT = "-100000000000"


def finding(rid: str, cost: float, detector: str = "ebs_unattached", age=None) -> Finding:
    return Finding(
        detector=detector,
        resource_id=rid,
        region="us-east-1",
        monthly_cost_usd=cost,
        age_days=age,
        reason="not attached to any instance",
        fix_command=f"aws ec2 delete-volume --volume-id {rid}",
    )


# --- format_digest ---


def test_digest_sorted_by_cost_with_total_and_mode():
    msg = notify.format_digest(
        [
            finding("vol-small", 1.60, age=12),
            finding("nat-big", 32.85, detector="nat_idle"),
            finding("eip-mid", 3.65, detector="eip_unassociated"),
        ],
        "observe",
    )
    lines = msg.splitlines()

    assert lines[0] == "🧟 CloudTripwire: 3 zombies found, about $38/month"
    assert lines[1].startswith("• NAT gateway nat-big (us-east-1): $32.85/mo")
    assert lines[2].startswith("• Elastic IP eip-mid (us-east-1): $3.65/mo")
    assert lines[3].startswith("• EBS volume vol-small (us-east-1): $1.60/mo")
    assert lines[3].endswith("flagged for 12 days")
    assert lines[-1] == "Mode: observe (nothing was changed)"


def test_digest_total_rounds_half_up_and_singular():
    msg = notify.format_digest([finding("vol-1", 2.50)], "enforce")

    assert msg.splitlines()[0] == "🧟 CloudTripwire: 1 zombie found, about $3/month"
    assert msg.splitlines()[-1].startswith("Mode: enforce")


def test_digest_all_clear():
    assert notify.format_digest([], "observe") == (
        "✅ CloudTripwire: no zombies found\nMode: observe (nothing was changed)"
    )


def test_digest_failure_warning_with_findings():
    msg = notify.format_digest([finding("vol-1", 1.0)], "observe", checks_failed=2)

    assert msg.splitlines()[-2:] == [
        "Warning: 2 checks failed, see logs",
        "Mode: observe (nothing was changed)",
    ]


def test_digest_failures_without_findings_is_not_an_all_clear():
    msg = notify.format_digest([], "observe", checks_failed=1)

    assert "✅" not in msg
    assert msg == (
        "⚠️ CloudTripwire: no zombies found in the checks that ran\n"
        "Warning: 1 check failed, see logs\n"
        "Mode: observe (nothing was changed)"
    )


def test_digest_truncation_keeps_failure_warning():
    findings = [finding(f"vol-{i:04d}", 1.0) for i in range(200)]

    msg = notify.format_digest(findings, "observe", checks_failed=3)

    assert len(msg) < notify.MAX_CHARS
    assert "Warning: 3 checks failed, see logs" in msg.splitlines()


def test_digest_truncates_under_limit_with_more_line():
    findings = [finding(f"vol-{i:04d}", 1.0) for i in range(200)]

    msg = notify.format_digest(findings, "observe")

    assert len(msg) < notify.MAX_CHARS
    shown = sum(1 for line in msg.splitlines() if line.startswith("• "))
    assert 0 < shown < 200
    assert f"+{200 - shown} more" in msg.splitlines()
    assert msg.splitlines()[-1] == "Mode: observe (nothing was changed)"
    assert "about $200/month" in msg  # total covers hidden findings too


def test_digest_no_more_line_when_everything_fits():
    msg = notify.format_digest([finding("vol-1", 1.0)], "observe")

    assert "more" not in msg


# --- send ---


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def channels(aws):
    ssm, sns = client("ssm"), client("sns")
    ssm.put_parameter(Name=notify.TELEGRAM_TOKEN_PARAM, Value=FAKE_TOKEN, Type="SecureString")
    ssm.put_parameter(Name=notify.TELEGRAM_CHAT_PARAM, Value=FAKE_CHAT, Type="SecureString")
    arn = sns.create_topic(Name="cloudtripwire-alerts")["TopicArn"]
    cfg = Config(channels=("telegram", "email"), alert_email="a@b.co", alert_topic_arn=arn)
    return cfg, ssm, sns


def test_send_telegram_and_email(channels, monkeypatch):
    cfg, ssm, sns = channels
    sent = []

    def fake_urlopen(req, timeout):
        sent.append((req, timeout))
        return FakeResponse(b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = notify.send("hello\nworld", "🧟 CloudTripwire: 1 zombie", cfg, ssm, sns)

    assert result == {"telegram": True, "email": True}
    [(req, timeout)] = sent
    assert timeout == 10
    assert req.get_method() == "POST"
    assert req.full_url == f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"
    assert json.loads(req.data) == {"chat_id": FAKE_CHAT, "text": "hello\nworld"}  # no parse_mode


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.HTTPError(
            f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage", 401, "Unauthorized", {}, None
        ),
        urllib.error.URLError(f"bad url https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"),
        TimeoutError(f"timed out: bot{FAKE_TOKEN}"),
    ],
)
def test_telegram_failure_never_leaks_token_and_email_still_sent(
    channels, monkeypatch, caplog, error
):
    cfg, ssm, sns = channels

    def failing_urlopen(req, timeout):
        raise error

    monkeypatch.setattr(urllib.request, "urlopen", failing_urlopen)
    caplog.set_level(logging.DEBUG)

    result = notify.send("hello", "subject", cfg, ssm, sns)

    assert result == {"telegram": False, "email": True}
    assert "notify_failed" in caplog.text
    assert FAKE_TOKEN not in caplog.text
    assert "123456" not in caplog.text


def test_telegram_ok_false_counts_as_failure(channels, monkeypatch):
    cfg, ssm, sns = channels
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda req, timeout: FakeResponse(b'{"ok": false}')
    )

    assert notify.send("hello", "s", cfg, ssm, sns)["telegram"] is False


def test_missing_ssm_parameter_fails_only_telegram(channels, monkeypatch, caplog):
    cfg, ssm, sns = channels
    ssm.delete_parameter(Name=notify.TELEGRAM_TOKEN_PARAM)

    result = notify.send("hello", "s", cfg, ssm, sns)

    assert result == {"telegram": False, "email": True}
    assert "ParameterNotFound" in caplog.text


def test_email_without_topic_arn_fails(channels):
    _, ssm, sns = channels
    cfg = Config(channels=("email",), alert_email="a@b.co")

    assert notify.send("hello", "s", cfg, ssm, sns) == {"email": False}


def test_slack_not_available_yet(channels):
    _, ssm, sns = channels

    assert notify.send("hello", "s", Config(channels=("slack",)), ssm, sns) == {"slack": False}


def test_sns_subject_is_ascii_single_line_max_100():
    subject = notify._sns_subject("🧟 CloudTripwire:\n" + "x" * 200)

    assert subject.startswith("CloudTripwire: xxx")
    assert len(subject) == 100
    assert notify._sns_subject("🧟") == "CloudTripwire"
