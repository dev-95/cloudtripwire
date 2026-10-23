"""Fail if an IAM policy grants destructive or wildcard actions.

Usage: python scripts/check_iam.py [FILE ...]   (default: terraform/iam.tf)

Reads IAM actions from Terraform `actions` / `not_actions` lists and from
jsonencode/JSON `Action` / `NotAction` values, then rejects:
  - Delete*, Terminate*, Release* actions (CloudTripwire stops, never deletes)
  - any wildcard: a bare "*", "<service>:*", or a partial one like "ec2:Describe*"
Also fails if a file has no actions at all, so a parsing miss can't pass silently.
"""

import re
import sys
from pathlib import Path

DEFAULT_FILES = [Path("terraform/iam.tf")]

# actions = ["a", "b"]  /  not_actions = [...]  (HCL, possibly multi-line)
_HCL_LIST = re.compile(r"\b(?:not_)?actions\s*=\s*\[(.*?)\]", re.DOTALL)
# "Action": "a"  /  Action = ["a", "b"]  (JSON or jsonencode)
_JSON_ACTION = re.compile(r"\"?\b(?:Not)?Action\"?\s*[:=]\s*(\[.*?\]|\"[^\"]*\")", re.DOTALL)
_STRING = re.compile(r"\"([^\"]*)\"")
_DESTRUCTIVE = re.compile(r"^[a-z0-9-]+:(delete|terminate|release)", re.IGNORECASE)


def _strip_comments(text: str) -> str:
    return re.sub(r"(?m)(#|//).*$", "", text)


def extract_actions(text: str) -> list[str]:
    text = _strip_comments(text)
    actions = []
    for pattern in (_HCL_LIST, _JSON_ACTION):
        for block in pattern.findall(text):
            actions.extend(_STRING.findall(block))
    return actions


def violations(action: str) -> list[str]:
    problems = []
    if "*" in action:
        problems.append("wildcard action")
    if _DESTRUCTIVE.match(action):
        problems.append("destructive action (Delete/Terminate/Release)")
    return problems


def check_file(path: Path) -> list[str]:
    """Return human-readable errors for one file (empty list = OK)."""
    actions = extract_actions(path.read_text(encoding="utf-8"))
    if not actions:
        return [f"{path}: no IAM actions found (parser miss or wrong file?)"]
    return [
        f"{path}: {action!r}: {problem}" for action in actions for problem in violations(action)
    ]


def main(argv: list[str]) -> int:
    files = [Path(a) for a in argv] or DEFAULT_FILES
    errors = [e for f in files for e in check_file(f)]
    for e in errors:
        print(e)
    if errors:
        print(f"IAM guard: {len(errors)} problem(s) found")
        return 1
    print(f"IAM guard: OK ({', '.join(str(f) for f in files)})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
