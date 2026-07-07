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
| `merge.json`   | CRDT merge (GSet union, LWW resolution)              | — | planned |
| `wire.json`    | Canonical `ModelSync` encoding                      | — | planned |

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

## Adding a case

Add an object to `cases[]`. No code change is needed in a conforming kit — the
consumer generates one named test per case. If a kit goes red, either the kit
has a bug or the vector encodes a behavior the kit has not yet implemented
(track the latter as an explicit workstream).

The canonical prose definitions behind these vectors live in [`../SPEC.md`](../SPEC.md).
