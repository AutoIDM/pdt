"""Write the permission policy of the AWS role that verify:aws signs in with.

    uv run python verify/scripts/aws_role.py --profile <admin profile>

The policy holds every action pdt deploy and destroy need, plus every read
action that inventory.py calls, so the role keeps up with both. The profile
must be allowed to change IAM roles in the verify account. Without
--profile the script prints the policy and changes nothing.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS.parent.parent / "src"))

from pdt.deploy_aws import deployer_policy
from pdt.deploy_aws_batch import DEPLOYER_ACTIONS

ROLE = "pdt-verify"
REGION = "us-east-1"
# The CLI names a service differently from IAM in this one case.
IAM_SERVICES = {"resourcegroupstaggingapi": "tag"}


def inventory_actions() -> list[str]:
    calls = re.findall(r'aws\(region, "([a-z-]+)", "([a-z-]+)"',
                       (SCRIPTS / "inventory.py").read_text())
    return sorted({f"{IAM_SERVICES.get(service, service)}:"
                   + "".join(word.title() for word in operation.split("-"))
                   for service, operation in calls})


def policy(account: str) -> dict:
    document = deployer_policy(DEPLOYER_ACTIONS, account, REGION)
    reads = [action for action in inventory_actions() if action not in DEPLOYER_ACTIONS]
    document["Statement"].append({"Effect": "Allow", "Action": reads, "Resource": ["*"]})
    return document


def aws(profile: str, *args: str) -> str:
    return subprocess.run(["pdt", "aws", *args, "--profile", profile, "--output", "json"],
                          check=True, capture_output=True, text=True).stdout


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="aws_role.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", help="AWS profile that may change IAM roles")
    parser.add_argument("--account", default="<account>",
                        help="account id to put in the printed policy")
    args = parser.parse_args(argv)
    if not args.profile:
        print(json.dumps(policy(args.account), indent=2))
        return 0
    account = json.loads(aws(args.profile, "sts", "get-caller-identity"))["Account"]
    aws(args.profile, "iam", "put-role-policy", "--role-name", ROLE, "--policy-name", ROLE,
        "--policy-document", json.dumps(policy(account), separators=(",", ":")))
    print(f"wrote policy {ROLE} on role {ROLE} in account {account}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
