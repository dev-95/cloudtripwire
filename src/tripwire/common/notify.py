"""Telegram / Slack / SNS senders + formatting.

Secrets (bot token, chat id) are read from SSM at send time and never logged: the
Telegram URL contains the token, so errors are logged by type and status code only.
"""

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal

from botocore.exceptions import ClientError

from tripwire.common.config import Config
from tripwire.common.models import Finding

logger = logging.getLogger(__name__)
# botocore logs full response bodies at DEBUG, including decrypted SSM SecureStrings.
# Pin it to INFO so a DEBUG log level (e.g. Lambda advanced logging) can't leak the token.
logging.getLogger("botocore").setLevel(logging.INFO)

MAX_CHARS = 4000  # Telegram allows 4096; keep a margin
TIMEOUT_SECONDS = 10
SNS_SUBJECT_MAX = 100

TELEGRAM_TOKEN_PARAM = "/cloudtripwire/telegram_bot_token"
TELEGRAM_CHAT_PARAM = "/cloudtripwire/telegram_chat_id"

LABELS = {
    "ebs_unattached": "EBS volume",
    "eip_unassociated": "Elastic IP",
    "nat_idle": "NAT gateway",
    "elb_no_targets": "Load balancer",
    "snapshots_old": "Snapshot",
    "ec2_stopped_long": "Stopped instance",
}


def _finding_line(f: Finding) -> str:
    line = f"• {LABELS.get(f.detector, f.detector)} {f.resource_id} ({f.region}):"
    line += f" ${f.monthly_cost_usd:.2f}/mo, {f.reason}"
    if f.age_days:
        line += f", flagged for {f.age_days} days"
    return line


def format_digest(findings: Sequence[Finding], mode: str, checks_failed: int = 0) -> str:
    """Daily digest, most expensive first, kept under MAX_CHARS with a "+N more" line.

    checks_failed > 0 adds a warning (never truncated), so a broken scan can't pass
    for an all-clear.
    """
    mode_line = (
        "Mode: observe (nothing was changed)"
        if mode == "observe"
        else "Mode: enforce (the sweep only reports; it never changes resources)"
    )
    checks = "check" if checks_failed == 1 else "checks"
    warning = [f"Warning: {checks_failed} {checks} failed, see logs"] if checks_failed else []
    if not findings:
        header = (
            "⚠️ CloudTripwire: no zombies found in the checks that ran"
            if checks_failed
            else "✅ CloudTripwire: no zombies found"
        )
        return "\n".join([header, *warning, mode_line])

    footer = ["Fix commands: in the sweep Lambda's CloudWatch logs.", *warning, mode_line]

    ordered = sorted(findings, key=lambda f: f.monthly_cost_usd, reverse=True)
    total = Decimal(str(sum(f.monthly_cost_usd for f in ordered)))
    total = total.quantize(Decimal("1"), ROUND_HALF_UP)
    noun = "zombie" if len(ordered) == 1 else "zombies"
    header = f"🧟 CloudTripwire: {len(ordered)} {noun} found, about ${total:,}/month"

    lines = [_finding_line(f) for f in ordered]
    body: list[str] = []
    for line in lines:
        rest = len(lines) - len(body) - 1  # lines still hidden if this one is added
        more = [f"+{rest} more"] if rest else []
        if len("\n".join([header, *body, line, *more, *footer])) >= MAX_CHARS:
            break
        body.append(line)
    if hidden := len(lines) - len(body):
        body.append(f"+{hidden} more")
    return "\n".join([header, *body, *footer])


def _log_failure(channel: str, error: Exception) -> None:
    # Only the type and status/code: str(error) can contain the Telegram URL (and token).
    detail: dict[str, object] = {"event": "notify_failed", "channel": channel}
    detail["error"] = type(error).__name__
    if isinstance(error, urllib.error.HTTPError):
        detail["status"] = error.code
    elif isinstance(error, ClientError):
        detail["code"] = error.response.get("Error", {}).get("Code")
    logger.error(json.dumps(detail))


def _secret(ssm, name: str) -> str:
    return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def _send_telegram(message: str, ssm) -> None:
    token, chat_id = _secret(ssm, TELEGRAM_TOKEN_PARAM), _secret(ssm, TELEGRAM_CHAT_PARAM)
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": message}).encode(),  # plain text
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        if not json.loads(resp.read()).get("ok"):
            raise RuntimeError("Telegram API returned ok=false")


def _sns_subject(subject: str) -> str:
    # SNS subjects: ASCII, no line breaks, at most 100 characters.
    ascii_only = subject.encode("ascii", "ignore").decode()
    return " ".join(ascii_only.split())[:SNS_SUBJECT_MAX] or "CloudTripwire"


def _send_email(message: str, subject: str, config: Config, sns) -> None:
    if not config.alert_topic_arn:
        raise ValueError("ALERT_TOPIC_ARN is not set")
    sns.publish(TopicArn=config.alert_topic_arn, Subject=_sns_subject(subject), Message=message)


def send(message: str, subject: str, config: Config, ssm, sns) -> dict[str, bool]:
    """Send to every configured channel. A failing channel is logged and skipped."""
    results = {}
    for channel in config.channels:
        try:
            if channel == "telegram":
                _send_telegram(message, ssm)
            elif channel == "email":
                _send_email(message, subject, config, sns)
            else:
                raise NotImplementedError(f"{channel} channel is not available yet")
        except Exception as e:  # one channel must never block the others
            _log_failure(channel, e)
            results[channel] = False
        else:
            logger.info(json.dumps({"event": "notify_sent", "channel": channel}))
            results[channel] = True
    return results
