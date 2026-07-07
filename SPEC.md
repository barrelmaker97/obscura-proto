# Obscura client contract — SPEC

**Spec version: 1**

The prose companion to the `.proto` files and the `conformance/` vectors. The
protos pin the **shape** of the client-to-client contract; this document and
the vectors pin its **behavior**. Where a rule is testable, it is backed by a
conformance vector and that vector is the authority — this document explains
*why*.

Scope: the C2 (kit ↔ kit) contract — the E2E payload the server never sees.
Layers:

- **L1 transport** — `obscura/v1/obscura.proto`. Server + kits. Out of scope here.
- **L2 content** — `obscura/v2/client.proto`. The message shapes.
- **L3 semantics** — this document. What the content *means* and how kits act on it.

All three kits (ObscuraKit-Kotlin, ObscuraKit-swift, obscura-client-web) MUST
conform. "MUST" / "MUST NOT" are normative.

---

## 1. Routing (delivery targeting)

*Vectors: [`conformance/routing.json`](conformance/routing.json).*

Every model declares an **audience** in its schema config that determines which
recipients a write is delivered to. A kit MUST resolve the audience using only
the declared configuration — it MUST NOT hard-code application field names.

### 1.1 Audience modes

| `audience` | Meaning | Recipients |
|---|---|---|
| omitted / `{"kind":"friends"}` | Broadcast | The author's own devices + every accepted friend's devices. |
| `{"kind":"self"}` | Private | The author's own devices only. MUST never leave the account. |
| `{"kind":"recipient","field":"<f>"}` | 1:1 by username | Own devices + the devices of the single username in `data[f]`. |
| `{"kind":"conversation","field":"<f>"}` | 1:1 by conversation | Own devices + the devices of both participants encoded in `data[f]`. |

The author's own devices are **always** included, regardless of audience
(self-sync). `selfUserId` therefore always appears in a vector's
`expect.recipients`.

### 1.2 Fail-loud rule (confidentiality)

A misrouted 1:1 payload is a confidentiality breach. Therefore, for the
`recipient` and `conversation` audiences, if the recipient cannot be resolved
the kit MUST raise `DIRECT_ROUTING_UNRESOLVED` and send **nothing**. It MUST
NOT fall back to a broadcast. Specifically:

- `recipient`: the named field is **missing or blank** → raise. (A field that
  is present and non-blank but names a non-friend is *not* an error: it resolves
  to zero external recipients, so the write reaches own devices only — fail-safe,
  never a broadcast.)
- `conversation`: the named field does not resolve to **exactly two**
  participants (missing, blank, or not a canonical two-party value) → raise.

### 1.3 Canonical `conversationId`

A `conversation` audience value is the canonical two-party id: the two
participants' userIds sorted lexicographically and joined with a single
underscore, `"userIdA_userIdB"`. Splitting on `_` MUST yield exactly two
non-empty parts. This form makes a 1:1 conversation address the same regardless
of which participant composed the write, so a reply/receipt resolves in either
direction.

### 1.4 Error codes

Fail-loud outcomes are identified by a stable `code` string (not message text),
so vectors and cross-platform error handling can match on it.

| Code | Raised when |
|---|---|
| `DIRECT_ROUTING_UNRESOLVED` | A `recipient`/`conversation` audience cannot be resolved (see §1.2). |

---

## 2. Merge (CRDT resolution)

*Vectors: [`conformance/merge.json`](conformance/merge.json).*

A model's `sync` strategy decides how concurrent writes to the same entry `id`
reconcile. Merge MUST be **convergent**: applying the same set of writes in any
arrival order yields the identical resolved state. `merge.json` enforces this by
replaying each case in multiple `applyOrders`.

### 2.1 GSet (grow-only set) — `sync: "gset"`

Union keyed by entry `id`. The first write seen for an `id` is kept; later
writes with the same `id` are ignored (idempotent). GSet entries are immutable
by construction (ids are unique: `model_timestamp_random`), so a repeated `id`
carries identical content and order cannot matter.

### 2.2 LWW (last-writer-wins map) — `sync: "lww"`

Each `id` resolves to the winner under a **total order on
`(timestamp, authorDeviceId)`**:

1. Strictly-greater `timestamp` wins.
2. On an **equal** `timestamp`, the lexicographically-**higher** `authorDeviceId`
   wins.
3. Equal `timestamp` **and** equal `authorDeviceId` is the same logical write —
   idempotent, the existing entry is kept.

The `authorDeviceId` tie-break (2) is mandatory: without it, an equal-timestamp
conflict resolves to "whichever write arrived first", so two devices that
receive the two writes in different orders converge to **different** states and
never reconcile. That silently corrupts state and is invisible in single-device
testing — hence it is pinned by a multi-order vector.

### 2.3 Tombstones (delete)

A delete is a normal LWW write whose `data` is `{ "_deleted": true }`, stamped
at the deleting device's current time. Because it is an ordinary write, it
participates in the §2.2 total order: a newer write (edit or delete) wins, and a
stale write can never resurrect a newer tombstone. `getAll`/`size` exclude
tombstones; `get` returns them.

### 2.4 Future-timestamp clamp

An incoming `timestamp` more than **60s** beyond local wall-clock is clamped to
`now + 60s` before it participates in §2.2, so a spoofed far-future timestamp
cannot win every future conflict forever. The clamp applies on **both** the
local-write and the incoming-sync (merge) paths — a timestamp arriving over sync
is no more trustworthy than a local one.

*Not vector-tested:* the clamp is relative to wall-clock `now`, which a static
fixture cannot express deterministically, so it is verified by per-kit unit
tests instead (e.g. Kotlin `LWWMapTest`).

## 3. Wire (encoding)

*Vectors: [`conformance/wire.json`](conformance/wire.json).*

The client content is a `ClientMessage` (`obscura/v2/client.proto`). L3 pins two
things about it: the **enum ↔ app-facing-form mappings** (which the v2
renumbering made non-trivial) and **round-trip preservation** of a `ModelSync`.

### 3.1 Enum mappings

The app never sees the `TYPE_`/`OP_`/`SIGNAL_KIND_` wire prefixes. A kit MUST map:

| Wire enum | App-facing form | Rule |
|---|---|---|
| `ClientMessage.Type` e.g. `TYPE_MODEL_SYNC` | `"MODEL_SYNC"` | strip the `TYPE_` prefix |
| `ModelSync.Op` e.g. `OP_CREATE` | `"CREATE"` | strip the `OP_` prefix |
| `SignalKind` e.g. `SIGNAL_KIND_TYPING` | `"typing"` | mapped name (see table) |

`*_UNSPECIFIED` (and any unrecognized value) decodes to the safe default:
`Op` → `CREATE`; `SignalKind` → ignored. These mappings MUST live in one place
per kit (a `WireCodec`), never duplicated, so they cannot drift within a kit.

### 3.2 Round-trip

`encode(ModelSync) → decode` MUST preserve `model`, `id`, `op`, `timestamp`, and
the `data` **value**. `data` is model-defined JSON carried in a proto `bytes`
field; equality is by parsed value, so key order is irrelevant.

### 3.3 What is deliberately NOT specified: byte-canonicity

There is intentionally **no canonical byte encoding**. Neither the inner `data`
JSON nor proto3 serialization is guaranteed byte-identical across
languages/libraries, and nothing needs it to be:

- **Signal (L2) already authenticates and integrity-protects the whole
  `ClientMessage`.** Sender authenticity and tamper-evidence are provided by the
  encryption layer, over the payload regardless of byte order.
- `data` is **parsed into a map and compared by value**, never by bytes.
- Dedup is by entry `id`; the transport idempotency key is computed by the
  sender over its own outgoing bytes and never needs cross-device reproducibility.

Byte-canonicity would only matter if an app-level signature verification or
content-addressing were introduced. It is not, so pinning exact bytes would
constrain the wire for a property nothing consumes. **If such a feature is ever
added, a canonical `data` encoding (e.g. sorted-key JSON) must be defined
first.** (Historically `ModelSync` carried a `signature` field — a keyless
`SHA-256` that was never verified; it has been removed as redundant with Signal.)

---

## Changelog

- **v1** — Initial spec. §1 Routing (audience modes, fail-loud rule, canonical
  `conversationId`, `DIRECT_ROUTING_UNRESOLVED`), backed by `routing.json`.
  §2 Merge (GSet union, LWW total order with `authorDeviceId` tie-break,
  tombstones, future-timestamp clamp), backed by `merge.json`. §3 Wire (enum ↔
  app-form mappings, round-trip; byte-canonicity deliberately out of scope;
  `ModelSync.signature` removed), backed by `wire.json`.
