# Container mass API

The OS API can enroll an already-opened container with its empty-container tare,
current contents mass, and full contents reference. All mass values are in **grams**.
These are item inventory fields; setting `tare_weight` does not tare the physical scale.

## Authentication and endpoints

Use `Authorization: Bearer <access_token>` on requests, including SSE connections.
Obtain a user token from `POST /api/auth/login/json` with JSON `email` and `pin`,
or use the configured [relay service token](../../../../README.md#relay-service-authentication).
Item writes require `storage:write`, reads require `storage:read`, or the relay
credential. Robot commands require authentication and enforce their normal item
access and storage preconditions. See the OpenAPI document served at `/openapi.json`
on the device for complete schemas.

| Endpoint | Mass input or output |
| --- | --- |
| `POST /api/storage/items` | Create/upsert an item with mass fields; returns the item. |
| `PUT /api/storage/items/{barcode}` | Update selected fields; returns the item. |
| `GET /api/storage/items/{barcode}` | Read current item mass fields. |
| `GET /api/storage/items` | Read the same fields in `items[]`. |
| `POST /api/robot/commands` | Enroll a new item through the `store_item` command's `item` object. Returns `command_id` and acceptance status, not a measurement. |
| `GET /api/robot/events` | Live SSE command results, with optional top-level `weight` in each decoded JSON event. |
| `GET /api/robot/commands/{command_id}` | Persisted command lifecycle, with optional `result.weight` after completion. Available for mock and production commands in this checkout. |

## Enroll and update

For example, send this JSON to `POST /api/storage/items`:

```json
{
  "barcode": "880000000001",
  "name": "Opened bottle",
  "dimensions": {"width": 10, "height": 10, "length": 10},
  "tare_weight": 100,
  "net_weight": 250.5,
  "full_net_weight": 500
}
```

The response includes `weight: 350.5`, `tare_weight: 100`, `net_weight: 250.5`,
and `full_net_weight: 500`. Creating the inventory record does not move the item.
Submit `{"command":"store_item","item":{"barcode":"880000000001"}}` to
`POST /api/robot/commands` to store it.

Alternatively, for a **new barcode**, enroll and queue storage in one request:

```json
{
  "command": "store_item",
  "request_id": "opened-bottle-first-store",
  "item": {
    "barcode": "880000000002",
    "name": "Opened bottle",
    "dimensions": {"width": 10, "height": 10, "length": 10},
    "tare_weight": 100,
    "net_weight": 250.5,
    "full_net_weight": 500
  }
}
```

Provide a name and complete positive dimensions for new command-enrolled items.
For an existing barcode, edit mass through the item endpoints before submitting its
store command; the command uses the existing inventory mass. An accepted command
can still fail during execution. Its enrollment survives failure or restart, so
edit that record and submit a new command when retrying; replaying the same
`request_id` returns the original command.

| Field | Meaning and update rules |
| --- | --- |
| `weight` | Authoritative current gross mass, including container. Can be supplied instead of `net_weight`. The legacy item input validator requires a positive gross value. |
| `tare_weight` | Known empty-container mass; must be finite and greater than zero, or `null` to clear it. |
| `net_weight` | Current contents mass. A finite value greater than or equal to zero requires known tare, supplied in the request or already stored. Writes set gross to tare plus net; `null` clears current gross and therefore net. |
| `full_net_weight` | Full contents reference, excluding the container; must be finite and greater than zero, or `null` to clear it. It does not change the current contents mass. |

Omitting any of the three new fields (`tare_weight`, `net_weight`,
`full_net_weight`) preserves its stored state on updates. Omitted `weight` also
preserves gross. Never supply both `weight` and `net_weight` in one request,
even if either is `null`; this returns `422`. To clear current mass on PUT,
use `{"net_weight":null}`; legacy `{"weight":null}` on PUT does not clear it.
For example, `{"tare_weight":null,"full_net_weight":null}` clears both
references while retaining gross, and `{"net_weight":0}` records empty contents
when tare is known. Changing tare alone retains gross and recalculates net.

Responses derive `net_weight` as `weight - tare_weight` only when both are known
and gross is at least tare. Otherwise net is `null`, including when a reading
is below tare. Existing items receive no inferred tare or full reference.

## Cycle readings and persisted inventory

Subscribe to `/api/robot/events` before submitting commands. An illustrative
successful result appears in an SSE `data:` frame as:

```json
{
  "message": "success",
  "command": "store_item",
  "command_id": "00000000-0000-4000-8000-000000000001",
  "barcode": "880000000001",
  "location": {"side": "left_module", "shelf_idx": 0, "cell_idx": 0},
  "weight": 350
}
```

The JSON event's `weight` is the cycle's gross reading. Polling the accepted
command ID exposes that same result under `result` (for example,
`status: "succeeded"` and `result.weight: 350`). `result` can be `null` while
queued or running. SSE is a live stream, not a replayable weight history.
Read the item endpoint for persisted gross, tare, derived net, and full reference;
production event processing is asynchronous, so inventory may briefly lag the event.

Initial explicit net enrollment is retained through the **first successful
storage**, even if that store event reports a different reading. In the example,
the event can report `350` while the item still returns gross `350.5` and net
`250.5`. A failed store does not consume this protection. Subsequent successful
direct store/retrieve cycles with finite, nonnegative readings replace current
gross and derive net using the retained tare. Missing, null, negative, or non-finite
cycle readings retain the last valid item gross; absent measurements never mean zero.
Tare and the full reference are not rewritten by cycle readings.

**Production limitation:** the current physical store path attempts to obtain a
weight; a disabled/unavailable scale or unsuccessful stable read can leave it
`null`. The physical retrieve path returns success without taking or supplying
a retrieval weight. Its result weight is therefore absent/null, and retrieval
retains item mass. Server support for a supplied retrieval reading does not imply
that production hardware measures each retrieval. Polling cannot supply a reading
that the producer did not emit.

**Mock behavior:** run `./scripts/start --mode mock` from the repository root.
The real authenticated API accepts the examples above. For items with known
tare, mock store and retrieve results simulate `350` grams; the first store still
preserves explicit net enrollment. The next mock retrieval therefore produces
gross `350` and net `250` for a `100` gram tare. These are deterministic simulated
readings. `/api/robot/status` scale telemetry and `/api/robot/debug/scale` are live
scale views, not the command-correlated cycle result described above.

## Multi-item bins

Aggregate bin mass is exposed separately as `bins[].gross_weight` on
`GET /api/storage/bins`, and as `bin.gross_weight` on an item's response when it
belongs to a bin. This includes the bin and all its contents. It is never copied
into an individual item's `weight` or used to derive that item's net mass.
Membership changes invalidate the aggregate; a valid returned-bin measurement
establishes a new value. It is exposed only while the bin is `at_home`, and a
return without a valid measurement leaves it `null`. Bin storage completion
preserves the enrolled item mass and completes its initial-storage protection.
