# Obscura client contract — SPEC

**Spec version: 2**

The prose companion to the `.proto` files and the `conformance/` vectors. The
protos pin the **shape** of the client-to-client contract; this document and
the vectors pin its **behavior**. Where a rule is testable, it is backed by a
conformance vector and that vector is the authority — this document explains
*why*.

Scope: the client-to-client (kit ↔ kit) contract — the E2E payload the server never sees.
Layers:

- **Transport** — `obscura/v1/obscura.proto`. Server + kits. Out of scope here.
- **Content** — `obscura/client/v1/client.proto`. The message shapes.
- **Semantics** — this document. What the content *means* and how kits act on it.

The `client` package is a distinct **layer** (client-to-client content), not a
newer version of the `obscura.v1` transport — hence `obscura.client.v1`, where
`v1` is a genuine version of the client contract. (`obscura.v1` is a legacy
exception to this `obscura.<layer>.<version>` convention: it is really the
*transport* layer but predates the naming. See the repo [`README`](README.md#package-naming).)

The shipping kits — **ObscuraKit-Kotlin** and **ObscuraKit-swift** — MUST
conform. "MUST" / "MUST NOT" are normative. (`obscura-client-web` is a throwaway
proof-of-concept and is not a normative conformance target.)

---

## 0. The kit boundary

**This section governs every other section in this document.** Where another
section conflicts with it, this one wins and the other is wrong.

### 0.1 What a kit is

A kit is the **native platform layer for the Obscura app**. It exists because two
things cannot be done in TypeScript on a phone:

1. **libsignal** ships as `libsignal-java` and `libsignal-swift`. There is no
   supported shared core, so the Signal protocol must be implemented twice.
2. **The push path must run with the app closed.** On iOS a Notification Service
   Extension is a separate process with a tight memory budget and no React Native
   runtime. It must decrypt a message and produce notification text on its own.

A kit is **not** a general-purpose framework. It has exactly one consumer — the
app — and no API-stability obligation to anyone else. It MUST NOT be designed,
documented, or marketed as a reusable data layer.

### 0.2 The rule

> **If the kit reads it, it is a field in `client.proto`.
> If it is not in `client.proto`, the kit MUST NOT read it.**

The proto *is* the boundary. Everything below follows from this one line.

This rule is what makes the boundary reviewable: to check whether a kit has
overstepped, read its field accesses. Any read of application data that did not
come from a declared proto field is a violation, no matter how reasonable it
looks locally.

### 0.3 The kit MUST own

- Transport (REST + gateway WebSocket, envelope ack, offline send queue).
- The Signal protocol: sessions, identity, prekeys, encrypt/decrypt.
- Device provisioning, linking, revocation, takeover.
- The friend graph — needed both to address a peer's devices and to resolve a
  sender's display name locally (§0.5).
- The message store. The push path writes to it with the app closed, so it
  cannot live in the app's runtime.
- Attachment encryption, upload, download.
- The push-wake path: decrypt → persist → notify.

### 0.4 The kit MUST NOT

- **MUST NOT parse an application payload.** `AppData.payload` is opaque bytes.
- **MUST NOT know an application model name.** `"pix"`, `"directMessage"`, and
  friends are opaque keys the kit stores and echoes back. A model name MUST NOT
  appear as a literal in kit source.
- **MUST NOT resolve recipients.** The caller names them. A kit fans out to the
  devices of the userIds it is given, and makes no delivery decision of its own.
- **MUST NOT read an application field by name.** No `data["conversationId"]`,
  no `data["senderUsername"]`, no field sniffing of any kind.
- **MUST NOT implement query, relationship, or observation APIs.** Derived state
  is the app's job.
- **MUST NOT accept configuration that names application concepts.** A settings
  field like `conversationModel` is proof the boundary has already been crossed:
  the kit only needs to be told what an app's data *means* if it is doing
  something it should not be doing.
- **MUST NOT post an OS notification whose content came from anywhere other than
  declared proto fields plus copy the app registered.**

### 0.5 Sender identity

A notification and a UI label MUST name the sender using the **local friend
graph**, keyed by the sender identity on the authenticated envelope. A kit MUST
NOT take a display name from a message payload — a payload-supplied name is
attacker-controlled and lets a peer choose how they are labelled on screen.

The envelope tells you *who really sent this*. The payload tells you *what they
chose to say*. Never confuse the two.

The one exception — a `FriendRequest` from someone not yet in the graph — is
carved out in §0.10 rule 5, which also states the envelope fields this keying
uses and their trust status.

### 0.6 The app MUST own

Model semantics and validation; recipient resolution; all derived state
(queries, filters, sorting); notification copy; expiry (an `expiresAt` field the
app filters on).

### 0.7 Consequences

- Adding a **field** to existing content: app only. The kit never sees it.
- Adding a **new notifiable content type**: a deliberate `client.proto` change
  plus both kits. This is rare, and it should be deliberate — you are also
  designing new notification UX at the same time.
- If a kit cannot do its job using declared proto fields alone, **fix the proto**.
  Reaching into the payload is never the answer.

### 0.8 Why this section exists

It was written after an audit found the opposite of all of the above: a
schema-driven ORM, CRDT engine, query DSL, and audience-routing system
implemented **twice**, in two languages, to serve five flat models — with a
conformance suite built to keep the two copies in agreement. None of it was
required by the app. A generic engine had grown in the kits because nothing was
written down that said it must not, and each individual commit looked reasonable.

The lesson generalizes: an agent or engineer working inside one kit repository
**cannot** see that the engine is unnecessary, because the evidence lives in the
app. Given "improve this repo," they will harden what they find. This document is
the brief you give them instead.

### 0.9 Receive: persist-then-ack

**An ACK is a DELETE. Ack only what you have durably persisted.**

On the gateway an ack is destructive: the server deletes the acked envelope from
the `messages` table (no tombstone, no redelivery). A message is redelivered only
because it is *still on the server* — a fresh `MessagePump` on the next connection
re-reads every remaining row. Therefore the ack is the client's commitment that it
no longer needs the server's copy, and it MUST NOT be sent until the message is in
the kit's own durable store.

Normative rules for the receive loop, identical in both kits:

1. A kit MUST NOT ack an envelope whose decrypt threw.
2. A kit MUST NOT ack an envelope it skipped (e.g. a rate-limited sender). The
   message stays on the server to be retried later.
3. A kit MUST NOT ack until the durable persistence step for that message has
   completed successfully. If persistence throws, the kit MUST NOT ack.
4. The strict order per envelope is **decrypt → persist → (notify) → ack**. Any
   in-process notification (a wake-up channel/flow the app observes) is emitted
   *after* persistence and carries no data that persistence did not already store,
   so it MAY be dropped under backpressure without loss — but only because the
   durable store, not the notification, is the delivery path, and persistence
   happened-before the ack. A notification that is the *sole* delivery path for a
   message that then gets acked MUST NOT be silently droppable.

### 0.10 Envelope identity: who sent this

The transport `Envelope` carries **both** identifiers, each stamped by the server
from the sender's device-scoped token and therefore unforgeable by the sender:

| Field | Meaning | Signal's equivalent |
|---|---|---|
| `sender_id` | the sending **user** (16-byte UUID) | `Envelope.source_service_id` |
| `sender_device_id` | the sending **device** (16-byte UUID) | `Envelope.source_device` |

Both are **hints — for routing, session selection and labelling. Neither is a
trust root.** The trust root is the Signal session: a valid MAC proves possession
of that session's chain key, which only the sending device holds.

1. A kit MUST select the inbound Signal session by `sender_device_id`. Signal
   sessions are pairwise device-to-device and a `SignalMessage` carries no sender
   identity, so nothing else on the wire can choose the session. An envelope whose
   `sender_device_id` is absent or not 16 bytes is an **error**: a kit MUST NOT
   guess a device, iterate candidate sessions, or fall back to a default device id.
2. A kit MUST key its local Signal address (`ProtocolAddress`) on the **device
   UUID**. `registrationId` MUST NOT be used as an addressing identifier: it is
   carried on exactly one wire surface (`PreKeyBundleResponse`), while the device
   UUID is carried on all of them. The address is a purely local store key and is
   never transmitted.
3. A kit MUST select a peer's prekey bundle by device UUID, with **no fallback**
   to an arbitrary bundle. Encrypting once under one device's keys and fanning that
   ciphertext to every device of the user is the defect this rule exists to prevent
   (`PLAN.md` F1): exactly one device can read it and the rest fail silently.
4. `authorDeviceId` MUST be derived from the **address of the session that
   decrypted** the message — never from a wire field. A malicious server that lies
   about `sender_device_id` can cause a decryption failure, but can never forge an
   attribution.
5. The display name MUST come from the local friend graph keyed on the
   authenticated sender (§0.5). The **friend-request bootstrap** is the single
   exception: a `FriendRequest` arrives from a user who is not yet in the graph, so
   its payload `username` is the only name available. It is attacker-chosen and MUST
   be treated as a request-time label, never as an authenticated identity, and MUST
   NOT be persisted as the friend's name once the friendship is accepted.
6. A kit that already knows which user owns `sender_device_id` (from its friend
   graph or a prekey fetch) SHOULD cross-check `sender_id` against it and log a
   mismatch as a security event. Neither kit does this today; the residual exposure
   is a **mis-labelled** message, never a forged one, because the content is
   authenticated by a session the server does not hold.

---

## 1. Routing (delivery targeting)

> **REWRITTEN 2026-07-31 — this is now an APP obligation, not a kit one.**
> Recipient resolution moved to the app (§0.4; kits Kotlin #56, Swift #24), so a
> kit no longer resolves an audience and cannot misroute. `conformance/routing.json`
> was deleted with it; its five leak guards live in
> `obscura-pix/src/domain/__tests__/audience.guards.test.ts`.
>
> The section survives because **the rules did not stop being true when they
> changed owner.** §1.2 and §1.3 are implemented today by
> `obscura-pix/src/domain/audience.ts` *and* by both kits' ephemeral-signal path,
> which refuses a `contextId` that does not name exactly two participants. What
> was deleted is §1.1's schema-driven audience table — the part that told a kit to
> read application config, which §0.4 now forbids outright.
>
> §1.2, §1.3 and §1.4 **keep their numbers**; both kits cite them. The gap where
> §1.1 was is deliberate — renumbering would silently redirect a live citation.

*Vectors: none — `routing.json` was deleted 2026-07-31.*

**The caller names the recipients** (§0.4). This section specifies what the
caller MUST get right, and it binds whoever resolves the audience — today the
app, for entries, and the kit itself only for the ephemeral-signal `contextId`,
which is the one audience a kit still derives.

### 1.1 Audience modes — DELETED

The table here mapped a model's declared `audience` config to a recipient set,
and instructed a kit to resolve it. §0.4 forbids exactly that: a kit does not
read application configuration. The app decides who a write is for and passes a
recipient list.

One invariant from it survives and has moved to §5 (`send`), because it is a
property of the send path rather than of an audience: **the author's own other
devices are always included**, whatever the caller asks for, and the *sending*
device is always excluded. A caller cannot opt out of self-sync and cannot
accidentally encrypt to itself.

### 1.2 Fail-loud rule (confidentiality)

A misrouted 1:1 payload is a confidentiality breach. Therefore, whoever resolves
a 1:1 audience MUST raise `DIRECT_ROUTING_UNRESOLVED` and send **nothing** when
it cannot be resolved. It MUST NOT fall back to a broadcast. Specifically:

- **by recipient**: the naming field is **missing or blank** → raise. (A field
  that is present and non-blank but names a non-friend is *not* an error: it
  resolves to zero external recipients, so the write reaches own devices only —
  fail-safe, never a broadcast.)
- **by conversation**: the value does not resolve to **exactly two** participants
  (missing, blank, or not a canonical two-party value per §1.3) → raise.

Two implementations, and both are load-bearing:

- **The app, for entries.** `obscura-pix/src/domain/audience.ts`, pinned by the
  five leak guards transcribed from the deleted `routing.json`.
- **The kit, for ephemeral signals.** A `MODEL_SIGNAL`'s audience comes from its
  `contextId`, which is the one audience a kit still derives — so the kit owes
  this rule too, and both kits implement it as `send nothing` rather than an
  error, because dropping a typing indicator costs nothing while guessing its
  audience leaks the conversation.

> A three-party test is required to see a violation. Two-party tests cannot
> distinguish "sent to the conversation" from "sent to everyone", which is why a
> live broadcast leak survived the original vectors and the whole suite.

### 1.3 Canonical `conversationId`

A conversation audience value — and a `MODEL_SIGNAL.contextId` — is the
canonical two-party id: the two
participants' userIds sorted lexicographically and joined with a single
underscore, `"userIdA_userIdB"`. Splitting on `_` MUST yield exactly two
non-empty parts. This form makes a 1:1 conversation address the same regardless
of which participant composed the write, so a reply/receipt resolves in either
direction.

### 1.4 Error codes

Fail-loud outcomes are identified by a stable `code` string (not message text),
so cross-platform error handling can match on it.

| Code | Raised when | Raised by |
|---|---|---|
| `DIRECT_ROUTING_UNRESOLVED` | A 1:1 audience cannot be resolved (see §1.2). | The app (`obscura-pix/src/domain/audience.ts`). Both kits still declare the code on their error enum, where it is now **dead** — a follow-up, kept in step across the two kits because the bridge exposes it to JS. |

---

## 2. Merge (conflict resolution)

> **REWRITTEN 2026-07-31 — an APP obligation now, like §1.** The CRDT *engine* is
> deleted from both kits (Kotlin #56, Swift #24). The *rules* are not: they are
> implemented once, in `obscura-pix/src/domain/merge.ts`, as `APPEND` and
> `REPLACE` declared per message on the wire rather than read from a schema.
>
> §2.1 and §2.2 keep their numbers and their content, renamed to the rule names
> the app and the wire actually use. What went with the engine is the generality
> around them — the query layer, observation, the tombstone ordering of §2.3 —
> which served concurrent multi-writer editing the app does not do.
>
> **§2.3 is DELETED.** `deleteEntry` had no caller in the app, both kits now hard
> delete, and a tombstone ordering nothing produced was dead on arrival.
>
> §2.4 (the future-timestamp clamp) **survives unchanged** and keeps its number:
> both kits and pix cite "SPEC §2.4" by name.
>
> `conformance/merge.json` was deleted 2026-07-31. It lives on as
> `obscura-pix/src/domain/__fixtures__/merge.json`, executed by
> `src/domain/__tests__/merge.vectors.test.ts` — pix vendored its own copy first,
> which `RESET.md` made a precondition of the deletion.

*Vectors: none — `merge.json` was deleted 2026-07-31.*

A message's merge rule decides how concurrent writes to the same entry `id`
reconcile. Merge MUST be **convergent**: applying the same set of writes in any
arrival order yields the identical resolved state. This is why the vectors
replayed each case in multiple `applyOrders`, and why the tie-break in §2.2 is
mandatory rather than cosmetic.

### 2.1 `APPEND` (first write wins) — formerly GSet

Union keyed by entry `id`. The first write seen for an `id` is kept; later
writes with the same `id` are ignored (idempotent). `APPEND` entries are
immutable by construction (ids are unique: `model_timestamp_random`), so a
repeated `id` carries identical content and order cannot matter.

### 2.2 `REPLACE` (last writer wins) — formerly LWW

Each `id` resolves to the winner under a **total order on
`(sentAt, authorDeviceId)`** (`timestamp` on the wire):

1. Strictly-greater `timestamp` wins.
2. On an **equal** `timestamp`, the lexicographically-**higher** `authorDeviceId`
   wins.
3. Equal `timestamp` **and** equal `authorDeviceId` is the same logical write —
   idempotent, the existing entry is kept.

The `authorDeviceId` tie-break (2) is mandatory: without it, an equal-timestamp
conflict resolves to "whichever write arrived first", so two devices that
receive the two writes in different orders converge to **different** states and
never reconcile. That silently corrupts state and is invisible in single-device
testing — hence it is pinned by a multi-order vector, now
`obscura-pix/src/domain/__tests__/merge.vectors.test.ts`.

> **`authorDeviceId` MUST come from the decrypting session, never from a payload
> field** (§0.10 rule 4). A peer-asserted value turns the tie-break into a way to
> win every equal-timestamp conflict on demand. This was a live defect: the
> deleted ORM took it from the payload, and it silently corrupted merge metadata
> for as long as it ran alongside the replacement.


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

The client content is a `ClientMessage` (`obscura/client/v1/client.proto`). This
section pins two things about it: the **wire ↔ app-facing-form mappings** (the message
kind and the two remaining enums) and **round-trip preservation** of a
`ModelSync`.

### 3.1 Message kind and enum mappings

The message kind is the `ClientMessage.payload` **oneof**: exactly one arm is
set, and *which* arm is set is the message type — there is no separate `Type`
enum to keep in sync (a kind/content mismatch is unrepresentable). The app-facing
type string is the oneof field name upper-snake-cased (`text` → `"TEXT"`,
`model_sync` → `"MODEL_SYNC"`).

The app never sees the `OP_`/`SIGNAL_KIND_` wire prefixes on the two enums that
remain. A kit MUST map:

| Wire form | App-facing form | Rule |
|---|---|---|
| `ClientMessage.payload` arm e.g. `model_sync` | `"MODEL_SYNC"` | oneof field name, upper-snake |
| `ModelSync.Op` e.g. `OP_CREATE` | `"CREATE"` | strip the `OP_` prefix |
| `SignalKind` e.g. `SIGNAL_KIND_TYPING` | `"typing"` | mapped name (see table) |

An unset payload maps to `""` (ignored). `*_UNSPECIFIED` (and any unrecognized
value) decodes to the safe default: `Op` → `CREATE`; `SignalKind` → ignored.
These mappings MUST live in one place per kit (a `WireCodec`), never duplicated,
so they cannot drift within a kit.

### 3.2 Round-trip

`encode(ModelSync) → decode` MUST preserve `model`, `id`, `op`, `timestamp`, and
the `data` **value**. `data` is model-defined JSON carried in a proto `bytes`
field; equality is by parsed value, so key order is irrelevant.

### 3.3 What is deliberately NOT specified: byte-canonicity

There is intentionally **no canonical byte encoding**. Neither the inner `data`
JSON nor proto3 serialization is guaranteed byte-identical across
languages/libraries, and nothing needs it to be:

- **Signal already authenticates and integrity-protects the whole
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

- **v3** (2026-07-31, Phase 3 "the reset") — the deleted-engine sections are settled rather
  than left SUPERSEDED. **§1.1 and §§2.3, 4 are removed**: schema-driven audience resolution,
  tombstone ordering and model-config parsing all described things a kit does not do, and none had
  a successor. **§1.2–1.4 and §2.1–2.2 are rewritten, keeping their numbers**, because their rules
  did not stop being true when they changed owner — they are implemented in
  `obscura-pix/src/domain/{audience,merge}.ts`, plus the kits' ephemeral-signal path for §1.2. §2.1
  and §2.2 are renamed to the `APPEND`/`REPLACE` rule names the wire actually carries. §2.4 is
  unchanged. Numbers are deliberately NOT compacted — §1.2, §2.2 and §2.4 are cited by name in both
  kits and in pix, and renumbering would silently redirect a live citation. `conformance/routing.json`,
  `merge.json` and `schema.json` deleted; `wire.json` is the only vector left, because encoding is
  the one thing two kits are forced to implement twice.
- **v2** — §0 **The kit boundary** (what a kit is, the proto-is-the-boundary rule,
  MUST/MUST NOT lists, sender identity from the friend graph) — governs every other
  section. §0.9 **Receive: persist-then-ack** (an ACK is a DELETE; decrypt → persist
  → notify → ack), implemented in Phase 1. §0.10 **Envelope identity** (`sender_id`
  + `sender_device_id` as hints, device-UUID session addressing and bundle
  selection, `authorDeviceId` from the decrypting session), implemented in Phase 2
  alongside `Envelope.sender_device_id`. §§1, 2.1–2.3 and 4 marked SUPERSEDED,
  pending removal in Phase 3. Not vector-backed: §0 is a boundary rule reviewable by
  reading a kit's field accesses; §0.9 and §0.10 are receive-loop behavior, verified
  by per-kit integration tests (`AckSemanticsTests`, `AuthorDeviceIdTests`,
  `TwoDeviceSendTests`).
- **v1** — Initial spec. §1 Routing (audience modes, fail-loud rule, canonical
  `conversationId`, `DIRECT_ROUTING_UNRESOLVED`), backed by `routing.json`.
  §2 Merge (GSet union, LWW total order with `authorDeviceId` tie-break,
  tombstones, future-timestamp clamp), backed by `merge.json`. §3 Wire (payload
  oneof as message-kind discriminator + `Op`/`SignalKind` mappings, round-trip;
  byte-canonicity deliberately out of scope; `ModelSync.signature` removed),
  backed by `wire.json`. §4 Model config (field
  types + optionality, sync/ttl/audience defaults, fail-loud `INVALID_SCHEMA`),
  backed by `schema.json`.
