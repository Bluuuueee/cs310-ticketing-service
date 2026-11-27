# Contracts

Everything in this file is shared between the two halves of the project. Agree on it first, then change it only together. If you change something here, change it in the same pull request as the code that depends on it.

Ownership: **A** = seats (operations 1 and 3), **B** = orders and client (operation 2).

---

## 1. Names and IDs

| Thing | Format | Example | Who creates it |
|---|---|---|---|
| Prefix | lowercase letters, digits, dashes | `matt`, `alex`, `prod` | You, when running the setup script |
| Event ID | lowercase letters, digits, dashes, no `\|` | `concert-1` | Seed script |
| Seat ID | row letter, dash, seat number | `A-3` | Seed script |
| Buyer ID | any string without `\|` | `bot-17`, `matt` | Client, sent in the `X-Buyer-Id` header |
| Hold ID | `h-` + 12 hex characters | `h-3f9a0c21b7d4` | Server, operation 1 |
| Order ID | the client's idempotency key: 32 hex characters (a UUID4 without dashes) | `9b1c...` | Client, sent in the `Idempotency-Key` header |

Rows are `A` (front) to `E` (back), with `row_index` 0 to 4. Seats are numbered 1 to 10 from left to right.

## 2. Time

All times are **integer milliseconds since 1970** (Unix epoch), named `*_ms`. Use `common.db.now_ms()`. Never store floats in DynamoDB.

## 3. DynamoDB tables

Table names are `{prefix}-events`, `{prefix}-seats`, `{prefix}-orders`. All use on-demand capacity.

### `{prefix}-events`, key: `event_id`

| Field | Type | Meaning |
|---|---|---|
| `event_id` | S | Partition key |
| `rows` | N | 5 |
| `seats_per_row` | N | 10 |
| `capacity` | N | 50 |
| `hold_seconds` | N | How long a hold lasts. 30 for the demo |
| `mode` | S | `correct` or `naive` |
| `conflicts` | N | Number of failed seat claims, for stats. Updated with `ADD`, outside any transaction |
| `created_at_ms` | N | |

### `{prefix}-seats`, key: `event_id` + `seat_id`

| Field | Type | Meaning |
|---|---|---|
| `event_id` | S | Partition key |
| `seat_id` | S | Sort key |
| `row` | S | `A` to `E` |
| `row_index` | N | 0 to 4 |
| `seat_num` | N | 1 to 10 |
| `status` | S | `available`, `held`, or `sold` |
| `holder` | S | Buyer ID while held or sold |
| `hold_id` | S | Set while held, kept after sale |
| `hold_expires_at_ms` | N | Set while held |
| `order_id` | S | Set when sold |
| `ticket_status` | S | `valid` or `used`, set when sold |
| `ticket_code` | S | Set when sold |
| `used_at_ms` | N | Set on first valid scan |
| `used_gate` | S | Set on first valid scan |

A seat is **free** if `status = available`, or if `status = held` and `hold_expires_at_ms <= now`. Nothing runs when a hold expires. The next claim treats the seat as free.

`status` is a reserved word in DynamoDB expressions. Always refer to it as `#st` with `ExpressionAttributeNames={"#st": "status"}`.

### `{prefix}-orders`, key: `order_id`

| Field | Type | Meaning |
|---|---|---|
| `order_id` | S | Partition key, equal to the idempotency key |
| `event_id` | S | |
| `hold_id` | S | |
| `buyer_id` | S | |
| `seats` | L of S | Seat IDs in the hold |
| `status` | S | `pending`, `confirmed`, or `failed` |
| `paid` | BOOL | True once payment succeeded |
| `tickets` | L of M | `[{"seat_id": "A-3", "code": "..."}]`, set when confirmed |
| `fail_reason` | S | `hold_expired` when status is `failed` |
| `created_at_ms` | N | |
| `updated_at_ms` | N | |

## 4. API

Base URL: the `api_url` in `config/{prefix}.json`, written by the setup script.

All request and response bodies are JSON. All errors use this shape and an HTTP status from 400 to 599:

```json
{"error": "no_block_available", "message": "No block of 4 adjacent free seats"}
```

Common errors on every route: `400 invalid_json`, `404 not_found` (unknown route), `500 internal_error`, `501 not_implemented` (scaffold stub not filled in yet).

### 4.1 Operation 1: Hold best seats (A)

`POST /events/{event_id}/holds`

Headers: `X-Buyer-Id`

Body, one of:
```json
{"party_size": 2}
{"seats": ["A-3", "A-4"]}
```

Rules:
- `party_size` is an integer from 1 to 4. `seats` is a list of 1 to 4 seat IDs.
- With `party_size`: find every block of `party_size` adjacent free seats in one row, score each block (section 4.1.1), and claim the lowest-scoring block in one `TransactWriteItems` call. If the transaction is cancelled because a seat was taken, add 1 to `events.conflicts`, re-read the seats, and try again. At most 3 attempts.
- With `seats`: skip the search and claim exactly those seats. One attempt.
- Claim transaction, one `Update` per seat:
  `SET #st = :held, holder = :buyer, hold_id = :hid, hold_expires_at_ms = :exp`
  condition `#st = :available OR (#st = :held AND hold_expires_at_ms <= :now)`.
- In `naive` mode: read the seats, pick the block, and write each seat with a plain `UpdateItem` and no condition. No transaction, no retry.

`201` response:
```json
{"hold_id": "h-3f9a0c21b7d4", "event_id": "concert-1", "seats": ["A-3", "A-4"], "expires_at_ms": 1764000030000}
```

Errors: `400 missing_buyer_id`, `400 invalid_party_size`, `400 invalid_seats`, `404 event_not_found`, `409 no_block_available`, `409 seats_taken`.

#### 4.1.1 Scoring

For a seat in row index $r$ with seat number $s$, in a row of $n$ seats with center $c = (n + 1) / 2$:

$$\text{score}(r, s) = r + 0.5 \cdot |s - c|$$

A block's score is the sum of its seats' scores. Lower is better. Ties go to the block nearer the front, then the block further left.

### 4.2 Operation 2: Check out (B)

`POST /events/{event_id}/holds/{hold_id}/checkout`

Headers: `X-Buyer-Id`, `Idempotency-Key` (becomes the order ID). Body: none, or `{}`.

API steps:
1. Query the event's seats and keep those with this `hold_id`. If none: `404 hold_not_found`. If any has `holder` different from the caller: `403 not_your_hold`.
2. `PutItem` the order with `status = pending`, condition `attribute_not_exists(order_id)`. If the condition fails, the order already exists: return it as below without sending anything to SQS.
3. Send the SQS message (section 5).

`202` response:
```json
{"order_id": "9b1c...", "status": "pending"}
```

Errors: `400 missing_buyer_id`, `400 missing_idempotency_key`, `404 hold_not_found`, `403 not_your_hold`.

Worker steps, for each message:
1. `GetItem` the order. If `status` is not `pending`, stop: duplicate delivery.
2. Simulate payment: wait 1 to 3 seconds, and fail with a temporary error with probability `PAYMENT_FAILURE_RATE` (default 0.1). A temporary error is raised, so SQS retries the message.
3. Compute one ticket code per seat (section 6).
4. One `TransactWriteItems` call:
   - each seat: `SET #st = :sold, order_id = :oid, ticket_status = :valid, ticket_code = :code`, condition `#st = :held AND hold_id = :hid AND hold_expires_at_ms > :now`.
   - the order: `SET #st = :confirmed, paid = :true, tickets = :tickets, updated_at_ms = :now`, condition `#st = :pending`.
5. If cancelled: read `CancellationReasons` (same order as the items, order item last).
   - Order item `ConditionalCheckFailed`: duplicate, stop.
   - Any seat item `ConditionalCheckFailed`: `UPDATE` the order to `status = failed, fail_reason = hold_expired`, condition `#st = :pending`. Stop.
   - Anything else (for example `TransactionConflict`, throttling): raise, so SQS retries.
6. In `naive` mode, step 4's seat updates have no condition.

Do not add an `events` counter to the checkout transaction. Every checkout would touch the same item, and concurrent transactions on one item fail with `TransactionConflict`.

`GET /orders/{order_id}`, `200` response:
```json
{
  "order_id": "9b1c...", "event_id": "concert-1", "hold_id": "h-3f9a0c21b7d4",
  "buyer_id": "matt", "seats": ["A-3", "A-4"], "status": "confirmed", "paid": true,
  "tickets": [{"seat_id": "A-3", "code": "concert-1|A-3|9b1c...|Xk29fQb7LmP2vN8sRt4wYc"}],
  "fail_reason": null
}
```
Errors: `404 order_not_found`.

### 4.3 Operation 3: Scan a ticket (A)

`POST /tickets/scan`

Body:
```json
{"code": "concert-1|A-3|9b1c...|Xk29fQb7LmP2vN8sRt4wYc", "gate": "north"}
```

Steps:
1. `tickets.verify_code(secret, code)`. If it returns `None`: respond `INVALID` without touching the database.
2. `UpdateItem` the seat: `SET ticket_status = :used, used_at_ms = :now, used_gate = :gate`, condition `ticket_status = :valid AND order_id = :oid`.
3. If the condition fails: `GetItem` the seat and respond `ALREADY_USED` with where and when it was first used.

All scan outcomes return `200`, because a scanner always gets an answer:
```json
{"result": "ADMIT", "seat_id": "A-3"}
{"result": "ALREADY_USED", "seat_id": "A-3", "first_gate": "north", "first_used_at_ms": 1764000012000}
{"result": "INVALID"}
```
Errors: `400 missing_code`.

### 4.4 Reads

`GET /events/{event_id}/seats` (A), `200`:
```json
{
  "event_id": "concert-1", "rows": 5, "seats_per_row": 10, "mode": "correct",
  "server_time_ms": 1764000000000,
  "seats": [{"seat_id": "A-1", "row": "A", "seat_num": 1, "status": "available", "holder": null}]
}
```
Seats with an expired hold are reported as `available`. Sorted by row, then seat number.

`GET /events/{event_id}/stats` (A), `200`:
```json
{
  "event_id": "concert-1", "capacity": 50,
  "available": 12, "held": 4, "sold": 34,
  "tickets_issued": 36, "double_booked_seats": ["B-4"], "conflicts": 87,
  "orders": {"pending": 1, "confirmed": 18, "failed": 2}
}
```
`tickets_issued` counts seats across all confirmed orders. `double_booked_seats` lists seats that appear in more than one confirmed order. Both come from scanning the orders table for the event, which is fine at this scale. In `correct` mode, `double_booked_seats` must always be empty.

## 5. SQS message

Queue: `{prefix}-checkout`. Dead-letter queue: `{prefix}-checkout-dlq`, after 3 failed receives. Visibility timeout 60 seconds. Worker batch size 1.

Body:
```json
{"order_id": "9b1c..."}
```

## 6. Ticket code

```
{event_id}|{seat_id}|{order_id}|{signature}
```

`signature` = the first 16 bytes of `HMAC-SHA256(TICKET_SECRET, "{event_id}|{seat_id}|{order_id}")`, encoded as URL-safe base64 without `=` padding (22 characters). Implemented in `server/common/tickets.py`: always use `make_code` and `verify_code`, never build codes by hand.

Codes are deterministic: the same event, seat, and order always produce the same code, so a retried checkout produces identical tickets.

## 7. Lambda environment variables

| Variable | Used by | Set by |
|---|---|---|
| `PREFIX` | both | setup script |
| `CHECKOUT_QUEUE_URL` | api | setup script |
| `TICKET_SECRET` | both | setup script, from `.secrets/{prefix}-ticket-secret` |
| `PAYMENT_FAILURE_RATE` | worker | setup script, default `0.1` |

## 8. Things that are deliberately left out

- Authentication: the buyer ID header is trusted.
- A waiting room.
- Per-buyer seat limits.
- Releasing expired holds actively. Optional stretch: a Step Functions timer per hold.
- Extending a hold when checkout starts. Optional stretch, see the write-up note in the README.
