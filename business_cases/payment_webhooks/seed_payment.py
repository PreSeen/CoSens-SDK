"""Legacy payment callback handler. Public API must stay compatible."""


def new_store():
    return {"orders": {}}


def order_view(store, order_id):
    row = store["orders"].get(order_id)
    if row is None:
        return {"status": "pending", "captured_cents": 0, "refunded_cents": 0, "fulfillments": 0}
    return {key: row[key] for key in ("status", "captured_cents", "refunded_cents", "fulfillments")}


def apply_event(store, event):
    order_id = event["order_id"]
    row = store["orders"].setdefault(order_id, {
        "status": "pending", "captured_cents": 0, "refunded_cents": 0, "fulfillments": 0})
    if event["kind"] == "capture":
        row["captured_cents"] += event["amount_cents"]
        row["status"] = "paid"
        row["fulfillments"] += 1
    elif event["kind"] == "refund":
        row["refunded_cents"] += event["amount_cents"]
        row["status"] = "refunded" if row["refunded_cents"] == row["captured_cents"] else "partially_refunded"
    elif event["kind"] == "failed":
        row["status"] = "failed"
    return order_view(store, order_id)
