# Conformance vectors

Language-neutral test fixtures that pin the **behavior** of the client
contract (C2/L3), the way the `.proto` files pin its **shape**. A schema can
say a `ModelSync` has a `model`, `id`, and `timestamp`; it cannot say *who a
write is delivered to* or *how two conflicting writes merge*. Those are
behaviors, and behaviors drift between independently-written kits
(ObscuraKit-Kotlin, ObscuraKit-swift, obscura-client-web) unless they are
pinned by a shared, executable spec.

Each kit loads these files in its own test suite and asserts its
implementation matches. A behavior change means editing the vector — which
forces every kit to prove it still conforms.

> **Format:** strict JSON (no comments/trailing commas). JSON is used
> deliberately: these are *test fixtures*, loaded by JUnit / XCTest / the web
> test runner with zero code generation. The contract data they describe (e.g.
> the model-config schema, the wire proto) lives in `.proto`; the fixtures
> *about* that contract are JSON.

## Files

| File | Behavior class | Consumes | Status |
|---|---|---|---|
| `routing.json` | Delivery targeting (audience → recipients, fail-loud) | Kotlin `RoutingConformanceTest` | active |
| `merge.json`   | CRDT merge (GSet union, LWW resolution)              | Kotlin `MergeConformanceTest` | active |
| `wire.json`    | Canonical `ModelSync` encoding                      | Kotlin `WireConformanceTest` | active |
| `schema.json`  | Model-config parsing (fields/sync/ttl/audience)     | Kotlin `SchemaConformanceTest` | active |

## `routing.json`

Pins which recipients an entry MUST reach given a model's schema config, or
the fail-loud error a kit MUST raise instead of misrouting.

```
{
  "version": 1,
  "kind": "routing",
  "topology": {
    "selfUserId": "uMe",
    "friends": [ { "userId", "username", "status" }, ... ]
  },
  "cases": [
    {
      "name":   "<human-readable, shown as the test name>",
      "schema": { "model"?, "sync", "ttl"?, "audience"? },   // a model-config
      "entry":  { "id", "data": { ... } },                   // the write
      "expect": { "recipients": ["uMe", ...] }               // OR:
      "expect": { "error": "DIRECT_ROUTING_UNRESOLVED" }
    }
  ]
}
```

**Design decisions:**

- **`expect.recipients` is a set of userIds, not devices.** The behavior that
  differs between kits is *audience → who*. Expanding a recipient to their
  devices is mechanical (a property of the friend/device graph) and is
  deliberately out of scope. `selfUserId` always appears in `recipients`
  because a write always reaches the author's own other devices.
- **`expect` is either `recipients` or `error`.** Fail-loud is a first-class
  outcome. When `error` is expected, the kit MUST raise it AND send nothing —
  a misrouted 1:1 payload is a confidentiality breach, so refusing to send is
  the safe failure.
- `schema.audience` mirrors the shared `schema.ts` / kit `Audience`:
  `{"kind":"friends"}` (or omitted), `{"kind":"self"}`,
  `{"kind":"recipient","field":"<usernameField>"}`,
  `{"kind":"conversation","field":"<convIdField>"}`.

### Consuming it (prototype: Kotlin)

A harness maps the topology so `deviceId == userId` (one device per user),
which makes the recorded target set equal the recipient-userId set directly,
then drives the real `SyncManager` per case:

```kotlin
val vectors = JSONObject(File("../proto/conformance/routing.json").readText())
// build ModelConfig via the SAME parse entry points as production
//   (SyncStrategy.fromWire / Audience.fromWire) so the parser is guarded too,
// call sm.broadcast(model, entry), then assert recorded == expect.recipients
//   (or that ObscuraError.code == expect.error and nothing was recorded).
```

See `ObscuraKit-Kotlin/lib/src/test/kotlin/scenarios/RoutingConformanceTest.kt`.

## `merge.json`

Pins CRDT merge resolution. Each case applies a list of `ops` (incoming syncs)
and asserts the resolved state; cases with multiple `applyOrders` assert
**convergence** — the same ops in different arrival orders MUST resolve
identically.

```
{
  "version": 1,
  "kind": "merge",
  "cases": [
    {
      "name": "<shown as the test name, suffixed with [order]>",
      "sync": "gset" | "lww",
      "ops": [ { "id", "ts", "authorDeviceId", "data": { ... } }, ... ],
      "applyOrders": ["forward", "reverse"],   // optional, default ["forward"]
      "expect": {
        "entries": [
          { "id", "authorDeviceId"?, "data"?, "deleted"? },   // assert only the present fields
          ...
        ]
      }
    }
  ]
}
```

**Design decisions:**

- **Convergence is the headline property.** `applyOrders` replays the same ops
  forward and reversed; both must match `expect`. This is what catches a
  non-deterministic tie-break (which passes single-order tests but corrupts
  state across replicas).
- **Ops are applied via the merge (incoming-sync) path**, since that is where
  reconciliation happens and where bugs hide.
- **`expect.entries` asserts only the fields present** per entry: `data` and/or
  `authorDeviceId` for the winner, or `deleted: true` for a tombstone.
- **The future-timestamp clamp is intentionally NOT here** — it is relative to
  wall-clock `now`, which a static fixture cannot pin. Each kit unit-tests it
  natively. See SPEC §2.4.

See `ObscuraKit-Kotlin/lib/src/test/kotlin/scenarios/MergeConformanceTest.kt`.

## `wire.json`

Pins the enum ↔ app-facing-form mappings from the v2 client.proto renumbering,
and `ModelSync` encode→decode round-trip (by value).

```
{
  "version": 1,
  "kind": "wire",
  "messageTypes": [ { "wire": "TYPE_MODEL_SYNC", "app": "MODEL_SYNC" }, ... ],
  "modelSyncOps": [ { "wire": "OP_CREATE", "app": "CREATE" }, ... ],
  "signalKinds":  [ { "wire": "SIGNAL_KIND_TYPING", "app": "typing" }, ... ],
  "roundTrip":    [ { "name", "modelSync": { model, id, op, timestamp, data } }, ... ]
}
```

**Design decisions:**

- **Mappings are the point.** Each kit consolidates them into one `WireCodec`
  (never duplicated) and asserts wire↔app in both directions.
- **Round-trip is by value, not bytes** — `data` is model-defined JSON; equality
  is over the parsed map, so key order is irrelevant.
- **No byte-canonicity.** Deliberately out of scope (SPEC §3.3): Signal already
  authenticates the payload, `data` is value-compared, and nothing hashes the
  bytes. So exact-byte assertions would over-constrain the wire for no consumer.

See `ObscuraKit-Kotlin/lib/src/test/kotlin/scenarios/WireConformanceTest.kt`.

## `schema.json`

Pins how one model's raw config (a value in the shared `schema.ts` map) is parsed
into the internal model definition, or the fail-loud `INVALID_SCHEMA` error for an
invalid definition. Directly guards the divergent-parsing bug class (a kit
ignoring `audience` so a private model broadcasts).

```
{
  "version": 1,
  "kind": "schema",
  "cases": [
    {
      "name": "<shown as the test name>",
      "config": { "fields": { "<name>": "<type>" }, "sync"?, "ttl"?, "audience"? },
      "expect": {
        "sync": "gset" | "lww",
        "ttl": "24h" | null,
        "audience": { "kind": "friends|self|recipient|conversation", "field": "..."|null },
        "fields": { "<name>": { "type": "string|number|boolean|timestamp", "optional": bool } }
      }
      // OR, for an invalid definition:
      "expect": { "error": "INVALID_SCHEMA" }
    }
  ]
}
```

**Design decisions:**

- **Consumed through the real parse entry point** (Kotlin `ModelConfig.fromWire`,
  which `defineModelsFromJson` also calls) — so the vector guards production
  parsing, not a reimplementation.
- **Field type is normalized** to `{ type, optional }`: the `?` suffix means
  optional/nullable; base type ∈ {string, number, boolean, timestamp}.
- **Defaults are pinned:** `sync` absent → `gset`; `audience` absent → `friends`;
  `ttl` absent → null.
- **`error: "INVALID_SCHEMA"`** asserts fail-loud parsing (unknown sync / field
  type / audience kind, or a `recipient`/`conversation` audience missing its
  `field`) — see SPEC §4.5.

See `ObscuraKit-Kotlin/lib/src/test/kotlin/scenarios/SchemaConformanceTest.kt`.

## Adding a case

Add an object to `cases[]`. No code change is needed in a conforming kit — the
consumer generates one named test per case. If a kit goes red, either the kit
has a bug or the vector encodes a behavior the kit has not yet implemented
(track the latter as an explicit workstream).

The canonical prose definitions behind these vectors live in [`../SPEC.md`](../SPEC.md).
