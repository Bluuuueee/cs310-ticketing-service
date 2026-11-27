"""CloudFormation resources for the demo REST API and its prod stage."""

import hashlib
import json
import re

STAGE = "prod"
METHODS = {
    "/": "get",
    "/app.css": "get",
    "/app.js": "get",
    "/events": "get",
    "/events/{event_id}/holds": "post",
    "/events/{event_id}/holds/{hold_id}/checkout": "post",
    "/events/{event_id}/seats": "get",
    "/events/{event_id}/stats": "get",
    "/orders/{order_id}": "get",
    "/tickets/scan": "post",
}


def rest_gateway(prefix, function, function_arn):
    """Return REST resources and the ApiUrl output for either deployment workflow."""
    integration = {
        "type": "aws_proxy",
        "httpMethod": "POST",
        "uri": {"Fn::Sub": [
            "arn:${AWS::Partition}:apigateway:${AWS::Region}:lambda:path/"
            "2015-03-31/functions/${DemoFunctionArn}/invocations",
            {"DemoFunctionArn": function_arn},
        ]},
    }
    paths = {}
    for path, method in METHODS.items():
        operation = {
            "responses": {"default": {"description": "Lambda proxy response"}},
            "x-amazon-apigateway-integration": integration,
        }
        parameters = [
            {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
            for name in re.findall(r"\{([^}]+)\}", path)
        ]
        if path == "/events":
            parameters.append({
                "name": "name", "in": "query", "required": True,
                "schema": {"type": "string", "maxLength": 120},
            })
        if parameters:
            operation["parameters"] = parameters
        paths[path] = {method: operation}

    spec = {
        "openapi": "3.0.1",
        "info": {"title": f"{prefix}-rest", "version": "1.0"},
        "paths": paths,
    }
    snapshot = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:12]
    deployment = f"RestDeployment{snapshot}"
    resources = {
        "RestApi": {
            "Type": "AWS::ApiGateway::RestApi",
            "Properties": {
                "Name": f"{prefix}-rest", "EndpointConfiguration": {"Types": ["REGIONAL"]},
                "Mode": "overwrite", "Body": spec,
            },
        },
        "RestUnknownRoute": {
            "Type": "AWS::ApiGateway::GatewayResponse",
            "Properties": {
                "RestApiId": {"Ref": "RestApi"},
                "ResponseType": "MISSING_AUTHENTICATION_TOKEN",
                "StatusCode": "404",
                "ResponseTemplates": {
                    "application/json": '{"error":"not_found","message":"Unknown route"}',
                },
            },
        },
        deployment: {
            "Type": "AWS::ApiGateway::Deployment",
            "DependsOn": "RestUnknownRoute",
            "Properties": {"RestApiId": {"Ref": "RestApi"}},
        },
        "RestStage": {
            "Type": "AWS::ApiGateway::Stage",
            "Properties": {
                "RestApiId": {"Ref": "RestApi"}, "DeploymentId": {"Ref": deployment},
                "StageName": STAGE,
                "MethodSettings": [{
                    "ResourcePath": "/*", "HttpMethod": "*",
                    "ThrottlingBurstLimit": 150, "ThrottlingRateLimit": 100,
                }],
            },
        },
        "RestGatewayInvoke": {
            "Type": "AWS::Lambda::Permission",
            "Properties": {
                "FunctionName": function, "Action": "lambda:InvokeFunction",
                "Principal": "apigateway.amazonaws.com",
                "SourceArn": {"Fn::Sub": (
                    "arn:${AWS::Partition}:execute-api:${AWS::Region}:${AWS::AccountId}:"
                    f"${{RestApi}}/{STAGE}/*/*"
                )},
            },
        },
    }
    url = {"Fn::Sub": f"https://${{RestApi}}.execute-api.${{AWS::Region}}.${{AWS::URLSuffix}}/{STAGE}"}
    return resources, url
