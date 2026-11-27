"""Deploy the CS310 scaffold from a terminal with a working AWS CLI profile."""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
from urllib.error import HTTPError
from urllib.request import urlopen

from package import ROOT, build
from rest_gateway import rest_gateway

AWS = shutil.which("aws") or str(
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Amazon/AWSCLIV2/aws.exe"
)


def ref(name):
    return {"Ref": name}


def attr(name, attribute="Arn"):
    return {"Fn::GetAtt": [name, attribute]}


def sub(value):
    return {"Fn::Sub": value}


def template(prefix, gateway="rest"):
    resources = {}

    def add(name, resource_type, **properties):
        resources[name] = {"Type": resource_type, "Properties": properties}

    for name, keys in {
        "Events": ("event_id",), "Seats": ("event_id", "seat_id"), "Orders": ("order_id",)
    }.items():
        add(
            name, "AWS::DynamoDB::Table",
            TableName=f"{prefix}-{name.lower()}", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"} for key in keys],
            KeySchema=[
                {"AttributeName": key, "KeyType": kind}
                for key, kind in zip(keys, ("HASH", "RANGE"))
            ],
        )

    add("DeadLetters", "AWS::SQS::Queue", QueueName=f"{prefix}-checkout-dlq",
        SqsManagedSseEnabled=True)
    add("Checkout", "AWS::SQS::Queue", QueueName=f"{prefix}-checkout",
        SqsManagedSseEnabled=True, VisibilityTimeout=60,
        RedrivePolicy={"deadLetterTargetArn": attr("DeadLetters"), "maxReceiveCount": 3})

    for logical, target, timeout in (("Api", "api", 15), ("Worker", "worker", 10)):
        function_name = f"{prefix}-{target}"
        add(f"{logical}Logs", "AWS::Logs::LogGroup",
            LogGroupName=f"/aws/lambda/{function_name}", RetentionInDays=14)
        table_actions = (
            ["GetItem", "Query", "Scan", "UpdateItem", "PutItem", "ConditionCheckItem"]
            if target == "api" else ["GetItem", "UpdateItem", "ConditionCheckItem"]
        )
        queue_actions = (
            ["SendMessage"] if target == "api"
            else ["ReceiveMessage", "DeleteMessage", "GetQueueAttributes"]
        )
        add(
            f"{logical}Role", "AWS::IAM::Role",
            AssumeRolePolicyDocument={"Version": "2012-10-17", "Statement": [{
                "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }]},
            Policies=[{"PolicyName": "ticketing-runtime", "PolicyDocument": {
                "Version": "2012-10-17", "Statement": [
                    {"Effect": "Allow", "Action": [f"dynamodb:{action}" for action in table_actions],
                     "Resource": [attr(name) for name in ("Events", "Seats", "Orders")]},
                    {"Effect": "Allow", "Action": [f"sqs:{action}" for action in queue_actions],
                     "Resource": attr("Checkout")},
                    {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                     "Resource": attr(f"{logical}Logs")},
                ],
            }}],
        )

        variables = {"PREFIX": prefix, "TICKET_SECRET": ref("TicketSecret")}
        if target == "api":
            variables["CHECKOUT_QUEUE_URL"] = ref("Checkout")
        else:
            variables["PAYMENT_FAILURE_RATE"] = "0.1"
        init_code = (
            'def lambda_handler(event, context):\n'
            '    raise RuntimeError("Deploy the project ZIP before use")\n'
        )
        add(
            f"{logical}Function", "AWS::Lambda::Function",
            FunctionName=function_name, Runtime="python3.12", MemorySize=256, Timeout=timeout,
            Role=attr(f"{logical}Role"), Handler="index.lambda_handler",
            Environment={"Variables": variables}, Code={"ZipFile": init_code},
        )

    add("WorkerMapping", "AWS::Lambda::EventSourceMapping", BatchSize=1,
        Enabled=True, EventSourceArn=attr("Checkout"), FunctionName=ref("WorkerFunction"))
    if gateway == "rest":
        gateway_resources, api_url = rest_gateway(prefix, ref("ApiFunction"), attr("ApiFunction"))
        resources.update(gateway_resources)
    else:
        add("HttpApi", "AWS::ApiGatewayV2::Api", Name=f"{prefix}-http", ProtocolType="HTTP")
        add("Integration", "AWS::ApiGatewayV2::Integration", ApiId=ref("HttpApi"),
            IntegrationType="AWS_PROXY", IntegrationUri=attr("ApiFunction"),
            PayloadFormatVersion="2.0")
        for name, route in {
            "EventSearch": "GET /events",
            "Hold": "POST /events/{event_id}/holds",
            "CheckoutRoute": "POST /events/{event_id}/holds/{hold_id}/checkout",
            "Order": "GET /orders/{order_id}",
            "Scan": "POST /tickets/scan",
            "SeatsRoute": "GET /events/{event_id}/seats",
            "Stats": "GET /events/{event_id}/stats",
            "DefaultRoute": "$default",
        }.items():
            add(name, "AWS::ApiGatewayV2::Route", ApiId=ref("HttpApi"), RouteKey=route,
                Target=sub("integrations/${Integration}"))
        add("Stage", "AWS::ApiGatewayV2::Stage", ApiId=ref("HttpApi"),
            StageName="$default", AutoDeploy=True,
            DefaultRouteSettings={"ThrottlingBurstLimit": 150, "ThrottlingRateLimit": 100})
        add("GatewayInvoke", "AWS::Lambda::Permission", FunctionName=ref("ApiFunction"),
            Action="lambda:InvokeFunction", Principal="apigateway.amazonaws.com",
            SourceArn=sub("arn:${AWS::Partition}:execute-api:${AWS::Region}:${AWS::AccountId}:${HttpApi}/*"))
        api_url = attr("HttpApi", "ApiEndpoint")
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Parameters": {"TicketSecret": {"Type": "String", "NoEcho": True, "MinLength": 32}},
        "Resources": resources,
        "Outputs": {
            "ApiUrl": {"Value": api_url},
            "CheckoutQueueUrl": {"Value": ref("Checkout")},
            "EventSourceMapping": {"Value": ref("WorkerMapping")},
        },
    }


def aws(args, *command, stream=False):
    result = subprocess.run(
        [AWS, *command, "--profile", args.profile, "--region", args.region,
         "--no-cli-pager", "--output", "json"],
        check=True, stdout=None if stream else subprocess.PIPE, encoding="utf-8",
    )
    return json.loads(result.stdout) if result.stdout and result.stdout.strip() else None


def deploy(args):
    identity = aws(args, "sts", "get-caller-identity")
    if identity["Account"] != args.account:
        raise SystemExit("AWS profile is authenticated to a different account.")

    stack = f"{args.prefix}-phase2"
    existing = aws(args, "cloudformation", "list-stacks", "--query",
                   f"StackSummaries[?StackName=='{stack}' && StackStatus!='DELETE_COMPLETE'].StackId")
    if existing:
        types = aws(args, "cloudformation", "list-stack-resources", "--stack-name", stack,
                    "--query", "StackResourceSummaries[].ResourceType")
        opposite = "AWS::ApiGatewayV2::Api" if args.gateway == "rest" else "AWS::ApiGateway::RestApi"
        if opposite in types:
            raise SystemExit("This stack uses the other API Gateway type. Use deploy_rest.py to attach REST without replacing the existing API.")
    secret_dir = ROOT / ".secrets"
    secret_dir.mkdir(exist_ok=True)
    secret_file = secret_dir / f"{args.prefix}-ticket-secret"
    try:
        with secret_file.open("x", encoding="ascii") as stream:
            stream.write(secrets.token_urlsafe(32))
    except FileExistsError:
        pass
    ticket_secret = secret_file.read_text(encoding="ascii").strip()
    if len(ticket_secret) < 32:
        raise SystemExit("Existing ticket secret file is invalid.")

    with tempfile.TemporaryDirectory(dir=secret_dir) as temporary:
        template_path = Path(temporary) / "stack.json"
        template_path.write_text(json.dumps(template(args.prefix, args.gateway)), encoding="utf-8")
        changes = Path(temporary) / "parameters.json"
        changes.write_text(json.dumps([{"ParameterKey": "TicketSecret", "ParameterValue": ticket_secret}]), encoding="ascii")
        print(f"Validating and deploying stack {stack} in {args.region}", flush=True)
        aws(args, "cloudformation", "validate-template", "--template-body", f"file://{template_path}")
        aws(args, "cloudformation", "deploy", "--template-file", str(template_path),
            "--stack-name", stack, "--parameter-overrides", f"file://{changes}",
            "--capabilities", "CAPABILITY_IAM", "--no-fail-on-empty-changeset", stream=True)

    stack_info = aws(args, "cloudformation", "describe-stacks", "--stack-name", stack)
    outputs = {item["OutputKey"]: item["OutputValue"] for item in stack_info["Stacks"][0]["Outputs"]}
    for target in ("api", "worker"):
        function = f"{args.prefix}-{target}"
        archive = build(target, ROOT / "dist")
        print(f"Uploading and waiting for {function}", flush=True)
        aws(args, "lambda", "update-function-code", "--function-name", function,
            "--zip-file", f"fileb://{archive}", "--query", "FunctionName")
        aws(args, "lambda", "wait", "function-updated-v2", "--function-name", function)
        aws(args, "lambda", "update-function-configuration", "--function-name", function,
            "--handler", f"{target}.handler.lambda_handler", "--query", "FunctionName")
        aws(args, "lambda", "wait", "function-updated-v2", "--function-name", function)

    mapping = aws(args, "lambda", "get-event-source-mapping", "--uuid", outputs["EventSourceMapping"])
    if mapping["State"] != "Enabled" or mapping["BatchSize"] != 1:
        raise SystemExit(f"Worker mapping not ready: {mapping['State']}")
    config = {
        "api_url": outputs["ApiUrl"], "region": args.region, "prefix": args.prefix,
        "stack_name": stack, "gateway_type": args.gateway, "checkout_queue_url": outputs["CheckoutQueueUrl"],
        "event_source_mapping": outputs["EventSourceMapping"],
    }
    verify_api(config["api_url"])
    config_dir = ROOT / "config"
    config_dir.mkdir(exist_ok=True)
    (config_dir / f"{args.prefix}.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"Verified API routing over HTTPS: {config['api_url']}")


def verify_api(api_url):
    try:
        with urlopen(api_url + f"/events/setup-check-{secrets.token_hex(8)}/seats", timeout=15) as response:
            status, body = response.status, response.read()
    except HTTPError as error:
        status, body = error.code, error.read()
    code = json.loads(body).get("error")
    if (status, code) not in {(404, "event_not_found"), (501, "not_implemented")}:
        raise SystemExit(f"Expected the API route probe response, got HTTP {status} ({code}).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True, help="Expected 12-digit AWS account ID")
    parser.add_argument("--profile", default="cs310")
    parser.add_argument("--region", default="us-east-2")
    parser.add_argument("--prefix", default="ticketing")
    parser.add_argument("--gateway", choices=("rest", "http"), default="rest")
    parser.add_argument("--template-only", action="store_true", help="Print template without AWS calls")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", args.prefix):
        parser.error("prefix must start with a lowercase letter and use at most 40 letters, digits or dashes")
    if not re.fullmatch(r"[0-9]{12}", args.account):
        parser.error("account must be a 12-digit AWS account ID")
    if args.template_only:
        print(json.dumps(template(args.prefix, args.gateway), indent=2))
    else:
        deploy(args)


if __name__ == "__main__":
    main()
