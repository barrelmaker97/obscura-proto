# The thin kit API — proposal

**Status: proposal, for review. Not normative until merged into [`SPEC.md`](SPEC.md).**

[`PLAN.md`](PLAN.md) Phase 3 calls this "the one genuinely hard-to-reverse decision in the plan".
This document is that decision, written down before any code is deleted, so it can be argued with
cheaply.

Governed by [`SPEC.md` §0](SPEC.md). Where this document conflicts with §0, §0 wins and this document
is wrong.

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

Today the receive path is: **decrypt → persist → ack**, all inside the kit, because the kit owns the
store. The reset takes application data away from the kit. If the thin kit instead *hands the payload
to the app* (an event, a callback, a bridge emit) and then acks, the ordering becomes:

```
decrypt → emit to app → ACK (server DELETEs) → ...app writes to its store, maybe, later, if it is running
```

That is precisely the Phase 1 data-loss bug — acking something not durably persisted — reintroduced
across a process boundary, in both kits at once, on a path where the app may not even be running.
React Native's bridge is asynchronous and lossy under backpressure; the push path has no JS runtime
at all. An event-stream API cannot satisfy §0.9. **The kit must persist before it acks, therefore the
kit must have somewhere durable to put bytes it does not understand. That place is the inbox.**

Two consequences worth naming immediately:

- This closes the Swift MODEL_SYNC ack-before-persist hole (`PLAN.md` Phase 1 status) **by
  construction** — there is no path where the kit acks without its own durable write. Phase 3 must
  *verify* that, not assume it.
- The inbox is the kit's contribution to correctness. The app's contribution is being **idempotent**
  when it drains — which it already is, because both merge rules are idempotent (§6.2).

---

## 3. The inbox

### 3.1 Record

One row per successfully decrypted message. Every field is either kit-owned identity or a **declared
`client.proto` field** — nothing is parsed out of `payload`.

| Field | Source | Notes |
|---|---|---|
| `id` | kit | Monotonic per install. Drain order. Not a message id. |
| `receivedAt` | kit clock | When the kit persisted it, not a peer-supplied time. |
| `senderUserId` | `Envelope.sender_id` | Authenticated (§0.10). |
| `senderDeviceId` | decrypting session address | Cryptographic attribution, never the wire field (§0.10 rule 4). |
| `senderDisplayName` | **kit's friend graph** | Resolved locally, keyed on `senderUserId` (§0.5). Never from payload. `null` if not a friend. |
| `modelKey` | `ModelSync.model` | **Opaque.** The kit stores and echoes it; it MUST NOT branch on its value. |
| `entryId` | `ModelSync.id` | Opaque. Carried so the app can merge. |
| `op` | `ModelSync.op` | `CREATE` / `UPDATE` / `DELETE`, mapped per §3.1 of SPEC. |
| `sentAt` | `ModelSync.timestamp` | Peer-supplied. Clamped per §2.4 before storage. |
| `payload` | `ModelSync.data` | **Opaque bytes.** The kit never parses this. |

`senderDisplayName` is the one field that might look like a boundary crossing and is not: §0.5
*requires* the name to come from the local friend graph, and the friend graph is kit-owned (§0.3).
Resolving it here is what stops the app from ever being tempted by a payload-supplied name.

### 3.2 Lifecycle

```
   envelope
      │
      ▼
   decrypt ──────────► FAIL → no ack, no row. Message stays on the server. (§0.9 rules 1–2)
      │
      ▼
   persist inbox row ─► FAIL → no ack. Message stays on the server. (§0.9 rule 3)
      │
      ▼
   ACK  (server deletes its copy — the kit's row is now the only copy)
      │
      ▼
   notify app (droppable — the row is the delivery path, not the notification)
      │
      ▼
   app drains: peek(limit) → process → consume(ids)
      │
      ▼
   kit deletes the consumed rows
```

### 3.3 Normative rules

1. The kit MUST NOT ack until the inbox row is durably committed.
2. The kit MUST NOT delete an inbox row for any reason other than an explicit `consume(ids)` from the
   app. Not on reconnect, not on logout, not on a size cap, not on TTL. The row is the only copy.
3. `peek` MUST return rows in `id` order and MUST be side-effect free. Draining twice without
   `consume` returns the same rows — that is the crash-safety property, not a bug.
4. `consume(ids)` MUST be idempotent and MUST accept a subset. Partial progress is normal.
5. The in-process change notification MAY be dropped under backpressure. The row is the delivery
   path. This is §0.9 rule 4, unchanged.
6. `inboxDepth()` MUST be exposed. An inbox that grows without bound means the app has stopped
   draining, and that MUST be visible rather than silently absorbed.

### 3.4 What happens when the app is slow, or broken

Nothing is discarded. Depth grows, and the app is expected to surface it. **Deliberately no eviction
policy**: every eviction rule is a rule for silently losing a message the server has already deleted.
If a cap is ever added it must fail loudly — refuse to ack further messages, leaving them on the
server, where redelivery still works — never drop rows.

---

## 4. Send

```
send(recipientUserIds: [UserId],
     modelKey: String,            // opaque
     entryId: String,             // opaque
     op: Op,
     sentAt: Timestamp,
     payload: Bytes)              // opaque
  -> queued (durably) | error
```

- **The caller names the recipients** (§0.4). The kit fans out to every device of every listed
  userId, plus the author's own other devices. It makes no delivery decision of its own — no
  audience, no scope, no field sniffing.
- Returns when the submission is **durably queued**, not when delivered. The offline queue is
  kit-owned (§0.3).
- The kit MUST NOT inspect `payload` to decide anything — not routing, not counts, not retries.

---

## 5. The rest of the surface

Everything the app cannot do in TypeScript, and nothing else.

| Group | Calls |
|---|---|
| **Auth / session** | `register`, `login`, `loginAndProvision`, `logout`, `restoreSession` |
| **Connection** | `connect`, `disconnect`, `connectionState` |
| **Friends** | `befriend`, `acceptFriend`, `removeFriend`, `friends()` → `[{userId, displayName, status}]` |
| **Devices** | `ownDevices()`, `generateLinkCode`, `approveLink`, `revokeDevice`, `takeoverDevice` |
| **Inbox** | `peek(limit)`, `consume(ids)`, `inboxDepth()`, event: `inboxChanged` |
| **Send** | `send(...)` (§4) |
| **Attachments** | `uploadAttachment(bytes)` → id, `downloadAttachment(id)` → bytes |
| **Push** | `processPendingMessages(timeout)` → counts **by opaque model key**, `registerNotificationTemplates(map)` |

Two notes on **Push**:

- Counts are keyed by opaque `modelKey`. `ProcessedCounts.pixCount` / `.messageCount` are deleted —
  they are the kit knowing application model names (§0.4).
- Notification **copy** comes from templates the app registers (§0.4, last bullet). The kit composes
  `"{senderDisplayName}" + template[modelKey]` and nothing else. It never reads a payload to build
  notification text.
- `processPendingMessages` still returns zero counts when it genuinely cannot connect (`PLAN.md`
  F10). Making that distinguishable is a **Phase 4** decision, and this API is where it lands — most
  likely as a result type rather than bare counts.

---

## 6. What this means for `obscura-pix`

### 6.1 pix must gain a durable store — this is new work, not a move

**`obscura-pix` has no local persistence today.** No AsyncStorage, no MMKV, no SQLite: its entire
durable store is the kit's ORM, and its in-memory state is zustand rebuilt from `allEntries` on every
`entriesChanged` event. Deleting the ORM therefore does not *move* pix's storage — **it removes it**.

Phase 3 must give pix a real store before, or in the same change as, the deletion. This is the
largest single piece of work in the phase and it is not in `RESET.md`'s inventory, because the
inventory lists what comes *out* of the kits.

### 6.2 Merge moves to pix, and it is small

Per `RESET.md`, the five models need exactly two rules:

- **APPEND** — dedupe by `entryId`. (`directMessage`, `story`)
- **REPLACE** — higher `sentAt` wins; equal `sentAt` is idempotent. (`pix`, `profile`)

Both are **idempotent**, which is what makes redelivery from the inbox safe: if the app crashes
between `peek` and `consume`, reprocessing the same rows converges to the same state. The safety of
the drain protocol depends on this — if a future rule is ever *not* idempotent, the drain contract
breaks with it.

[`conformance/merge.json`](conformance/) is **ported into pix's test suite** rather than deleted: it
is the only executable statement of these semantics that exists, and the semantics are not going
away, only changing address.

### 6.3 Also pix's, now

Schema and validation; recipient resolution; queries, filtering, sorting; expiry (`expiresAt` the app
filters on — `story`'s `ttl: '24h'` becomes a field, and note **nothing in pix references expiry
today**, so this is a feature to build); notification copy; and display names — which must come from
`senderDisplayName` on the inbox row or from `friends()`, never from a payload (§0.5). `senderUsername`
has **18 references** in `pix/src` and every one of them needs rerouting.

---

## 7. What is deliberately absent, and why

Written down so it does not regrow. Each of these existed, in two languages, for one app.

| Absent | Why |
|---|---|
| `defineModels` / any schema | A kit does not act on application fields, so it does not parse an application schema (§0.4). |
| Query DSL, `orderBy`, `limit`, operators | Derived state is the app's (§0.4). Zero callers today. |
| `observe()` on entries, filtered observation | Same. The app owns its store and can observe it directly. |
| Relationships, `include()` | Never bridged; never used. |
| CRDT engine (GSet, LWWMap, tie-breaks) | Two idempotent rules, applied by the app (§6.2). |
| Audience / routing engine | The caller names recipients (§0.4). |
| `TTLManager` | An `expiresAt` field the app filters. |
| Typed models | A Kotlin-only API the app cannot reach. |
| `deleteEntry`, `queryEntries` | Zero callers. |
| Per-model config in kit settings | Config naming an application concept proves the boundary was already crossed (§0.4). |

**The test for adding anything here later:** can it be done in TypeScript, in the app? If yes, it does
not belong in the kit — no matter how convenient the bridge makes it.

---

## 8. Decisions taken (2026-07-25)

1. **Attachments are always by reference, never inline in `peek`.** Bridge transfers are copies, so a
   batch of image payloads would be paid for twice — once crossing the bridge, once in JS heap.
   `payload` carries an attachment **id**; the app calls `downloadAttachment(id)` when it actually
   needs the bytes. This also keeps `peek` cheap enough to call on every wake.
2. **The inbox IS the message store — one table, not two.** §0.3 says the kit owns "the message
   store" for the push path; that store is the inbox, and `consume` is what makes a row disappear. A
   separate long-lived history in the kit would reintroduce the kit holding application data, which
   is the thing this reset exists to remove. **Consequence to design against:** history lives in the
   app, so anything the app wants to show later it must persist on consume.
3. **Migration order** — pix cannot compile against a kit whose old API is gone:
   1. pix gains a durable store **and** its test suite (prerequisite — `PLAN.md` Phase 3);
   2. kits gain `inbox` + `send` **alongside** the existing ORM;
   3. pix switches to the new API;
   4. the old surface is deleted.

   Steps 2–4 are per-kit, and **Kotlin goes first**: Swift cannot be built or iterated on Linux
   (`PLAN.md` 0.4), so it pays a ~20-minute CI round trip per attempt and should port a shape already
   proven in Kotlin rather than discover it.

### Still open

4. **Does the app ever need to write to the inbox?** For self-sync, the sender's own other devices
   receive a copy through the normal receive path — so probably not. Confirm before building.
5. **`registerNotificationTemplates` shape.** Flat `{modelKey: "sent you a pix"}` for now; revisit in
   Phase 4, which is the only thing that will exercise pluralisation and localisation properly.
   Deliberately not over-designed ahead of its only consumer.
