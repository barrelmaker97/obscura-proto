# The thin kit API — proposal

**Status: proposal, for review. Not normative until merged into [`SPEC.md`](SPEC.md).**

[`PLAN.md`](PLAN.md) Phase 3 calls this "the one genuinely hard-to-reverse decision in the plan".
This document is that decision, written down before any code is deleted, so it can be argued with
cheaply.

Governed by [`SPEC.md` §0](SPEC.md). Where this document conflicts with §0, §0 wins and this document
is wrong.

> **Revision 2 (2026-07-25)** — rewritten after an adversarial review. What changed, and why it
> matters more than the diff suggests:
>
> - The record could only hold a `ModelSync`. `client.proto` has **18** payload arms, and one of
>   them (`ModelSignal`, typing) is explicitly never persisted — so **"the inbox closes the
>   ack-before-persist hole by construction" was false as written**. Now there is a `kind`
>   discriminator and a per-arm policy (§4).
> - REPLACE said "equal `sentAt` is idempotent", which **negates the tie-break `SPEC.md` §2.2 calls
>   mandatory** and would let two devices diverge permanently. Fixed in §8.2, using the
>   *authenticated* device id rather than the wire field.
> - The attachment signatures could not work: the decryption key lives inside the payload, which the
>   kit may not read (§5).
> - §3.4 claimed unacked messages stay on the server. They do not: **≤1000 per device, ≤30 days,
>   oldest evicted first.**
> - The migration order was not executable — pix has one TypeScript surface for two platforms (§10).
> - Notification naming is now an explicit, persisted, app-chosen **policy** rather than an
>   accidental privacy change (§7).

---

## 1. The shape, in one line

> The kit is a **durable, authenticated inbox and outbox for opaque payloads**, plus the identity,
> device and transport machinery needed to fill them. It stores bytes it cannot read, addressed to
> and from identities it can prove.

Everything below follows from that, plus one rule that decides the whole design: **an ACK is a
DELETE** (§0.9).

---

## 2. Why an inbox, and not an event stream

This is the load-bearing decision. It is not a style preference.

Today the receive path is **decrypt → persist → ack**, all inside the kit, because the kit owns the
store. The reset takes application data away from the kit. If the thin kit instead *hands the payload
to the app* (an event, a callback, a bridge emit) and then acks, the ordering becomes:

```
decrypt → emit to app → ACK (server DELETEs) → ...app writes to its store, maybe, later, if it is running
```

That is precisely the Phase 1 data-loss bug, reintroduced across a process boundary, in both kits at
once, on a path where the app may not be running. React Native's bridge is asynchronous and lossy
under backpressure; the push path has no JS runtime at all. **The kit must persist before it acks,
therefore it must have somewhere durable to put bytes it does not understand.**

**What this does and does not close.** It closes the ack-before-persist hole *for everything that
enters the inbox* — which after §4 includes the app's entire data path. It does **not** close it for
message kinds the kit handles internally, which have their own durable stores and their own
obligation, nor for kinds that are deliberately droppable. Those are enumerated in §4 rather than
assumed away. Phase 3 must **verify** the MODEL_SYNC hole is closed, not assume the architecture did
it.

---

## 3. The inbox

### 3.1 Record

One row per successfully decrypted **inboxed** message (§4). Every field is either kit-owned identity
or a declared `client.proto` field — nothing is parsed out of `payload`.

| Field | Source | Notes |
|---|---|---|
| `id` | kit | Monotonic per install. Drain order. Not a message id. |
| `envelopeId` | `Envelope.id` | **`UNIQUE`. Insert with `INSERT OR IGNORE`.** The dedupe key — see below. **New in rev 3.** |
| `kind` | the `payload` oneof arm | §3.1 of SPEC; the message type. **New in rev 2** — without it the record could only describe a `ModelSync`. |
| `receivedAt` | kit clock | When the kit persisted it, not a peer-supplied time. |
| `senderUserId` | `Envelope.sender_id` | Authenticated (§0.10). |
| `senderDeviceId` | **decrypting session address** | Cryptographic attribution (§0.10 rule 4). Doubles as the REPLACE tie-break — see §8.2. |
| `senderDisplayName` | kit's friend graph | Resolved locally, keyed on `senderUserId` (§0.5). `null` if not a friend. **Read §7 before trusting this.** |
| `modelKey` | `ModelSync.model` | **Opaque.** Stored and echoed; the kit MUST NOT act on its meaning. |
| `entryId` | `ModelSync.id` | Opaque. Carried so the app can merge. |
| `op` | `ModelSync.op` | `CREATE` / `UPDATE` / `DELETE`. |
| `sentAt` | `ModelSync.timestamp` | Peer-supplied. Clamped per §2.4 **before** storage. |
| `payload` | `ModelSync.data` | **Opaque bytes.** Never parsed by the kit. |
| `deliveryAttempts`, `lastError` | kit | How many times the app has peeked this row without consuming it, and why it last failed. Kit-owned metadata carrying no application semantics — see §3.4. |

### 3.2 Lifecycle

```
   envelope
      │
      ▼
   decrypt ──────────► FAIL → no ack, no row. Stays on the server. (§0.9 rules 1–2)
      │
      ▼
   classify by kind (§4)
      │
      ├─ kit-internal ─► kit's own durable write ─► FAIL → no ack
      ├─ droppable ────► handle in memory ────────► ack (explicitly permitted, §4)
      │
      ▼ inboxed
   persist inbox row ─► FAIL → no ack. Stays on the server. (§0.9 rule 3)
      │
      ▼
   ACK  (server deletes its copy — the kit's row is now the ONLY copy)
      │
      ▼
   notify app (droppable — the row is the delivery path, not the notification)
      │
      ▼
   app drains: peek(limit, after:) → process → consume(ids) | discard(ids, reason)
```

### 3.3 Normative rules

1. The kit MUST NOT ack until the durable write for that message has committed — the inbox row for
   an inboxed kind, the kit's own store for a kit-internal kind.
2. The kit MUST NOT delete an inbox row except on an explicit `consume(ids)` or `discard(ids, reason)`
   from the app. Not on reconnect, not on logout, not on a size cap, not on a TTL. The row is the
   only copy. **Carve-out:** a device wipe or a remote revocation MUST be able to destroy decrypted
   plaintext — that is a security requirement, not an eviction policy, and it destroys the whole
   store rather than selecting rows.
3. `peek(limit, after: id?)` MUST return rows in `id` order and MUST be side-effect free, except for
   incrementing `deliveryAttempts`. Draining twice without consuming returns the same rows — that is
   the crash-safety property, not a bug. **The `after:` cursor exists so one unreadable row cannot
   block every row behind it.**
4. `consume(ids)` MUST be idempotent and MUST accept a subset. Partial progress is normal.
5. `discard(ids, reason)` deletes rows the app declares it can never process. It MUST be logged as a
   security-relevant event and surfaced — it is data loss, chosen deliberately, and must never be
   the quiet path.
6. The in-process change notification MAY be dropped under backpressure. The row is the delivery
   path (§0.9 rule 4).
7. `inboxDepth()` MUST be exposed. An inbox that grows without bound means the app has stopped
   draining, and that MUST be visible rather than silently absorbed.
8. **The inbox MUST be keyed `UNIQUE` on `envelopeId` and insert with `INSERT OR IGNORE`.**
   *(New in rev 3, 2026-07-25.)* Without this the design is **strictly less idempotent than the code
   it replaces**, which is the opposite of what §2 claims for it.
9. **The inbox is kit-write, app-read-and-delete. There is no `insert` on the API.** *(Decided
   2026-07-26.)* The only candidate for an app-side write was self-sync, and self-sync does not need
   one: a send fans out to the user's **other** devices via the server (`deliver(to: nil,
   toSelf: true)` in both `SyncManager`s), and those devices receive it through the ordinary
   envelope path, so it lands in their inbox the same way a friend's message does. The
   **originating** device is not echoed to and never was — it writes the entry to its own store
   directly, which is an app store write, not an inbox write. Routing it through the inbox would
   mean the app posting a message to itself to read it back a tick later, and it would put a
   locally-authored row into a table whose entire contract is *"this arrived from the server,
   authenticated, and the server's copy is already gone"*. Three methods, and no fourth
   (cf. §8.1's three bridge methods and §9's rule about query APIs).

### 3.3.1 Why the dedupe key is not optional

Persist-then-ack *guarantees* redelivery. The ack is best-effort and its failure is swallowed:

```kotlin
// ObscuraClient.kt:943
try { gateway.ack(listOf(envelope.id)) } catch (e: Exception) { log("envelope ack failed: …") }
```

Persist succeeds, the ack fails or the socket drops before it lands, and the server's per-connection
cursor (`message_pump.rs:63`) redelivers on the next connection. That is correct behaviour — §0.9
requires exactly this rather than losing the message.

**Today that redelivery is harmless, and the reason is the engine being deleted.** `ModelStore.put`
is `INSERT OR REPLACE` on `(model_name, entry_id)`, so a re-delivered entry overwrites itself and
nothing is duplicated. Take the ORM away and put a monotonic `id` in front of it, and the same
redelivery inserts a **second row**: `inboxDepth()` inflates, `processPendingMessages` counts
inflate, and the app posts a duplicate notification for a message the user already has.

Idempotence must therefore be re-established explicitly, at the inbox, where it now lives. One
column and one clause. `Envelope.id` is server-assigned and already the ack key, so it is exactly the
right identity — and the merge rules (§8.2) stay idempotent underneath as a second line of defence,
not as the only one.

### 3.4 Poison rows, and why there is still no eviction policy

A row the app cannot process — an unknown `modelKey` from a newer peer, a corrupt payload, an app
downgrade, a plain bug — must not wedge delivery. Without a cursor it would: the canonical drain is
`peek → process → consume`, the bad row sits at the head forever, `inboxDepth()` never reaches zero,
and on the push path it burns the entire extension budget re-fetching the same row.

Hence `after:` (skip past it), `deliveryAttempts`/`lastError` (make "stuck" observable rather than
inferred), and `discard(ids, reason)` (the app can say *"I permanently cannot read this"* out loud).

What is still deliberately absent is an **eviction policy**. Every automatic eviction rule is a rule
for silently losing a message the server has already deleted. The escape hatch belongs to the app,
where it is a decision; it does not belong to a timer.

> **Correction (rev 2).** This section previously said a cap could "refuse to ack, leaving them on
> the server, where redelivery still works". **That is false.** The server keeps at most
> `max_inbox_size: 1000` messages per device and expires at `ttl_days: 30`, evicting the **oldest
> first** with no tombstone (`obscura-server/src/adapters/database/message_repo.rs`,
> `src/config.rs`). Declining to ack is a bounded reprieve, not durability, and past the bound it
> silently destroys the oldest — most-wanted — messages. Any cap must therefore surface to the user.

> **DEFERRED under YAGNI (rev 3, 2026-07-25).** Ship **`peek(limit)` / `consume(ids)` /
> `discard(ids, reason)` / `inboxDepth()`** and nothing else. The `after:` cursor,
> `deliveryAttempts` and `lastError` are all machinery for a poison row, and a poison row needs a
> payload this app cannot parse. pix defines all four live model keys itself, so that requires **two
> app versions in the field** — which, with no real users, is not now. The `discard` escape hatch
> and `inboxDepth()` alone make a wedged inbox visible and clearable.
>
> This is a deferral, not a repudiation: the reasoning above is sound and the cursor is the right
> answer *when a row actually wedges*. Add it then, with the row that motivated it. `envelopeId`
> (§3.3 rule 8) is **not** part of this deferral — it is load-bearing from day one.

### 3.5 The failure chain nobody has written down

*(Added rev 3.)* Rules 2 and 7 forbid eviction and require `inboxDepth()`. Both are right. Together
they imply a chain worth stating explicitly, because each link is individually correct:

> app stops draining (crash loop, or an iOS user who never opens the app while the NSE keeps acking)
> → inbox grows → disk pressure → the durable write throws → the kit correctly refuses to ack
> → the message stays on the server → the **server's** queue hits `max_inbox_size: 1000`
> → it evicts **oldest first, silently** → permanent loss of the oldest messages.

This is the same conclusion the rev-2 correction above reached from the other end. The answer is
still not eviction: it is that `inboxDepth()` must be **surfaced past a threshold**, not merely
exposed on an API nobody calls. A number no one reads is not observability.

---

## 4. Message kinds, and what each one is allowed to do

`client.proto` has **18** payload arms. The old draft's record could only describe one of them. Every
arm MUST be classified, because the classification is what makes §0.9 checkable rather than aspirational.

| Class | Meaning | Ack rule |
|---|---|---|
| **Inboxed** | Application content. Goes in the inbox; the app drains it. | Ack only after the inbox row commits. |
| **Kit-internal** | Mutates kit-owned state (friend graph, devices, sessions). Never reaches the inbox. | Ack only after the kit's own durable write commits. |
| **Droppable** | Ephemeral by design; no durable delivery guarantee. | MAY be acked without persistence — **explicitly permitted, and only for arms listed here.** |

| Arm | Class | Note |
|---|---|---|
| `model_sync` | Inboxed | The app's entire data path. |
| `content_reference`, `chunked_content_reference` | Inboxed | Attachment references; bytes fetched separately (§5). |
| `friend_request`, `friend_response`, `friend_sync` | Kit-internal | Friend graph is kit-owned (§0.3). See §7 — these decide what a name *is*. |
| `device_announce`, `device_link_approval`, `device_recovery_announce` | Kit-internal | Device graph. |
| `session_reset` | Kit-internal | Session store. |
| `sync_blob`, `sent_sync`, `history_chunk`, `sync_request` | Kit-internal | Own-device sync. |
| `model_signal` | **Droppable** | Typing/read indicators. `client.proto` says "in-memory only". |
| `text` | — | Legacy; deleted by `RESET.md`. |
| `settings_sync`, `read_sync` | **DELETE** | *Decided 2026-07-26.* Zero implementations anywhere — see §4.3. |
| *arm absent from this table* | **Inboxed, unparsed** | *Decided 2026-07-26.* Not "decline to ack" — that is a remote wipe primitive. See §4.1. |

**`SPEC.md` §0.9 needs a matching sentence**: *a payload with no durable delivery guarantee,
enumerated in the proto, MAY be acked without persistence.* Without it, §0.9 is a rule the code
cannot follow — and unfollowable rules are how the last round of false claims started (§0.8).

### 4.1 Unknown arms: inbox them unparsed (decided 2026-07-26)

The question was *inbox it unparsed, or decline to ack.* **Decline-to-ack is not a safe default here,
and the reason is on the server, not the client.**

Any authenticated user can send to any device — friendship is not required to deliver, as both kits'
own `FRIEND_RESPONSE` handlers say out loud (`ObscuraClient.kt:1046`, `ObscuraClient.swift:1936`).
A never-acked message is never deleted, so it redelivers on every reconnect, forever. And the
server's per-device queue is capped at `max_inbox_size: 1000` with `ttl_days: 30`, evicting
**oldest-first and silently** (§3.4's rev-2 correction). Compose those three facts:

> a stranger sends unknown arms in a loop → the kit declines to ack each one → they accumulate on
> the server → the cap is reached → the server evicts **oldest-first**, which is your real
> undelivered mail → permanent, silent loss, triggered remotely, by an unauthenticated-to-you peer.

Declining to ack is the right answer when the failure is **ours and transient** — disk full, a
migration not yet applied, a store that will work on the next launch (§0.9 rules 1–3). An unknown arm
is neither ours nor transient: retrying it changes nothing, so the retry is unbounded by
construction.

**So: an arm absent from the §4 table is persisted as an inbox row and acked.** Concretely:

- `kind` = the arm's proto field name; `payload` = that arm's serialized bytes.
- `modelKey`, `entryId`, `op`, `sentAt` are `null` — they are `ModelSync`-derived and there is no
  `ModelSync` here. The kit does not look inside the bytes (§0 boundary).
- `envelopeId` dedupes it like any other row (§3.3 rule 8).
- The app drains it, cannot process it, and calls `discard(ids, reason)` — data loss that is
  **chosen and logged**, which is exactly what rule 5 exists for.

> **This is the condition on §3.4's deferral.** With `after:` deferred, a row the app will not
> process sits at the head of the drain forever. So pix's drain MUST treat an unrecognised `kind` as
> `discard`, not as "skip and try again later". That is one branch in pix's drain loop, and it is
> what keeps the missing cursor from mattering. If it is ever cheaper to add the cursor than to hold
> that line, add the cursor.

### 4.2 An arm in the table but unimplemented is a kit bug, not an unknown arm

The fallback above keys on **absence from the classification table**, not on absence from
`routeMessage`'s switch. The distinction is load-bearing, because today those two sets are very
different.

`routeMessage` handles **11 of 18** arms in Kotlin and **10 of 18** in Swift (Swift additionally
lacks `device_link_approval` — the gap recorded at Phase 2 sign-off). Everything else reaches
`else -> { }` (`ObscuraClient.kt:1011`) or `default: break` (`ObscuraClient.swift`, `routeMessage`)
and is then **acked and destroyed**:

If these were merely routed to the inbox by the unknown-arm rule, the inbox would become the place
kit-internal work goes to be forgotten, and §4's classification would stop meaning anything. So each
one is resolved here, before the inbox ships.

**The question that resolves them is "who sends it".** An arm nobody sends cannot lose data, whatever
the receiver does; an arm something sends and nothing receives is either a bug or an unfinished
feature, and the difference is whether the sender can fire in the running app.

| Arm | Sender | Receiver | Reachable from the app? | **Resolution** |
|---|---|---|---|---|
| `sync_request` | both kits | none | no caller | **Delete** |
| `history_chunk` | **none** | none | — | **Delete** |
| `content_reference` | both kits | none | no caller | **Delete**, with its send methods |
| `chunked_content_reference` | **none** | none | — | **Delete** |
| `device_recovery_announce` | both kits | none | **no — gated off** | **Keep the arm, defer the handler** |

**Four deletions.** `history_chunk` and `chunked_content_reference` have no sender anywhere, so they
are dead on both sides. The other two are one-sided and neither costs anything to remove:

- **`sync_request` is the pull half of device linking, and the push half already works.**
  `ClientSyncManager.pushHistoryToDevice` sends `SYNC_BLOB`, both kits handle it, and
  `DeviceManager.kt:149` / `ObscuraClient.swift:1084` fire it when a device links. `sync_request`
  (`ClientSyncManager.kt:25`, `ObscuraClient.swift:1015`) is a request nobody answers, and its public
  `requestSync()` has no caller outside the kit.
- **`content_reference`** — see §4.3; pix's attachments never use the arm.

**One deferral, and it is a deferral rather than a bug.** `device_recovery_announce` is built by
`RecoveryManager.kt:48` and `ObscuraClient.swift:1688` and handled by neither kit — but
`ObscuraConfig.enableRecoveryPhrase` defaults to **`false`**, pix never sets it, and pix has **zero**
recovery UI, so `announceRecovery` throws before it can send. Nothing in the running app emits this.
Recovery is a real subsystem — the server's backup side is fully built and E2E-encrypted (§8.4) — so
deleting the arm is wrong; building a receive handler for a flag that is off, with no UI, is the
speculative work Phase 3 is explicitly not doing (`PLAN.md`, "Explicitly NOT in Phase 3").

> **What must change now is the test, not the code.** `RecoveryMessagingTests` exists in **both** kits
> and asserts only that the wire message *arrives*:
>
> ```swift
> XCTAssertEqual(msg.type, "DEVICE_RECOVERY_ANNOUNCE")
> XCTAssertTrue(clientMsg.deviceRecoveryAnnounce.isFullRecovery)
> ```
>
> Nothing asserts the recipient's friend graph or device list changed — because nothing changes them.
> The test passes identically whether or not a handler exists, and none does. That is a delivery test
> named like a feature test: the same false-green pattern the F-findings were about. Rename it and
> state the unimplemented receive half in the file, so the gap is legible to the next reader instead
> of being contradicted by a green tick.

**When the handler is eventually built, note the trap.** `device_recovery_announce` carries
`recovery_public_key` *inside the message whose signature it authenticates*, so the naive
implementation verifies an attacker's signature with the attacker's own key. It must verify against
the **stored** key for that user and treat a first key as TOFU. The neighbouring handler shows the
shape to avoid: `handleDeviceAnnounce` verifies against `friend.recoveryPublicKey` but its `if let`
falls through, so it **accepts an unsigned device list when no key is stored yet**
(`ObscuraClient.swift`, `case .deviceAnnounce`).

**Net effect on the wire.** With these four, plus `settings_sync` / `read_sync` (§4.3) and `text`
(already a `RESET.md` deletion), `client.proto` goes from **18 payload arms to 11** — and the inbox's
classification table, which every kit must implement and keep in step, shrinks with it.

### 4.3 Arms with no implementation on either side

*(Findings from the 2026-07-26 sweep. These change what §4 is classifying.)*

**`settings_sync` and `read_sync` have zero implementations anywhere.** Not "unused by pix" — a
grep across both kits, pix and the server finds them only in generated protobuf and one Swift
`WireCodec` name mapping. Nothing constructs them, nothing sends them, nothing receives them. They
were speculative arms, and classifying them would be designing for a message that does not exist.
**Delete both from `client.proto` and `reserved 41, 42;`.** That answers the §11 question by removing
its subject. (This also disposes of the related §11 note: `pix/src/models/schema.ts:35` declares a
`settings` model with zero references in `src/` — re-confirmed 2026-07-26. Four live models.)

**`content_reference` / `chunked_content_reference` are sent by both kits and received by neither,
and the app calls neither sender.** `MessagingManager.kt:54` and `ObscuraClient.swift:1243` build
them; nothing handles them on receive; and pix's bridge exposes only `uploadAttachment` /
`downloadAttachment` (`src/native/ObscuraModule.ts:147`), not the reference-sending API.

pix's attachments do not use this arm at all. They ride **inside a `model_sync` entry** —
`RecipientPicker.tsx:58` puts `mediaRef` / `contentKey` / `nonce` on the story model — and the bytes
are fetched by id afterwards. That is a coherent design and it is the one in production.

So §4 was classifying as "Inboxed" a path with **no sender and no receiver in the live app**.

**Resolved (§4.2): delete both arms and the send methods with them.** `chunked_content_reference` has
no sender at all; `content_reference`'s only senders are kit-public methods the bridge does not
expose. The attachment *bytes* path — `uploadAttachment` / `downloadAttachment`, `AttachmentCrypto`,
the attachment cache — is untouched by this: it is the reference **message** that is dead, not
attachments. Keeping the arm "as documented protocol" would mean shipping a classification that
asserts a live inbox path where there is no sender, no receiver and no caller.

---

## 5. Send and attachments

```
send(recipientUserIds, modelKey, entryId, op, sentAt, payload) -> queued | error
```

The **caller names the recipients** (§0.4). The kit fans out to every device of every listed userId,
plus the author's own **other** devices, and makes no delivery decision of its own. It returns when
the submission is durably queued, not when delivered.

> Two things to prove with a test before building on this: (i) the sending device must be **excluded**
> from its own fan-out — the ORM's self-sync target list is currently the only one that does not
> filter it, so a send may echo to itself; (ii) the sending device gets **no inbox row**, so pix must
> write its own outgoing entry locally. That is one write path in the kit and two in the app.

**Attachments — corrected in rev 2.** The proposed `uploadAttachment(bytes) -> id` /
`downloadAttachment(id) -> bytes` cannot work: the content key lives **inside `payload`**, which the
kit may not read, and moving image bytes through the bridge is exactly the cost §10 avoids. The
shipped, correct shape:

```
uploadAttachment(filePath)                    -> { id, contentKey, nonce }
downloadAttachment(id, contentKey, nonce)     -> filePath
```

The app stores `{id, contentKey, nonce}` inside its own payload; the kit treats them as opaque.
**Coupling to write down:** attachment blobs expire server-side at 30 days while inbox rows have no
expiry, so an unconsumed row can outlive its own media.

---

## 6. The rest of the surface

| Group | Calls |
|---|---|
| **Auth / session** | `register`, `login`, `loginSmart` (returns a scenario, incl. new-device / device-mismatch), `loginAndProvision`, `logout`, `restoreSession` |
| **Identity** | `getUserId`, `getUsername`, `getDeviceId` |
| **Connection** | `connect`, `disconnect`, `connectionState` |
| **Friends** | `getFriendCode`, `addFriendByCode`, `befriend`, `acceptFriend`, `removeFriend`, `friends()`, `getPendingRequests()` |
| **Devices** | `ownDevices()`, `generateLinkCode`, `approveLink`, `revokeDevice`, `takeoverDevice` |
| **Inbox** | `peek(limit, after:)`, `consume(ids)`, `discard(ids, reason)`, `inboxDepth()` |
| **Send** | `send(...)` (§5) |
| **Attachments** | `uploadAttachment`, `downloadAttachment` (§5) |
| **Signals** | typing / read indicators — droppable (§4), **not** inbox rows |
| **Push** | `processPendingMessages(timeout)` → counts by **opaque** model key; `registerNotificationTemplates` (§7) |
| **Events** | `inboxChanged`, `connectionChanged`, `authStateChanged`, `authFailed`, `friendsUpdated`, `pushTokenReceived`, `appStateChanged`, `launchedFrom` |

> **This list is still not exhaustive, and that is a known risk.** pix's bridge exposes ~44 methods
> and 11 event types. A list that omits calls gets completed ad hoc during the port by whoever hits
> the gap, which is the mechanism §0.8 describes. **Before any code: diff this table against
> `obscura-pix/src/native/ObscuraModule.ts` and justify every omission as a deletion.**
> `friendsUpdated` is the sharpest example — with only `inboxChanged`, an inbound friend request is
> invisible until something polls.

`processPendingMessages` returns zero counts when it genuinely cannot connect (`PLAN.md` F10);
making that distinguishable is a Phase 4 decision and this API is where it lands. Counts MUST be of
rows **persisted during that call** — not depth, not rows the app already consumed — and whoever
composes the notification MUST read the store, not a channel. (Today a channel race means an FCM
wake can steal a message from the app and post no notification at all.)

---

## 7. Notifications: whose name, and whether to show one

> **CUT FROM PHASE 3 under YAGNI (rev 3, 2026-07-25). Keep the threat-model reasoning; drop the
> API.** Three reasons, in order of weight:
>
> 1. **It contradicts §9.** `registerNotificationTemplates(templates: { modelKey: String })` is
>    per-model configuration, stored durably in the kit, keyed by application model names. That is
>    the same thing `ObscuraConfig.conversationModel` is being **deleted** for, wearing a different
>    name. §9 lists exactly this as deliberately absent.
> 2. **Android already solves it outside the kit.** `ObscuraSession.kt:272`'s `classifyForNotification`
>    plus `NotificationHelper.postGeneric` run in the FCM cold-start process, in the app repo, and
>    already enforce the generic-copy invariant. The kit is not needed for this and does not have it.
> 3. **The only consumer that cannot do this app-side is the iOS NSE, which does not exist yet** —
>    and Phase 4 builds it. Designing its configuration API now, before the extension that would use
>    it, is speculative by definition.
>
> The four open sub-cases below (template miss, first run, null name, locale) are design work for a
> feature nobody has asked for. **Decide this in Phase 4, against a real NSE.** What survives into
> Phase 3 is one sentence: *the kit exposes `senderDisplayName` on the inbox record; what appears on
> a lock screen is the app's decision, and today's answer is "nothing".*

The old draft had the kit compose `"{senderDisplayName}" + template[modelKey]`. That was a
**privacy-model change made in passing**, and it rested on a name that was not trustworthy.

**The name is now trustworthy — as of today, and only just.** Both kits accepted a payload-supplied
username into the friend graph unconditionally, so an accepted friend could rename itself and a
stranger could self-accept into the graph under a chosen name (fixed 2026-07-25:
ObscuraKit-Kotlin #45, ObscuraKit-swift #13, with tests that fail against the old code). Any design
that puts a graph name on a lock screen depends on that fix holding, so it needs a regression test
standing over it permanently — those PRs are it.

**pix today shows nothing.** Its notification path documents generic text — *no sender, no content* —
as an invariant "enforced here, never relaxed" (`NotificationHelper.kt`). Silently replacing that
because a new API made it convenient is the wrong way to change a threat model.

**Proposal: make it an explicit, persisted, app-chosen policy, defaulting to today's behaviour.**

```
registerNotificationTemplates(
  templates: { modelKey: String },        // "sent you a pix"
  includeSenderName: Bool = false         // default: preserve today's privacy posture
)
```

- **Default `false`** — a privacy default may be *changed*, but never by accident.
- Stored **durably in the kit**, because an NSE or an FCM cold start has no JS runtime to call this.
- The kit composes from registered copy plus, only if permitted, the graph name. It never reads a
  payload to build notification text (§0.4).
- Unspecified today and needing an answer: template **miss** (unknown `modelKey`), first run before
  any registration, `senderDisplayName == null` (non-friend or self-sync), and locale change.
- Whether the product wants the name at all is a **product decision**, not an API one. The API's job
  is to make it a decision rather than a side effect.

---

## 8. What this means for `obscura-pix`

### 8.1 pix's store already exists — keep the table, delete the engine

> **Corrected 2026-07-25.** The draft said pix "must gain a durable store … the largest single piece
> of work in Phase 3". That was wrong, and it made the phase look far bigger than it is.

**`obscura-pix` has no persistence of its own** — no AsyncStorage, no MMKV, no SQLite in `src/`, and
its in-memory state is zustand rebuilt from `allEntries` on every `entriesChanged`. But the *table*
it needs is already on disk, in the kit's own schema:

```sql
-- ObscuraKit-Kotlin/lib/src/main/sqldelight/com/obscura/kit/ModelEntry.sq
CREATE TABLE IF NOT EXISTS ModelEntry (
    model_name TEXT NOT NULL,
    entry_id   TEXT NOT NULL,
    data       TEXT NOT NULL,
    timestamp  INTEGER NOT NULL,
    author_device_id TEXT NOT NULL,
    deleted    INTEGER NOT NULL DEFAULT 0,
    ttl_expires_at INTEGER,
    PRIMARY KEY (model_name, entry_id)
);
```

`ModelStore.kt` writes `data` as `JSONObject(entry.data).toString()` — **plain application JSON, with
the merge metadata in columns beside it, not folded into the blob.** So the row format is directly
reusable and there is no data transform, no new schema, and no migration.

Phase 3's storage work is therefore: **keep the table, delete the ~1,726 lines of engine above it,
and expose three *storage* methods across the bridge** (`putEntry`, `allEntries`, `findEntry`).

> **The send half is not included in that three, and saying "three bridge methods" without this
> caveat undersells the phase** *(corrected 2026-07-25)*. `createEntry` is not a store call:
> `Model.kt:66` ends in `syncManager?.broadcast(this, entry)`, so it is the app's **entire outbound
> path**, and pix has no `send` of its own today. After the reset pix must resolve the audience for
> four models, honour `SPEC §1.2`'s fail-loud rule (see `RESET.md`, "The `routing.json` leak
> guards"), call the kit's `send`, **and** write its own local row — the last of which §5 notes but
> the "three methods" framing hides. The scope guard is still *three storage methods and no fourth*;
> it was never a claim that the whole phase is three methods. `timestamp` and
`author_device_id` stay — they cost nothing and they are the only thing a future sync could be built
from. `ModelAssociation` goes with the relationships that were never bridged; `deleted` and
`ttl_expires_at` go dead with tombstones and `TTLManager` and can be dropped whenever convenient.

The store stays **native**, not TypeScript. Two reasons: iOS already opens this database through
SQLCipher with a per-user Keychain key (`ObscuraClient.swift:282`), and a TS-side store would need
that key to cross the bridge into the JS heap; and the inbox drain (§3) then runs entirely in one
process, with no bridge round-trip inside the peek → write → consume window.

> **Platform gap, unrelated to this phase but found while checking the above.** Android does *not*
> encrypt: `obscura-pix/android/.../ObscuraSession.kt:174` constructs
> `AndroidSqliteDriver(ObscuraDatabase.Schema, appContext, dbName)` with no factory, and SQLCipher
> appears nowhere in the Android app — while `ObscuraClient.kt:83` documents the encrypted factory
> integrators are meant to pass. Model entries **and** Signal session state, identity keys and
> prekeys are plaintext in app-private storage, protected only by the app sandbox and full-disk
> encryption. Turning SQLCipher on re-keys an existing database (`sqlcipher_export`) or wipes it, so
> it does not belong inside the deletion — **separate work, and it should not drift indefinitely.**

### 8.2 Merge moves to pix — including the tie-break

- **APPEND** — dedupe by `entryId`. (`directMessage`, `story`)
- **REPLACE** — higher `sentAt` wins; **on equal `sentAt`, the higher `senderDeviceId` wins**.
  (`pix`, `profile`)

> **Corrected in rev 2.** The draft said "equal `sentAt` is idempotent", i.e. first-seen wins — which
> is exactly the silent permanent divergence `SPEC.md` §2.2 calls the tie-break **mandatory** to
> prevent: two devices receiving the same pair in different orders converge to different states and
> never reconcile. `pix.viewedAt` is written by a second user, so equal timestamps are not
> hypothetical.
>
> The tie-break key is the **authenticated** `senderDeviceId` from the decrypting session — not
> `ModelSync.author_device_id` (wire field 7), which is peer-asserted and contradicts §0.10 rule 4.
> Each device stamps its own local writes with its own device id and received writes with the
> authenticated sender's, so both sides compare the same pair and compute the same winner. **This is
> strictly stronger than today: the tie-break becomes cryptographically attributed. Wire field 7
> should then be deleted** — it is currently on no deletion inventory.

Both rules are **idempotent**, which is what makes redelivery from the inbox safe: crashing between
`peek` and `consume` and reprocessing converges. **The drain protocol's safety depends on this — a
future non-idempotent rule breaks the contract with it.**

> **Caveat found by mutation-testing the ported vectors (2026-07-25):** APPEND is *first-wins*, so it
> converges only under its real invariant — that an entry id is written once, and any duplicate is a
> redelivery of identical bytes (ids are `model_timestamp_random`). A **hostile** peer replaying an
> established id with *different* content makes the outcome arrival-order dependent, and the two
> devices can disagree about that entry.
>
> That is a **deliberate choice, not a defect**: the alternative, last-wins, converges but lets any
> peer overwrite an entry you already hold by replaying its id — structurally the same as the
> friend-graph self-rename fixed the same day. Divergence about a hostile write is visible and
> recoverable; a silently rewritten message is neither. Pinned by
> `pix/src/domain/__tests__/merge.test.ts`, which asserts the non-convergence explicitly so nobody
> later "fixes" it.
>
> The vectors alone could not have caught this: `merge.json`'s idempotence case uses a duplicate
> carrying identical data, so first-wins and last-wins are indistinguishable there. Flipping APPEND
> to last-wins passed all 13 vector-driven tests.

**`merge.json` is a contract today and a fixture tomorrow, and the switch has an order.** While the
kits still implement these semantics there are three implementations, so it is a genuine conformance
vector and `obscura-pix` is right to read it from its `proto/` submodule (which it does, as of pix
PR #56). Once Phase 3 deletes the kits' merge engine there is exactly **one** implementation left,
and a vector with one implementation is not a contract — it is pix's test fixture, and it should move
into pix. **pix must be reading a local copy before this file is deleted**, or the deletion breaks
pix's only test suite on the day it lands. Sequenced in `RESET.md`, "The `merge.json` handover".

**`conformance/merge.json` ports partially, not wholly.** Of its six cases, three survive as
APPEND/REPLACE (GSet union, GSet idempotence, LWW higher-timestamp) and one survives *only because*
of the tie-break decision above. The two tombstone cases (`newer tombstone wins`, `stale write does
not resurrect`) pin deletes, which `RESET.md` removes — so they retire with the feature rather than
"port". Say which are which when porting; do not claim the file moved.

### 8.3 Also pix's, now

Schema and validation; recipient resolution; queries, filtering, sorting; expiry (`expiresAt` the app
filters — `story`'s `ttl: '24h'` becomes a field, and **nothing in pix references expiry today**, so
this is a feature to build); notification copy (§7); and display names — from `senderDisplayName` or
`friends()`, never a payload. `senderUsername` has **18 references** in `pix/src`, all needing rerouting.

### 8.4 Backup: the server already provides it, and we ship it empty

`consume` deletes the inbox row, so history lives only in the app's store. The draft concluded from
this that "pix owns multi-device convergence and backup, or the product has neither," and that
Phase 3 had to decide it. **Both halves were wrong** — the server owns backup already, and it is
built.

**What `obscura-server` offers today:**

| | |
|---|---|
| `GET /v1/backup` | Streams the blob. `ETag` = version; honours `If-None-Match` (304 when current) |
| `HEAD /v1/backup` | Version + size without downloading — "is my copy stale?" |
| upload | `If-Match` for optimistic concurrency; `If-Match: *` asserts first-ever upload |
| storage | Streamed to S3/MinIO under `backups/`; never buffered in the server |
| safety | `UPLOADING`/`ACTIVE` states plus a `backup_cleanup` worker that reaps stale uploads |
| limits | **2 MB max**, 32 bytes min, both configurable (`config.rs`) |

> **CORRECTION (2026-07-25, same day).** This section first stated as fact that the backup "is
> already end-to-end encrypted … the server holds ciphertext it cannot read." **That is true of
> Kotlin only, and it is a live confidentiality defect on iOS.** Guardrail 4 in `RESET.md` — *a doc
> comment asserting a safety property must cite the test that proves it* — applies to this document
> too, and this is what it looks like when it is not followed.

**Kotlin** encrypts: `RecoveryManager.uploadBackup()` gzips and encrypts under
`BackupCrypto.encrypt(compressed, recoveryPublicKey)`, and `downloadBackup(recoveryPhrase)` decrypts
with the mnemonic. It **falls back to plaintext when `recoveryPublicKey == null`**, silently.

**Swift does not encrypt at all**, despite a doc comment that says it does:

```swift
// ObscuraClient.swift:1700 — "/// Upload encrypted backup to server."
let exportData = SyncBlobExporter.export(friends: friendsData, messages: [])
let etag = try await api.uploadBackup(exportData, etag: backupEtag)
```

No `BackupCrypto`, no recovery key — and `SyncBlob.swift:36` reads *"In production this would be
gzipped. For now, raw JSON."* **On iOS the server receives the user's plaintext friend list.**

Two consequences beyond the confidentiality gap:

- **The two kits' `SyncBlob`s are mutually unreadable** — Kotlin gzips, Swift emits raw JSON, and the
  field sets differ (`type` + `authorDeviceId` vs `isSent`). `DEVICE_LINK_APPROVAL.friendsExport`
  rides on this, so a Kotlin↔Swift device link transfers **nothing** — before it even reaches
  Swift's missing `case .deviceLinkApproval`.
- **The `messages` slot is the legacy TEXT shape, not model entries.** `SyncBlob.kt:33-45` writes
  `{messageId, conversationId, content, timestamp, type, authorDeviceId}` from `MessageData` — i.e.
  from `MessageDomain`, **which `RESET.md` deletes**. So "the format already has room, filling it is
  not new infrastructure" is wrong twice over: wrong shape, and its producer is on the deletion list.
  There is also no version field in the blob (`{friends, messages, timestamp}`), so changing the
  shape has no compatibility story.

The *conclusion* below survives all of that — backup is still the server's, still built, still not
Phase 3 work. The reasoning needed the correction.

**And the blob format has a slot we ship empty.** `SyncBlob.kt:21` is
`export(friends, messages: Map<…> = emptyMap())` — Kotlin never passes the second argument, Swift
writes `messages: []` explicitly (`ObscuraClient.swift:1031`, `:1061`), and `pushHistoryToDevice`
does the same. The slot is the legacy TEXT shape rather than model entries (see the correction
above), so filling it means defining a new payload — but the **endpoint, the versioning, the
concurrency control and the transport are all built and unused.**

So filling it later is not new infrastructure on either side, and **Phase 3 owes only that it not
foreclose the option** — which it does not, for free, since `timestamp` and `author_device_id` stay
on the table (§8.1). Under YAGNI this is **not Phase 3 work.**

> **Backup is device-scoped, and recovery does not re-derive a device id.** `api/backup.rs:25`
> requires a device-scoped token and keys every row on that `device_id`; `devices.id` is
> `uuidv7()` generated by Postgres (`migrations/001_core_and_auth.sql`), never client-supplied and
> never derived from the seed — `device_repo.create` takes only `(user_id, name)`. A fresh install is
> therefore a new device whose `GET /v1/backup` is a 404, while the old blob sits in object storage
> encrypted under a phrase the user still holds.
>
> It is still reachable, by **re-claiming rather than re-deriving**: `list_devices` accepts a
> user-only JWT, and `auth_service.login` will mint a *device-scoped* token for any `device_id` that
> `belongs_to_user`. So login → list devices → login again naming the old device → `GET /v1/backup` →
> decrypt with the phrase. **No kit implements this today.** Whoever builds restore should follow
> that path rather than assume a derived identity.
>
> Note the property that path depends on: **a password alone yields a device-scoped token for any of
> that user's existing devices.** Device identity is an id the server hands out on request, not
> something bound to key material. The backup stays confidential (it is encrypted under the recovery
> phrase), but an attacker holding only the password can overwrite it, or drain and ack that device's
> queued envelopes. Recorded here as an observation; it is a server-side question, not a Phase 3 one.

---

## 9. What is deliberately absent

| Absent | Why |
|---|---|
| `defineModels` / any schema | A kit does not act on application fields, so it does not parse an application schema (§0.4). |
| Query DSL, `orderBy`, `limit`, operators | Derived state is the app's (§0.4). Zero callers today. |
| `observe()` on entries, filtered observation | Same. The app owns its store. |
| Relationships, `include()` | Never bridged; never used. |
| CRDT engine (GSet, LWWMap) | Two idempotent rules applied by the app (§8.2). |
| Audience / routing engine | The caller names recipients (§0.4). |
| `TTLManager` | An `expiresAt` field the app filters. |
| Typed models | A Kotlin-only API the app cannot reach. |
| `deleteEntry`, `queryEntries` | Zero callers. |
| Per-model config in kit settings | Config naming an application concept proves the boundary was already crossed (§0.4). |

`peek`/`inboxDepth` are **not** a query or observation API in the §0.4 sense: they read the kit's own
delivery queue, not application-derived state. Stated so it is not relitigated.

**The test for adding anything here later:** can it be done in TypeScript, in the app? If yes, it does
not belong in the kit — no matter how convenient the bridge makes it.

---

## 10. Prerequisites, and the migration order

**Three decisions, taken 2026-07-25, each to be done BEFORE the first line of inbox code.**

### P1 — Kotlin adopts SQLDelight migrations (decided)

**The two kits fail differently for the same change, and only one fails loudly.** Swift creates every
table with `CREATE TABLE IF NOT EXISTS` at store init, so a new store class quietly works on an
existing database. Kotlin opens with `AndroidSqliteDriver(ObscuraDatabase.Schema, …)`, has **zero**
`.sqm` files and no `deriveSchemaFromMigrations`, so the generated schema version never moves: on an
existing DB neither `create()` nor `migrate()` runs, and the new table simply does not exist. The
persist step then throws, the kit correctly refuses to ack — and that device receives nothing,
silently, forever, while the server prunes at 1000 messages / 30 days.

| Option | Cost | Verdict |
|---|---|---|
| **Adopt SQLDelight migrations** (`.sqm` + a migration test) | one-time, small | **Chosen** |
| Greenfield wipe on mismatch | small, but destroys the Signal identity — the device must re-provision | escape hatch only, never automatic |
| Mimic Swift (`IF NOT EXISTS` on open) | trivial | rejected — codifies an accident as a design |

Chosen for a reason beyond the inbox: **Phase 3 deletes tables too** (ORM entries, model config). A
kit with no migration mechanism can only add by luck and can never remove. The migration must be
covered by a test that a *migrated* database and a *freshly created* one end up with identical
schemas — the double-entry between `.sq` and `.sqm` is exactly the mistake that produces the silent
per-device kill above.

`RESET.md`'s "greenfield — bump the schema and wipe" is only half true: `versionCode 1` and no store
release mean no public installs, but developer devices are precisely the population this kills, and
the wipe mechanism it refers to was itself deleted.

### P2 — the store moves to an App Group container, now (decided)

The iOS NSE cannot reach today's store on ~~two~~ **three** counts: the DB path is
`.applicationSupportDirectory` (app-private, not an App Group), the SQLCipher key is stored with no
`kSecAttrAccessGroup`, so an extension in a different bundle id cannot read it — and **the session
token is in the same position**. **The inbox and the entry table (§8.1) share that database, so this
decides where both live.**

> **Third count added 2026-07-26, while implementing this.** The drafted version named only the
> database and its key, which is necessary and not sufficient. **There is no REST message fetch** —
> `POST /v1/messages` is send-only and delivery is exclusively the gateway WebSocket (see Phase 4's
> note in `PLAN.md`). So an NSE must `POST /v1/gateway/ticket` before it can receive anything, and
> that needs the auth token, which pix keeps in `KeychainSession` under `kSecAttrService` with no
> access group. An NSE that can open the database but cannot authenticate does nothing at all.
>
> Two things follow that are easy to get wrong and are worth stating once:
>
> - **A keychain item cannot change access group in place.** It must be deleted and re-created. So
>   the pre-move item survives the switch, and a query naming a group only matches that group — the
>   old copy is unreachable rather than gone. For a *bearer token* that means logout leaves a live
>   credential in the keychain permanently unless the delete explicitly targets both groups.
> - **The token must be `kSecAttrAccessibleAfterFirstUnlock`, not `…WhenUnlocked`.** An NSE runs
>   while the device is locked; an item it cannot read is an extension that cannot authenticate. The
>   SQLCipher key already uses the `…ThisDeviceOnly` variant of the same class.
>
> Implemented in obscura-pix PR #60, which also records why the App Group id is used directly as the
> keychain access group rather than adding a `keychain-access-groups` entitlement: that entitlement's
> **first** entry becomes the default group for items that do not name one, which silently relocates
> existing items.

Do it in Phase 3, not Phase 4. Now it costs a path change, a keychain attribute and an entitlement.
Later it costs a **data migration of the only copy of the user's messages**, on a kit whose migration
mechanism is P1. The asymmetry is the entire argument: today there is no data worth migrating.

**Status: the app half is implemented (obscura-pix PR #60, 2026-07-26); the kit half needed no
change.** `ObscuraClient.init(…, keychainAccessGroup:)` already plumbs through to
`DatabaseSecret.getOrCreate`, and the kit has always taken `dataDirectory` from the caller — so P2
was app-side in its entirety. **It is not proven, and cannot be here:** pix CI has no iOS job and the
kit does not build on Linux, so nothing in CI executes this path. Two things gate it actually
working, both outside the code: the App Group must be **registered in the Apple Developer portal**,
and an unregistered group yields a `nil` container at runtime rather than a build error. pix's
`SharedContainer` therefore degrades to the pre-P2 behaviour and logs loudly on every launch instead
of failing — treat a quiet log, not a green build, as the evidence.

> **Split under YAGNI (rev 3, 2026-07-25): do the path-and-keychain half now, defer the concurrency
> machinery.** The argument above is right and cheap *for the path, the keychain attribute and the
> entitlement* — that half stays in Phase 3. The two items below are **Phase 4**, because the second
> process they exist to coordinate **is the NSE, which does not exist yet**. Building an advisory-lock
> protocol and converting the database to `DatabasePool` + WAL to arbitrate between one process and a
> hypothetical one is machinery ahead of its requirement — and it will be designed better against a
> real extension, inside a real 30 s / 24 MB budget, than against an imagined one.

Two things settled with it — **both deferred to Phase 4 per the note above**:

- **Single-drainer rule.** The server's per-device notifier is a broadcast and each `MessagePump`
  keeps its own cursor, so app and NSE can both connect, both insert and both notify. Exactly one
  process may hold the gateway connection at a time, enforced by an advisory lock file in the App
  Group container; the NSE takes it only when the app does not hold it.
- **GRDB `DatabaseQueue` → `DatabasePool` + WAL**, since two processes will touch the file even under
  a single-writer rule.

### P3 — pix's store is SQLite, chosen deliberately (decided)

Not AsyncStorage, not MMKV. The app now owns merge-by-`entryId`, REPLACE-by-timestamp-with-tie-break
(§8.2), `expiresAt` filtering and conversation queries — all of which want indexed lookups. A
key-value store turns every merge into a read-modify-rewrite of the whole model, which is
approximately what `allEntries`-refetch-everything does today, and that pattern is being deleted
*because* it does not scale.

It is the easiest choice to default into by accident, which is why it is written down as a decision
rather than left to whoever starts first.

> **Correction (2026-07-26).** This paragraph used to end "this is also the largest single piece of
> Phase 3 work (§8.1)", which **contradicts the §8.1 correction two sections above it**: the SQLite
> store already exists as `ModelEntry`, with the columns this design needs, and pix takes ownership
> of it across the bridge rather than building one. What P3 actually decides is only that the store
> stays SQLite and stays native. The long pole moved: it is now the **in-memory kit double** that
> makes migration step 3 testable at all (see the status note under "Order"), not the store.

### Order

1. **P1** — unblocks every later schema change, additive or destructive.
2. **P2** — cheapest while there is no data.
3. **P3** + pix's test suite — `merge.json`'s portable cases become its first tests. **Done as far
   as merge goes** (`src/domain/merge.ts`, 22 passing tests). What remains here is the **in-memory
   kit double** — the long pole, per the correction under P3 and the status note below.
4. Then the inbox itself, Kotlin designing first.

**Migration order** — pix cannot compile against a kit whose old API is gone, and it has **one**
TypeScript surface for both platforms with both kits consumed from source (Gradle composite build,
local SPM). So "Kotlin ships first" would break iOS for the whole duration of the Swift port:

1. pix gains its test suite **(done, PR #56)** and takes ownership of the existing `ModelEntry`
   table across the bridge (§8.1) — **DONE 2026-07-28**, via `EntryStore` in both kits
   (Kotlin #51, Swift #19) and the bridge methods in pix #64;
2. **both** kits gain `inbox` + `send` alongside the existing ORM — Kotlin **designs** first and
   Swift ports the proven shape, but pix does not switch until both have landed — **DONE
   2026-07-28**: inbox (Kotlin #49, Swift #18), `send` (Kotlin #52, Swift #20);
3. pix switches to the new API — **DONE 2026-07-28** (pix #64, #65). Every ORM call site is gone
   from pix's production code;
4. the old surface is deleted, per kit — **NOT STARTED.** This is `RESET.md`.

Pin kit commits in pix CI for the duration, so step 4 cannot strand an older pix commit.
**Done and currently active**, pinned at `503ce22` (pix #63, bumped in #64). **Remove the pin after
step 4 lands**, restoring the float — the comment block at the pin says so at the line that has to
change.

> **What steps 1–3 actually cost, recorded because the estimate was wrong in both directions.**
>
> The *store* was much smaller than this document predicted — §8.1's correction was right, the table
> already existed and `EntryStore` is ~100 lines per kit. What was NOT predicted: `send` had to be
> built too (§5 existed as a design but not as code), the audience resolver had to move with it, and
> an adversarial review of the inbox found **five data-loss defects** in freshly-merged code —
> including an ORM call left inside the ack gate that made a malformed payload wedge the receiver
> permanently, and an unvalidated `Envelope.id` that collapsed every short id onto one dedupe key.
>
> The lesson for step 4 is not "go slower"; it is that **the dangerous defects were all in the
> ordering of effects, not in the deletions**. Step 4 is mostly subtraction, which is the safer half.

> **The `routing.json` handover is COMPLETE (2026-07-28).** `RESET.md` requires pix to vendor the
> five leak guards and have them passing *before* the vectors are deleted here. They are transcribed
> verbatim in `obscura-pix/src/domain/__tests__/audience.guards.test.ts`, against
> `src/domain/audience.ts`. The precondition on deleting `conformance/routing.json` is met.

> **Status of that mitigation (checked 2026-07-25): NOT done, and two things about pix CI change how
> much this plan can lean on it.**
>
> - **The kit checkout floats.** `.github/workflows/ci.yml`'s `android` job checks out
>   `rhelsing/ObscuraKit-Kotlin` with **no `ref:`**. That float is *deliberate* — the job's own
>   comment explains it couples pix's green to the kit's `main` and catches cross-repo toolchain
>   drift — so it should not be pinned permanently. It must be pinned **for the duration of steps
>   2–4**, because those steps deliberately land a kit deletion before pix can switch. A reminder
>   now sits in the workflow at the line that has to change.
> - **There is no iOS job at all** — the jobs are `typecheck`, `domain-tests`, `lint`, `android`. So
>   the entire argument for this ordering ("Kotlin ships first would break iOS for the whole
>   duration of the Swift port") is **invisible to CI**: nothing would go red if Swift and pix's one
>   shared TypeScript surface diverged. The ordering is sound; it is currently enforced by care
>   rather than by a check.
>
> **And step 1's "done" is thinner than it reads.** pix's suite is 12 tests over an 80-line pure
> function, and `ObscuraModule.ts:11` proxies every native call to a noop under jest
> (`new Proxy({}, { get: () => noop })`). Nothing that crosses the bridge is testable as configured,
> which makes **step 3 ("pix switches to the new API") untestable by construction**. What that step
> needs is an in-memory kit double, not more merge tests. Budget for it there rather than
> discovering it mid-switch.

---

## 11. Still open

- ~~`settings_sync` / `read_sync` classification (§4).~~ **Answered (§4.3):** both have zero
  implementations anywhere — delete the arms and reserve 41, 42. The `settings` model's zero
  references are re-confirmed; four live models, not five.
- ~~Unknown-arm policy: inbox unparsed, or decline to ack (§4)?~~ **Answered (§4.1):** inbox it
  unparsed. Declining to ack composes with the server's oldest-first eviction into a remotely
  triggered wipe of real mail.
- ~~Does the app ever need to write to the inbox?~~ **Answered (§3.3 rule 9):** no. Self-sync reaches
  other devices through the ordinary envelope path; the originating device writes to its own store.
- ~~The five arms classified in §4 that neither kit implements.~~ **Answered (§4.2):** four are
  deleted (`sync_request`, `history_chunk`, `content_reference`, `chunked_content_reference`) and
  `device_recovery_announce` keeps its arm with the handler deferred — it cannot fire, because
  `enableRecoveryPhrase` defaults to `false` and pix has no recovery UI. `client.proto` goes from 18
  arms to 11.
- **New, from §4.2:** `RecoveryMessagingTests` in **both** kits asserts only that the wire message
  arrives, so it passes with no handler — a delivery test named like a feature test. Rename and
  annotate; tracked as kit work, not proto work.
- **New, from §4.2:** when the recovery handler is built, it must verify against the **stored**
  recovery key, never the one inside the message. `handleDeviceAnnounce` already accepts an unsigned
  device list when no key is stored — worth closing at the same time.
- Template miss / first-run / null-name / locale behaviour for notifications (§7).
- ~~Multi-device convergence and backup — whose problem, and in which phase?~~ **Answered (§8.4):**
  the server's, already built; the client fills `SyncBlob`'s empty `messages` slot in a later phase.
  Phase 3 owes only that it not foreclose it, which it does not.
- Restore-after-reinstall must re-claim the old `device_id` from `list_devices`, not re-derive it
  (§8.4). Which phase builds that, and does the password-alone device-claim property need closing
  first?
- **`obscura-client-web` is a fourth wire-compatible client** with its own ModelSync ORM. It is
  non-normative, but nothing says "retire it", and if it is ever run against an account after pix
  switches it will emit tombstones and LWW writes pix no longer understands.
