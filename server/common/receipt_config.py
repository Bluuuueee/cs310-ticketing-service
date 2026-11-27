import os
import re


def valid_email(value):
    return (
        isinstance(value, str) and len(value) <= 254 and
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+\-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", value)
        is not None
    )


def test_recipient():
    value = os.environ.get("SES_TEST_RECIPIENT", "").strip()
    return value if valid_email(value) else ""
