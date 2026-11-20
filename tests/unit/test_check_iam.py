import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("check_iam", ROOT / "scripts" / "check_iam.py")
check_iam = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_iam)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "iam.tf"
    path.write_text(text, encoding="utf-8")
    return path


def test_real_iam_tf_passes(capsys):
    assert check_iam.main([str(ROOT / "terraform" / "iam.tf")]) == 0
    assert "IAM guard: OK" in capsys.readouterr().out


def test_ci_file_holds_every_lambda_policy():
    """CI checks terraform/iam.tf only, so every role's actions must live there."""
    actions = set(check_iam.extract_actions((ROOT / "terraform" / "iam.tf").read_text()))
    for action in (
        "ec2:DescribeVolumes",  # sweep
        "rds:StopDBInstance",  # ttl
        "iam:UpdateAccessKey",  # killswitch
        "iam:GetAccessKeyLastUsed",
        "events:PutEvents",  # killswitch forwarder
    ):
        assert action in actions
    others = [p.name for p in (ROOT / "terraform").glob("*.tf") if p.name != "iam.tf"]
    for name in others:  # no IAM policy documents hiding in another file
        assert "aws_iam_policy_document" not in (ROOT / "terraform" / name).read_text(), name


def test_extracts_multiline_hcl_lists_and_skips_comments():
    text = """
    statement {
      actions = [
        "ec2:DescribeVolumes", # "ec2:DeleteVolume" in a comment is ignored
        "ec2:DescribeAddresses",
      ]
    }
    statement {
      actions = ["sns:Publish"]
    }
    """
    assert check_iam.extract_actions(text) == [
        "ec2:DescribeVolumes",
        "ec2:DescribeAddresses",
        "sns:Publish",
    ]


BAD_POLICY = """
data "aws_iam_policy_document" "bad" {
  statement {
    actions   = ["ec2:DescribeVolumes", "ec2:DeleteVolume", "ec2:TerminateInstances"]
    resources = ["*"]
  }
  statement {
    not_actions = ["ec2:ReleaseAddress"]
    resources   = ["*"]
  }
  statement {
    actions   = ["iam:*", "*", "s3:Get*"]
    resources = ["*"]
  }
}
"""


def test_bad_policy_fails_with_every_problem(tmp_path, capsys):
    path = write(tmp_path, BAD_POLICY)

    assert check_iam.main([str(path)]) == 1

    out = capsys.readouterr().out
    for action in (
        "ec2:DeleteVolume",
        "ec2:TerminateInstances",
        "ec2:ReleaseAddress",
        "iam:*",
        "'*'",
        "s3:Get*",
    ):
        assert action in out
    assert "ec2:DescribeVolumes" not in out
    assert "6 problem(s)" in out


@pytest.mark.parametrize(
    "text",
    [
        'policy = jsonencode({ Statement = [{ Action = "ec2:DeleteSnapshot" }] })',
        'policy = jsonencode({ Statement = [{ Action = ["sns:Publish", "dynamodb:*"] }] })',
        '{"Statement": [{"Effect": "Allow", "NotAction": "*", "Resource": "*"}]}',
    ],
)
def test_json_and_jsonencode_policies_are_checked(tmp_path, text):
    assert check_iam.check_file(write(tmp_path, text)) != []


@pytest.mark.parametrize(
    "action",
    ["ec2:deletevolume", "rds:DeleteDBInstance", "ec2:ReleaseHosts", "ec2:terminateinstances"],
)
def test_destructive_is_case_insensitive(action):
    assert check_iam.violations(action)


@pytest.mark.parametrize("action", ["ec2:StopInstances", "iam:UpdateAccessKey", "sns:Publish"])
def test_allowed_actions(action):
    assert check_iam.violations(action) == []


def test_file_without_actions_fails(tmp_path):
    [error] = check_iam.check_file(write(tmp_path, 'resource "aws_iam_role" "x" {}'))
    assert "no IAM actions found" in error
