# PLAN — order of operations

**Status:** active. This is the execution plan for the reset. It supersedes ad-hoc task lists.

Companion documents:
- [`SPEC.md`](SPEC.md) — the normative contract. §0 defines the kit boundary.
- [`RESET.md`](RESET.md) — the deletion inventory (what comes out, with evidence per line).

This file answers *in what order*, and *how we know each step worked*.

---

## Why the reset is not first

`RESET.md` deletes the ORM, the CRDT engine, the query DSL, the schema parser and the
audience-routing engine from both kits. None of the defects below live in that code. They live in
`MessengerDomain` / `MessengerActor`, the envelope loop, the session store and the gateway — the
part of the kit that SPEC §0.3 says a kit **must** own, and that the reset **keeps**.

So fixing them first is not wasted work, and doing a ten-thousand-line deletion on top of a broken
crypto foundation means debugging a protocol bug through the diff.

Reset third. Correctness first.

---

## The disease

Both kits key their Signal sessions on **`registrationId`**.

`registrationId` is carried on exactly one wire surface: `PreKeyBundleResponse`, returned by
`GET /v1/users/{userId}` — which destructively consumes a one-time prekey per device on every call.

The **device UUID** is carried on every surface: `Submission.device_id`, `DeviceInfo.device_id`,
`PreKeyBundleResponse.deviceId`, and (once we add it) `Envelope.sender_device_id`.

They picked the identifier that doesn't travel. Everything below follows from that one choice.

### Why nobody noticed

**Every existing test is single-device.** In the one-device case "an arbitrary device" and "the only
device" are the same device, and `registrationId = 1` is a consistent-if-meaningless local store key
on both ends. The system is coherent *only* in the single-device case. Device linking is a shipped
feature.

---

## Findings

Ranked by severity. Evidence is cited so it can be re-verified rather than trusted.

### F1 — Multi-device sending is broken; the sender encrypts for an arbitrary device

`client.proto`'s `DeviceInfo` — the message `DeviceAnnounce` and `FriendSync` use to propagate a
friend's device list — **has no `registration_id` field**. So the mechanism designed to tell you
about a friend's devices structurally cannot carry the identifier the kit uses to address them.

Kotlin papers over it with a default:

```kotlin
// stores/FriendDomain.kt:34
data class FriendDeviceInfo(
    val deviceUuid: String,
    val deviceId: String,
    val deviceName: String,
    val registrationId: Int = 1,        // ← default
    val signalIdentityKey: ByteArray? = null
)
```

Every construction from the wire passes three arguments (`ObscuraClient.kt:948`, `:1041`, `:1067`),
so **every friend device in the store has `registrationId = 1`**, persisted, and read back the same
way (`parseDevices`: `obj.optInt("registrationId", 1)`).

`connect()` then calls `rebuildDeviceMap(friends.getAccepted())` (`ObscuraClient.kt:671`), writing
`deviceMap[deviceId] = Pair(userId, 1)` for every friend device — **overwriting any correct
registration IDs** that `parsePreKeyBundles` learned as a side effect. The map regresses on every
reconnect.

The send path then:

1. `registrationId = mapped?.second ?: 1` → **1** (`MessengerDomain.kt:66`)
2. `ensureSession(userId, 1)` → address `(userId, 1)`, no session → fetch bundles
3. `bundles.find { it.registrationId == 1 } ?: bundles.firstOrNull()` — real registration IDs are
   random 14-bit values, so the `find` essentially never matches and it takes **an arbitrary
   device's bundle**. (`get_all_bundles_for_user` runs `SELECT id FROM devices WHERE user_id = $1`
   with no `ORDER BY`.)
4. Session built at `(userId, 1)` with *that* device's identity and prekeys
5. `encrypt(userId, bytes, 1)` → ciphertext only that one device can read
6. `MessageSender.sendToAllDevices` submits **that same ciphertext to every one of the friend's
   devices**

One device decrypts. The rest receive ciphertext they can never read.

**Swift is broken differently:** `processServerBundle` correctly builds *outbound* sessions at
`(userId, realRegId)` per device, but `decrypt(...)` defaults to `senderRegId: 1`
(`MessengerActor.swift:121`) and the only call site (`ObscuraClient.swift:1772`) never overrides it.
Swift files outbound sessions under one address and inbound sessions under another. This is the most
likely explanation for the "session desync happens occasionally under load (investigating)" note in
Swift's own README.

Two kits, wire-identical, choosing **different local addresses for the same session, in both
directions**. The conformance vectors could never catch this: they pin the wire, and the wire is fine.

> **Status: reasoned from code, not yet observed.** F1 is the one finding in this document that has
> not been reproduced against a live server. Proving it is Phase 0's first task. If the two-device
> test passes, this section is wrong and the plan changes.

### F2 — Kotlin acks messages it failed to decrypt; the server then deletes them

An ACK is a DELETE. `AckBatcher` → `message_service.delete_batch` →
`DELETE FROM messages WHERE id = ANY($1) AND device_id = $2`. No tombstone, no redelivery.

`ObscuraClient.kt:830-868` puts the ack **outside** the try/catch:

```kotlin
try {
    val decrypted = messenger.decrypt(envelope)
    ...
} catch (e: Exception) {
    log("RECV FAIL decrypt ...")
    trackDecryptFailure(senderId)
    logger.decryptFailed(...)
}

try { gateway.ack(listOf(envelope.id)) } catch (...) { ... }   // ← runs on failure too
```

Decrypt throws → we log it → we ack anyway → the server deletes it. Unrecoverable. Combined with F1,
every message sent to a friend's second device is destroyed on arrival.

Also on this path: `incomingMessages.trySend(received)` and `_events.tryEmit(received)` are both
*droppable* — they return `false` on a full buffer and the return value is discarded — and the ack
fires afterward regardless.

**Swift acks only on the success path** (`ObscuraClient.swift:1809-1819`, inside the `do` block), so
it retains undecryptable messages. That is the safer half, but not correct either: `MessagePump`
holds a monotonic in-memory cursor for the life of a connection, so an unacked message is never
re-sent on that connection — it returns only on reconnect, and then forever, for the full 30-day
`ttl_days`, or until `delete_global_overflow` evicts the **oldest** messages at `max_inbox_size:
1000` (i.e. a pile of poison messages evicts the real backlog).

The two kits disagree on the single most safety-critical operation in the protocol. One destroys, one
hoards. Neither persists-then-acks.

**The correct rule was written down exactly once, in the README of the dead web PoC:**

> **Ack only after persistence**: Messages are acknowledged only after successfully persisting to
> local IndexedDB. If processing fails, the message stays queued on the server for retry.

It never made it into `SPEC.md`, and neither kit follows it.

### F3 — Kotlin acks rate-limited senders unread

`ObscuraClient.kt:825-828`. Ten decrypt failures from one sender inside 60s
(`MAX_DECRYPT_FAILURES = 10`) trips the limiter, and then every subsequent message from that sender
is **acked and deleted, unread**. A session desync with one friend silently eats that friend's entire
conversation. Swift's limiter `return`s without acking, which is correct.

### F4 — `Envelope` carries no sender device

```protobuf
message Envelope {
  bytes id = 1;
  bytes sender_id = 2;    // user UUID
  uint64 timestamp = 3;
  bytes message = 4;
}
```

The server **has** the sending device and explicitly discards it — `api/messages.rs:22`:

```rust
let _ = auth_user.device_id.ok_or_else(|| AppError::Forbidden("Device-scoped token required".into()))?;
```

It validates that a device-scoped token was used, binds the device to `_`, then passes only
`auth_user.user_id` to `message_service.send()`.

Signal sessions are pairwise **device-to-device**. A `SignalMessage` on the wire carries
`ratchet_key`, `counter`, `previous_counter`, `ciphertext` and a MAC — **no sender identity at all**
(only a `PreKeySignalMessage`, i.e. first contact, carries `registration_id`). So for every message
after the first, the recipient cannot determine which session to use from what is on the wire.

Kotlin therefore brute-forces a candidate list until one doesn't throw
(`MessengerDomain.kt:139-176`); Swift assumes device 1. This is also why `authorDeviceId` is a lie:
the reverse lookup through `deviceMap` returns null on a cold map and callers fall back to
`sourceUserId` — a *user* id in a field documented as a *device* id. **A security property asserted
falsely.**

### F5 — `GET /v1/users/{userId}` is destructive and is used as device discovery

`key_repo.rs:166-180` runs a `DELETE ... RETURNING` that consumes one one-time prekey from **every
device the user owns**, per call. The OpenAPI says so. Nothing on the client side acts like it knows.

`MessageSender.sendToAllDevices` calls it whenever its in-memory `deviceMap` has no entry for a user,
and `parsePreKeyBundles` populates `deviceMap` as a *side effect* — so a prekey-consuming endpoint is
the kits' device-enumeration mechanism. When prekeys run out, `fetch_pre_key_bundle` returns
`one_time_pre_key: None` and sessions silently fall back to signed-prekey-only: **weaker forward
secrecy, no error**.

There is no non-destructive alternative. `GET /v1/devices` is self-only, enforced in SQL
(`device_repo.rs:44,81` — every query carries `AND user_id = $2` from the JWT).

### F6 — A friend's *new* device never receives your messages

`MessageSender.kt:17-21` refreshes the device map only `if (deviceIds.isEmpty())`. Know one of
Alice's devices, and when she links a second you keep fanning out to the stale list. `DeviceAnnounce`
is supposed to cover this, but it is a client-to-client broadcast that can simply be missed, with no
reconciliation against the server — which is the actual source of truth for device lists.

### F7 — Server hygiene (minor)

- **Idempotency cache is not sender-scoped.** `api/messages.rs:31` keys the response cache on the
  bare `Idempotency-Key`, short-circuiting *before* the per-sender `ON CONFLICT (sender_id,
  submission_id)` guard. Unreachable in practice (UUIDs; Kotlin derives the key from a content hash
  over a payload containing a random `submission_id` and ratcheted ciphertext) but the cache is the
  weaker layer and it is cheap to scope.
- **`key_service.rs::verify_keys` still accepts the dead web PoC's signature form.** It tries both
  the 33-byte and 32-byte key forms with a comment naming `libsignal-protocol-typescript`. That
  client no longer exists. The server accepts a signature form neither shipping kit produces.

### F8 — The push cannot wake an iOS Notification Service Extension

`fcm.rs:301-319` sends a **background** push: `push_type: "background"`, `content-available: 1`,
`priority: "5"`, `collapse_id: "obscura_check"`, and **no `mutable-content: 1`, no `alert`**.

Apple does not guarantee delivery of background pushes: they are rate-limited, delivered at the
system's discretion, and **not delivered at all after the user force-quits**. They cannot launch an
NSE. The collapse key means ten messages coalesce into one wakeup.

The Android payload (data-only, high priority, collapsed) is correct and should not change.

**This matters beyond the feature.** SPEC §0.1 justifies the *existence of native kits* on the
grounds that "the push path must decrypt with the app closed — on iOS, in a Notification Service
Extension." The server has never sent a push that launches one. The justification is currently
fiction.

**Privacy constraint (non-negotiable):** no plaintext and no ciphertext may ever appear in a push
payload. Nothing derived from message content reaches Google or Apple. The fix is to change the push
*type*, not its *contents*: an alert push carrying a **placeholder** body plus `mutable-content: 1`,
which launches the NSE, which connects, decrypts locally, persists, and *rewrites* the notification
from plaintext it decrypted on-device. Apple sees "New message" and nothing else.

---

## Phases

Each phase has an acceptance criterion. We stop between phases and verify before proceeding.

### Phase 0 — Make the truth observable

Nothing else is safe until this exists, and it is also the safety net for the reset.

| # | Task | Repo |
|---|---|---|
| 0.1 | **Two-device integration test.** Alice with two linked devices; Bob sends; assert *both* decrypt. | ObscuraKit-Kotlin |
| 0.2 | **Ack-semantics test.** Feed an undecryptable envelope; assert the server still holds it afterward. | ObscuraKit-Kotlin |
| 0.3 | **Get the integration suite green.** 6 failures at baseline (4 × `ORMMessageTests`, 2 × `SignalECSTests`), unnoticed because the Copilot sandbox had no server. | ObscuraKit-Kotlin |
| 0.4 | **Swift verification story.** Swift cannot be built or run on the current Linux box (`docs/PITFALLS.md`: Linux unsupported; vendored libsignal absent). Every Swift claim in this document is code-inspection only. | ObscuraKit-swift / CI |

**Acceptance:** 0.1 **fails** (proving F1), 0.2 **fails on Kotlin** (proving F2), and the rest of the
suite is green. A red suite cannot protect a reset.

> If 0.1 *passes*, F1 is wrong. Stop and re-plan.

### Phase 1 — Stop the data loss

Client-only. No proto change, no server change. Small, safe, independently shippable.

- Persist-then-ack, **identically** in both kits.
- Never ack a decrypt failure. Never ack a rate-limited skip. Never ack before the message is
  durably written.
- Replace the droppable `trySend` / `tryEmit` on the receive path with something that cannot
  silently discard a message that is about to be acked.
- Write the rule into `SPEC.md`: **an ACK is a DELETE; ack only what you have durably persisted.**

**Acceptance:** 0.2 goes green on both kits. No behavioral difference between them on the receive path.

### Phase 2 — One identifier, everywhere

The coordinated proto + server + both-kits change. Cures F1, F4, F5, F6 and the `authorDeviceId` lie
together, because they are one disease.

- `Envelope.sender_device_id` (16-byte device UUID) in `obscura/v1/obscura.proto`; server stops
  discarding `auth_user.device_id`.
- Both kits key `ProtocolAddress` on the **device UUID**. `libsignal`'s `ProtocolAddress` is a
  purely *local* store key — it is never transmitted — and its `name` slot is a `String`, so a UUID
  fits. `registrationId` stops being an addressing identifier entirely.
- `ensureSession` selects the bundle by device UUID (already present in `PreKeyBundleResponse`).
  Delete the `firstOrNull()` fallback, the candidate-guessing loop, `FriendDeviceInfo.registrationId`,
  and Swift's `senderRegId: 1` default.
- `authorDeviceId` is derived from **the address of the session that decrypted** — never from a wire
  field. A valid MAC proves possession of that session's chain key, which only that device has. A
  malicious server that lies about `sender_device_id` can then cause a decryption failure but never a
  forged attribution. If the two ever disagree, log it as a security event.

**Acceptance:** 0.1 goes green. `authorDeviceId` returns a device id, verified against the sender's
actual device.

> **Do not import Signal-Server's schema to satisfy libsignal — libsignal does not ask for it.**
> obscura-server is a protocol implementation, not a Signal-Server clone: device UUIDs (not small
> ints), per-device identity keys (not per-account), a client-side friend graph, no ACI/PNI, no
> sealed sender, no groups. This rule is why Phase 2 needs no device-numbering scheme. It belongs in
> SPEC §0.

**Open decision — blocks Phase 2.** F5 has no non-destructive alternative today. Recommendation: add
`GET /v1/users/{userId}/devices` returning device UUIDs and identity keys with **no key material**.
It separates "who are your devices" from "give me key material" — the actual conflation — and makes
the server the source of truth for device lists instead of `DeviceAnnounce`, a peer broadcast that
can be missed (F6). It leaks nothing the prekey endpoint does not already leak. **Needs Nolan's call.**

### Phase 3 — The reset

Now execute `RESET.md`, on a foundation that is correct and has tests.

- Delete the ORM, CRDT engine, query DSL, schema parser and audience-routing engine from both kits.
- **Define the thin kit's API before coding it.** This is the one genuinely hard-to-reverse decision
  in the plan.
- Move the real merge logic into pix TypeScript. It is small: append-with-dedupe, LWW-by-timestamp,
  TTL. `pix.viewedAt` is a viewed-receipt wearing a CRDT costume.
- **pix needs a test suite.** It has none; CI runs `tsc`, `eslint` and an Android release build —
  compile breaks are caught, every semantic regression is not. The reset moves the domain *into* pix,
  so pix becomes where correctness lives.

**Acceptance:** pix builds and runs on both platforms against the thin kits, with tests, and the
deleted surface has no callers.

### Phase 4 — Push and the NSE

- Server `apns` block: alert push, `mutable-content: 1`, no collapse, priority 10. Payload carries
  **nothing** — placeholder body only. Android's data-only payload is unchanged.
- Build the iOS Notification Service Extension: connect, decrypt, persist, ack, rewrite the
  notification from on-device plaintext.

Last, because it depends on Phase 1 (an NSE that acks before persisting destroys messages from a
background process — a far worse place to have that bug) and Phase 3 (the NSE writes to the message
store, whose shape the reset changes).

**Note the NSE's constraint:** there is no REST message fetch. `POST /v1/messages` is send-only;
delivery is exclusively the WebSocket gateway. So an NSE must mint a ticket, open a WebSocket,
receive an `EnvelopeBatch`, decrypt, persist and ack — inside iOS's ~24 MB / 30 s extension budget,
and the ack deletes server-side, so a persist failure after an ack loses the message permanently.
A `GET /v1/messages` may be worth adding for this. Decide in Phase 4.

**Acceptance:** SPEC §0.1 becomes true — a push arrives with the app force-quit, the NSE decrypts it,
and the notification shows who it is from and what kind of message it is. Nothing about the message
ever reaches Apple.

---

## For agents working on this

Read [`SPEC.md`](SPEC.md) §0 first. Then the findings above that touch your phase.

The reason this document exists at all: **an agent scoped to one repo cannot see any of this.** The
evidence for F1 is split across `client.proto` (a missing field), Kotlin (`FriendDomain.kt`, a
default), and the server (`key_repo.rs`, a missing `ORDER BY`). No single-repo agent could find it,
and one previously "improved" the Kotlin kit by hardening the very machinery that is now on the
deletion list. If a task seems to require inventing an identifier, a field, or a fallback — stop.
That is how this happened the first time.
