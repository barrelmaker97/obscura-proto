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
| `settings_sync`, `read_sync` | **UNDECIDED** | Decide before coding. Both look like application concerns that should be `model_sync`, not their own arms. |
| *unknown / future arm* | **MUST NOT be silently dropped** | Today `routeMessage`'s `else` branch falls through to the ack, destroying it. Either inbox it unparsed or decline to ack. |

**`SPEC.md` §0.9 needs a matching sentence**: *a payload with no durable delivery guarantee,
enumerated in the proto, MAY be acked without persistence.* Without it, §0.9 is a rule the code
cannot follow — and unfollowable rules are how the last round of false claims started (§0.8).

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

### 8.1 pix must gain a durable store — this is new work, not a move

**`obscura-pix` has no local persistence today.** No AsyncStorage, no MMKV, no SQLite: its entire
durable store is the kit's ORM, and its in-memory state is zustand rebuilt from `allEntries` on every
`entriesChanged`. Deleting the ORM therefore does not *move* pix's storage — **it removes it**. This
is the largest single piece of work in Phase 3 and it is absent from `RESET.md`, whose inventory
lists only what comes *out* of the kits.

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

### 8.4 The consequence of one table: no history, no backup, no second device

`consume` deletes the row, so history lives only in the app's new store. Today the only cross-device
transfer is `SyncBlob`, and it carries **friends only** — `pushHistoryToDevice` passes an empty
message list; account backup is the same blob. So after the reset: **a newly linked device, or a
reinstall, is permanently empty**, with no mechanism that could fill it, and the server's copies are
long since acked and deleted.

That is true today too, but today the kit at least *holds* the entries, so a sync could be built on
them. After this change, **pix owns multi-device convergence and backup, or the product has neither.**
This needs a decision in Phase 3, not a discovery in Phase 5.

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

**Before the first line of inbox code:**

1. **Kotlin has no schema-migration mechanism.** Every table is `CREATE TABLE IF NOT EXISTS`, there
   are no `.sqm` files, and `DatabaseMigrations` was deleted. Adding an `Inbox` table to an existing
   install yields `no such table` inside the persist step — which correctly refuses to ack, and then
   that device receives nothing, silently, forever, while the server prunes at 1000/30 days. Decide:
   one migration file, or an explicit greenfield wipe. `RESET.md` says "bump the schema and wipe",
   but the wipe mechanism was deleted, so today it is neither.
2. **Decide where the store lives, in Phase 3 not Phase 4.** The iOS NSE cannot read the current one:
   the DB path is an app-private directory (not an App Group container) and the SQLCipher key has no
   `kSecAttrAccessGroup`, so an extension cannot open it. Because the inbox *is* the message store,
   this decides the table's location — and moving it later is a data migration on a kit that has no
   migration mechanism (see 1). Also write down the **single-drainer rule**: only one process may
   hold a gateway connection per device, or app and NSE both drain and both insert.
3. **pix's durable store and test suite** (`PLAN.md` Phase 3 — a prerequisite, not a deliverable).

**Migration order** — pix cannot compile against a kit whose old API is gone, and it has **one**
TypeScript surface for both platforms with both kits consumed from source (Gradle composite build,
local SPM). So "Kotlin ships first" would break iOS for the whole duration of the Swift port:

1. pix gains a durable store **and** its test suite;
2. **both** kits gain `inbox` + `send` alongside the existing ORM — Kotlin **designs** first and
   Swift ports the proven shape, but pix does not switch until both have landed;
3. pix switches to the new API;
4. the old surface is deleted, per kit.

Pin kit commits in pix CI for the duration, so step 4 cannot strand an older pix commit.

---

## 11. Still open

- `settings_sync` / `read_sync` classification (§4).
- Unknown-arm policy: inbox unparsed, or decline to ack (§4)?
- Does the app ever need to write to the inbox? Probably not — self-sync arrives through the normal
  receive path — but confirm before building.
- Template miss / first-run / null-name / locale behaviour for notifications (§7).
- Multi-device convergence and backup after §8.4 — whose problem, and in which phase?
- **`obscura-client-web` is a fourth wire-compatible client** with its own ModelSync ORM. It is
  non-normative, but nothing says "retire it", and if it is ever run against an account after pix
  switches it will emit tombstones and LWW writes pix no longer understands.
