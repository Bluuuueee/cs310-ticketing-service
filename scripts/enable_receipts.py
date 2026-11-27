"""Enable a verified, one-recipient Amazon SES demo on the existing Lambdas."""

import argparse
import json
from pathlib import Path
import re
import sys
import tempfile

from package import ROOT, build
from setup import aws

sys.path.insert(0, str(ROOT / "server"))
from common.receipt_config import valid_email


def update_environment(args, name, additions, directory):
    current = aws(args, "lambda", "get-function-configuration", "--function-name", name,
                  "--query", "{Variables:Environment.Variables,Revision:RevisionId}")
    changes = {"FunctionName": name, "RevisionId": current["Revision"],
               "Environment": {"Variables": {**(current["Variables"] or {}), **additions}}}
    path = directory / f"{name}.json"
    path.write_text(json.dumps(changes), encoding="ascii")
    try:
        aws(args, "lambda", "update-function-configuration", "--cli-input-json", f"file://{path}",
            "--query", "FunctionName")
    finally:
        path.unlink()
    aws(args, "lambda", "wait", "function-updated-v2", "--function-name", name)


def receipt_policy(args, partition):
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": ["ses:SendEmail", "ses:SendRawEmail"],
            "Resource": f"arn:{partition}:ses:{args.region}:{args.account}:identity/{args.sender}",
            "Condition": {
                "StringEquals": {"ses:FromAddress": args.sender},
                "ForAllValues:StringEquals": {"ses:Recipients": [args.recipient]},
                "Null": {"ses:Recipients": "false"},
            },
        }],
    }


def enable(args):
    identity = aws(args, "sts", "get-caller-identity")
    if identity["Account"] != args.account:
        raise SystemExit("AWS profile is authenticated to a different account.")
    for address in dict.fromkeys((args.sender, args.recipient)):
        if aws(args, "sesv2", "get-email-identity", "--email-identity", address,
               "--query", "VerifiedForSendingStatus") is not True:
            raise SystemExit("First verify both SES email identities in the selected region.")

    partition = identity["Arn"].split(":")[1]
    worker, api = f"{args.prefix}-worker", f"{args.prefix}-api"
    role = aws(args, "lambda", "get-function-configuration", "--function-name", worker,
               "--query", "Role")
    aws(args, "lambda", "get-function-configuration", "--function-name", api, "--query", "FunctionName")
    policy = receipt_policy(args, partition)
    private = ROOT / ".secrets"
    private.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=private) as temp:
        directory = Path(temp)
        iam_file = directory / "iam-policy.json"
        iam_file.write_text(json.dumps(policy), encoding="ascii")
        aws(args, "iam", "put-role-policy", "--role-name", role.rsplit("/", 1)[-1],
            "--policy-name", "TicketingRestrictedSesDemo", "--policy-document", f"file://{iam_file}")
        if args.permissions_only:
            print("Restricted SES permissions updated.")
            return
        for target, function, environment in (
            ("worker", worker, {"SES_FROM_EMAIL": args.sender, "SES_TEST_RECIPIENT": args.recipient}),
            ("api", api, {"SES_TEST_RECIPIENT": args.recipient}),
        ):
            archive = build(target, ROOT / "dist")
            print(f"Updating {function}", flush=True)
            aws(args, "lambda", "update-function-code", "--function-name", function,
                "--zip-file", f"fileb://{archive}", "--query", "FunctionName")
            aws(args, "lambda", "wait", "function-updated-v2", "--function-name", function)
            update_environment(args, function, environment, directory)

    print("SES demo configured. Reload the checkout page to test an email to the verified address.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True, help="Expected 12-digit AWS Account ID")
    parser.add_argument("--profile", default="cs310")
    parser.add_argument("--region", default="us-east-2")
    parser.add_argument("--prefix", default="ticketing")
    parser.add_argument("--sender", required=True)
    parser.add_argument("--recipient", required=True)
    parser.add_argument("--permissions-only", action="store_true",
                        help="Repair the SES role policy without changing Lambda code or settings")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]{12}", args.account):
        parser.error("account must be a 12-digit AWS Account ID")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", args.prefix):
        parser.error("invalid resource prefix")
    if not valid_email(args.sender) or not valid_email(args.recipient):
        parser.error("enter valid ASCII email addresses for both identities")
    enable(args)


if __name__ == "__main__":
    main()
