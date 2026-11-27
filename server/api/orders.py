from api.responses import error, response
from common.db import database


def get_order(params, headers, body):
    order = database().get_order(params["order_id"])
    if order is None:
        return error(404, "order_not_found", "Order not found")
    return response(200, {
        **{key: order[key] for key in (
            "order_id", "event_id", "hold_id", "buyer_id", "seats", "status", "paid",
        )},
        "tickets": order.get("tickets", []),
        "fail_reason": order.get("fail_reason"),
    })
