import json


def response(status, payload):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(payload),
    }


def error(status, name, message):
    return response(status, {"error": name, "message": message})
