# Conformance vectors

Language-neutral test fixtures that pin the **behavior** of the client
contract (client-to-client), the way the `.proto` files pin its **shape**. A schema can
say a `ModelSync` has a `model`, `id`, and `timestamp`; it cannot say *who a
write is delivered to* or *how two conflicting writes merge*. Those are
behaviors, and behaviors drift between independently-written kits (the shipping
kits ObscuraKit-Kotlin and ObscuraKit-swift; obscura-client-web is a throwaway
PoC, not a normative target) unless they are pinned by a shared, executable spec.

Each kit loads these files in its own test suite and asserts its
implementation matches. A behavior change means editing the vector — which
forces every kit to prove it still conforms.

> **Format:** strict JSON (no comments/trailing commas). JSON is used
> deliberately: these are *test fixtures*, loaded by JUnit / XCTest / the web
> test runner with zero code generation. The contract data they describe (e.g.
> the model-config schema, the wire proto) lives in `.proto`; the fixtures
> *about* that contract are JSON.

## Files

| File | Behavior class | Consumed by | Status |
|---|---|---|---|
| `wire.json`    | Wire ↔ app mappings + `ModelSync` round-trip | Kotlin + Swift | **active — the only one left** |

> **`routing.json`, `merge.json` and `schema.json` were DELETED (2026-07-31, `RESET.md` §10 step
> 4), and it is worth understanding why — because the mechanism is sound and the mistake was
> upstream of it.**
>
> Where they went, so this is not a dead end for anyone following a citation:
>
> - `routing.json` — its five leak guards are transcribed verbatim into
>   `obscura-pix/src/domain/__tests__/audience.guards.test.ts`, run against
>   `src/domain/audience.ts`. `RESET.md` made that transcription a precondition of this deletion.
> - `merge.json` — vendored to `obscura-pix/src/domain/__fixtures__/merge.json` and executed by
>   `src/domain/__tests__/merge.vectors.test.ts` against `src/domain/merge.ts`. pix reads its OWN
>   copy, deliberately: the handover had to be complete before the original could go.
> - `schema.json` — no successor, and it never had a second implementation. Swift never adopted it,
>   which was a fair signal of its value; the kits do not parse application schemas at all now
>   (`SPEC.md` §0.4).
>
> These fixtures exist to keep two hand-written implementations of the same logic in agreement.
> That is a good solution to a problem the project should not have had: routing, merge, and
> schema parsing were implemented *twice*, in two kits, to serve five flat models in one app.
> Under [`SPEC.md` §0](../SPEC.md) that logic moved to the app, where it exists once — so there
> is nothing left for those vectors to police. `wire.json` survives precisely because encoding
> *must* be implemented twice: it is the one thing two kits genuinely have to agree on.
>
> The lesson to keep: a conformance suite is the right tool for logic you are *forced* to
> duplicate, and a warning sign for logic you merely *chose* to duplicate.

## `wire.json`

Pins the wire ↔ app-facing-form mappings for the `ClientMessage.payload` oneof
(the message kind) and the `ModelSync.Op` / `SignalKind` enums, plus a
`ModelSync` encode→decode round-trip (by value).

```
{
  "version": 1,
  "kind": "wire",
  "messageTypes": [ { "wire": "model_sync", "app": "MODEL_SYNC" }, ... ],
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

Consumers: Kotlin `WireConformanceTest.kt`, Swift `WireConformanceTests.swift`.

## Adding a case

Add an object to `cases[]`. No code change is needed in a conforming kit — the
consumer generates one named test per case. If a kit goes red, either the kit
has a bug or the vector encodes a behavior the kit has not yet implemented
(track the latter as an explicit workstream).

## Enforcement (CI)

Two gates, split by dependency direction — proto never builds a kit:

- **Upstream (this repo):** `conformance/validate.py` runs in CI
  (`.github/workflows/ci.yml`) and fails the PR if any vector is malformed —
  invalid JSON, wrong structure, a bad error code, an orphan file, or one not
  referenced by `SPEC.md`. It has zero knowledge of any kit. Run it locally with
  `python3 conformance/validate.py`.
- **Downstream (each kit):** whether a kit *satisfies* a vector is proven in
  that kit's own CI, against the proto commit it pins. A kit adopts a vector
  change by bumping its `proto` submodule in a PR; that PR's conformance suite
  is the gate. This is the same adopt-and-verify model the server proto and the
  PoC web client already follow.

The canonical prose definitions behind these vectors live in [`../SPEC.md`](../SPEC.md).
