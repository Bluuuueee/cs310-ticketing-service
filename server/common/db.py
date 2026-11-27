import os
from functools import lru_cache
from time import time_ns


def now_ms() -> int:
    return time_ns() // 1_000_000


class SeatClaimConflict(Exception):
    """The requested seats could not be claimed in this attempt."""


@lru_cache(maxsize=1)
def database():
    # Import lazily so local tests and ZIP import checks need no AWS SDK.
    import boto3
    from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

    return Database(
        boto3.client("dynamodb"), os.environ["PREFIX"],
        TypeSerializer(), TypeDeserializer(),
    )


class Database:
    def __init__(self, client, prefix, serializer, deserializer):
        self.client = client
        self.events = f"{prefix}-events"
        self.seats = f"{prefix}-seats"
        self.orders = f"{prefix}-orders"
        self.serializer = serializer
        self.deserializer = deserializer

    def encode(self, values):
        return {key: self.serializer.serialize(value) for key, value in values.items()}

    def decode(self, values):
        return {key: self.deserializer.deserialize(value) for key, value in values.items()}

    def list_events(self):
        pages = self.client.get_paginator("scan").paginate(
            TableName=self.events, ConsistentRead=True,
            ProjectionExpression="event_id, #name, starts_at, ends_at, #timezone",
            ExpressionAttributeNames={"#name": "name", "#timezone": "timezone"},
        )
        return [self.decode(item) for page in pages for item in page["Items"]]

    def get_event(self, event_id):
        result = self.client.get_item(
            TableName=self.events, Key=self.encode({"event_id": event_id}),
            ConsistentRead=True,
        )
        return self.decode(result["Item"]) if "Item" in result else None

    def list_seats(self, event_id):
        pages = self.client.get_paginator("query").paginate(
            TableName=self.seats,
            KeyConditionExpression="event_id = :event",
            ExpressionAttributeValues=self.encode({":event": event_id}),
            ConsistentRead=True,
        )
        return [self.decode(item) for page in pages for item in page["Items"]]

    def list_orders(self, event_id):
        pages = self.client.get_paginator("scan").paginate(
            TableName=self.orders,
            FilterExpression="event_id = :event",
            ExpressionAttributeValues=self.encode({":event": event_id}),
            ConsistentRead=True,
        )
        return [self.decode(item) for page in pages for item in page["Items"]]

    def record_conflict(self, event_id):
        self.client.update_item(
            TableName=self.events, Key=self.encode({"event_id": event_id}),
            UpdateExpression="ADD conflicts :one",
            ExpressionAttributeValues=self.encode({":one": 1}),
        )

    def claim_seats(self, event_id, seat_ids, buyer, hold_id, now, expires, naive):
        values = {":held": "held", ":buyer": buyer, ":hid": hold_id, ":exp": expires}
        if not naive:
            values.update({":available": "available", ":now": now})
        updates = [
            {
                "TableName": self.seats,
                "Key": self.encode({"event_id": event_id, "seat_id": seat_id}),
                "UpdateExpression": (
                    "SET #st = :held, holder = :buyer, hold_id = :hid,"
                    " hold_expires_at_ms = :exp"
                ),
                "ExpressionAttributeNames": {"#st": "status"},
                "ExpressionAttributeValues": self.encode(values),
            }
            for seat_id in seat_ids
        ]
        if naive:
            for update in updates:
                self.client.update_item(**update)
            return
        for update in updates:
            update["ConditionExpression"] = (
                "#st = :available OR (#st = :held AND hold_expires_at_ms <= :now)"
            )
        try:
            self.client.transact_write_items(
                TransactItems=[{"Update": update} for update in updates],
            )
        except self.client.exceptions.TransactionCanceledException as exc:
            codes = {
                reason.get("Code")
                for reason in exc.response.get("CancellationReasons", [])
            } - {"None", None}
            if codes and codes <= {"ConditionalCheckFailed", "TransactionConflict"}:
                raise SeatClaimConflict from exc
            raise

    def get_order(self, order_id):
        result = self.client.get_item(
            TableName=self.orders, Key=self.encode({"order_id": order_id}),
            ConsistentRead=True,
        )
        return self.decode(result["Item"]) if "Item" in result else None

    def create_order(self, order):
        try:
            self.client.put_item(
                TableName=self.orders, Item=self.encode(order),
                ConditionExpression="attribute_not_exists(order_id)",
            )
        except self.client.exceptions.ConditionalCheckFailedException:
            return False
        return True

    def confirm_order(self, order, tickets, now, naive):
        updates = []
        for ticket in tickets:
            values = {
                ":sold": "sold", ":oid": order["order_id"],
                ":valid": "valid", ":code": ticket["code"],
            }
            update = {
                "TableName": self.seats,
                "Key": self.encode({"event_id": order["event_id"], "seat_id": ticket["seat_id"]}),
                "UpdateExpression": (
                    "SET #st = :sold, order_id = :oid, ticket_status = :valid, ticket_code = :code"
                ),
                "ExpressionAttributeNames": {"#st": "status"},
            }
            if not naive:
                update["ConditionExpression"] = (
                    "#st = :held AND hold_id = :hid AND hold_expires_at_ms > :now"
                )
                values.update({":held": "held", ":hid": order["hold_id"], ":now": now})
            update["ExpressionAttributeValues"] = self.encode(values)
            updates.append({"Update": update})

        updates.append({"Update": {
            "TableName": self.orders,
            "Key": self.encode({"order_id": order["order_id"]}),
            "UpdateExpression": (
                "SET #st = :confirmed, paid = :paid, tickets = :tickets, updated_at_ms = :now"
            ),
            "ConditionExpression": "#st = :pending",
            "ExpressionAttributeNames": {"#st": "status"},
            "ExpressionAttributeValues": self.encode({
                ":confirmed": "confirmed", ":paid": True, ":tickets": tickets,
                ":now": now, ":pending": "pending",
            }),
        }})
        try:
            self.client.transact_write_items(TransactItems=updates)
        except self.client.exceptions.TransactionCanceledException as exc:
            codes = [reason.get("Code") for reason in exc.response.get("CancellationReasons", [])]
            if len(codes) != len(updates):
                raise
            if codes[-1] == "ConditionalCheckFailed":
                return
            if "ConditionalCheckFailed" not in codes[:-1]:
                raise
            self.fail_expired_order(order["order_id"], now)

    def fail_expired_order(self, order_id, now):
        try:
            self.client.update_item(
                TableName=self.orders, Key=self.encode({"order_id": order_id}),
                UpdateExpression="SET #st = :failed, fail_reason = :reason, updated_at_ms = :now",
                ConditionExpression="#st = :pending",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues=self.encode({
                    ":failed": "failed", ":reason": "hold_expired",
                    ":now": now, ":pending": "pending",
                }),
            )
        except self.client.exceptions.ConditionalCheckFailedException:
            # A competing worker already moved the order out of pending.
            return


    def get_seat(self, event_id, seat_id):
        result = self.client.get_item(
            TableName=self.seats,
            Key=self.encode({"event_id": event_id, "seat_id": seat_id}),
            ConsistentRead=True,
        )
        return self.decode(result["Item"]) if "Item" in result else None

    def use_ticket(self, event_id, seat_id, order_id, gate, now):
        try:
            self.client.update_item(
                TableName=self.seats,
                Key=self.encode({"event_id": event_id, "seat_id": seat_id}),
                UpdateExpression="SET ticket_status = :used, used_at_ms = :now, used_gate = :gate",
                ConditionExpression="ticket_status = :valid AND order_id = :oid",
                ExpressionAttributeValues=self.encode({
                    ":used": "used", ":now": now, ":gate": gate,
                    ":valid": "valid", ":oid": order_id,
                }),
            )
        except self.client.exceptions.ConditionalCheckFailedException:
            return False
        return True


    def claim_receipt(self, order_id, recipient, retry_token=None):
        values = {
            ":sending": "sending", ":confirmed": "confirmed",
            ":expected": "failed" if retry_token else "pending", ":email": recipient,
        }
        update = "SET receipt_status = :sending"
        condition = "#st = :confirmed AND receipt_status = :expected AND receipt_email = :email"
        if retry_token:
            values[":token"] = retry_token
            update += ", receipt_retry_token = :token"
            condition += " AND (attribute_not_exists(receipt_retry_token) OR receipt_retry_token <> :token)"
        try:
            self.client.update_item(
                TableName=self.orders, Key=self.encode({"order_id": order_id}),
                UpdateExpression=update, ConditionExpression=condition,
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues=self.encode(values),
            )
        except self.client.exceptions.ConditionalCheckFailedException:
            return False
        return True

    def finish_receipt(self, order_id, status, error_code=None):
        values = {":status": status, ":sending": "sending"}
        expression = "SET receipt_status = :status"
        if error_code:
            expression += ", receipt_error = :error"
            values[":error"] = error_code
        else:
            expression += " REMOVE receipt_error"
        self.client.update_item(
            TableName=self.orders, Key=self.encode({"order_id": order_id}),
            UpdateExpression=expression,
            ConditionExpression="receipt_status = :sending",
            ExpressionAttributeValues=self.encode(values),
        )
