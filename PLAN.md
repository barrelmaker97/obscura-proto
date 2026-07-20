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

> **Status (2026-07-14): CONFIRMED against a live server — but LATENT, masked by F9.** The
> `F1 mechanism probe` in `TwoDeviceSendTests` reproduces it exactly: with a friend's device list
> populated, `rebuildDeviceMap` rewrites both devices to `registrationId = 1`, both are encrypted
> under one `(aliceId, 1)` session, and device 2 fails to decrypt. The mechanism is real.
>
> **It cannot fire in any current end-to-end flow, because of a second bug (F9): the own-device
> registry is never populated, so every `DeviceAnnounce` is inert and a friend's device list in the
> store is *always empty*.** With an empty list, `rebuildDeviceMap` has nothing to clobber, so the
> `deviceMap` keeps the real per-device registration IDs the prekey fetch learned — and multi-device
> delivery *works today*. (This corrects an earlier claim that multi-device is actively broken: it is
> not. F1 is a landmine, not an active fire.) The probe had to inject a populated `DeviceAnnounce`
> through Alice's genuine encryption path — exactly what a *fixed* propagation path would emit — to
> detonate it. See F9 and the Phase 2 sequencing constraint.

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

### F5 — Cold-start re-discovery re-fetches prekey bundles it doesn't need *(downgraded)*

*(Severity revised down 2026-07-16 after review. No new endpoint. Left here because the cleanup lands
naturally in Phase 2.)*

`key_repo.rs:166-180` runs a `DELETE ... RETURNING` that consumes one one-time prekey from **every
device the user owns**, per call. Fetching a bundle on **first contact** is legitimate and by design —
you need a session and the prekey is consumed to build it. The clients also honor the replenishment
mechanism on both paths: `startPreKeyStatusListener` acts on the server's `PreKeyStatus` frames, and
`checkAndReplenishPreKeys` self-checks after every received message (`ObscuraClient.kt:781-799`). So
routine consumption is self-correcting for any device that comes online periodically.

The real defect is narrower: `deviceMap` is **in-memory**, so a cold start re-fetches bundles purely
to repopulate it (`MessageSender.sendToAllDevices` when `deviceMap` is empty), consuming a prekey per
device even when a **persisted session already exists** and no prekey is needed. That is a client-side
inefficiency — persist the device map, or rebuild it from persisted sessions and the friend device
list — **not a server change**.

Phase 2 largely dissolves it: once addresses key on the device UUID and F9 is fixed, ongoing device
discovery moves to the reliably-queued `DeviceAnnounce` / friend-handshake path, and the prekey fetch
returns to first-contact-only. Residual risk: a device offline long enough to be drained to zero falls
back to signed-prekey-only for *new* sessions — rare, and a well-understood Signal degradation, not a
break. `GET /v1/devices` is self-only (`device_repo.rs:44,81`), but that no longer matters because we
are not adding a cross-user device-list endpoint.

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

### F9 — The own-device registry is never populated, so `DeviceAnnounce` is inert

*(Discovered 2026-07-14 while proving F1. Not in the original audit.)*

`addOwnDevice` (`DeviceDomain.kt:58`) has **no callers**. `register` / `loginAndProvision`
(`AuthManager`) never record the local device; `approveLink` ships the *approver's*
`getOwnDevices()` — which is empty — as the approval's `ownDevices`, so the approvee's
`setOwnDevices([])` (`ObscuraClient.kt:1070`) also writes empty. Instrumented:
`alice1.getOwnDevices() = 0`, `alice2.getOwnDevices() = 0`.

Consequence: every `DeviceAnnounce` a client broadcasts carries an **empty** device list, so a
friend's device list in the store is **always empty**, so `DeviceAnnounce` propagates nothing. This
is a superset of F6: not only does a sender fail to learn a friend's *new* device, it never learns
any friend's device list through the linking/announce path at all. Multi-device sends work today only
because `sendToAllDevices` sources its targets from the `deviceMap` that the prekey fetch populates
(F5) — a different, destructive path.

**F9 is why F1 is currently latent.** An empty friend device list gives `rebuildDeviceMap` nothing to
clobber. Fix F9 alone and F1 detonates. See the Phase 2 constraint.

---

## Phases

Each phase has an acceptance criterion. We stop between phases and verify before proceeding.

### Phase 0 — Make the truth observable

Nothing else is safe until this exists, and it is also the safety net for the reset.

| # | Task | Repo |
|---|---|---|
| 0.1 | **Two-device integration test** that forces the **sender-reconnect-after-friendship** sequence (see below). Alice with two devices; Bob befriends her, **reconnects**, then sends; assert *both* of Alice's devices decrypt. | ObscuraKit-Kotlin |
| 0.2 | **Ack-semantics test.** Feed an undecryptable envelope; reconnect; assert the server still holds it. | ObscuraKit-Kotlin |
| 0.3 | **Fix the test *environment*, not the suite.** The "6 baseline failures" story is fiction (see below). Seed the MinIO bucket and raise the auth rate limit in `docker-compose.yml` so the suite is runnable in one pass. | ObscuraKit-Kotlin / obscura-server |
| 0.4 | **Swift verification story.** libsignal v0.40.0's FFI **does** build on this Linux box (NDK libclang + libxml2 shim); the full kit does **not** — GRDB/SQLCipher needs Apple-only CommonCrypto. See below. | ObscuraKit-swift / CI |

**Acceptance:** 0.1 **fails** (proving F1), 0.2 **fails on Kotlin** (proving F2). The rest of the
suite is already green against a *correctly configured* server — that is not the bar. The bar is that
0.1 and 0.2 exist and force the paths nothing currently tests.

> **0.3 correction (triage, 2026-07-14).** The premise "6 failures (4 × `ORMMessageTests`, 2 ×
> `SignalECSTests`), unnoticed because the sandbox had no server" is **wrong**. Both classes pass
> (6/6, 9/9). Against a *default* local server the suite instead suffers ~63 environmental failures:
> 57 × HTTP 429 (server auth limiter defaults to 1/s; the suite fires faster) and 6 × HTTP 500
> (`docker-compose.yml` declares `test-bucket` but never creates it). Seed the bucket and raise the
> limit and there are **zero code failures**. So RESET.md item 4 ("the suite was red and nobody
> knew") is also fiction and should be corrected. The real problem is not red — it is a **coverage
> gap** on exactly the invariants the reset must not break (F1's send path, F2/F3 ack semantics, F4
> `authorDeviceId`), plus a compose file that can't run the suite as shipped.

> **0.1 is a trap.** The repo already has *passing* two-device tests (`MultiDeviceFanOutTests`,
> `DeviceLinkFlowTests`). They pass because the **sender never reconnects after the multi-device
> friend lands in its accepted store**, so its `deviceMap` is empty at send time and
> `sendToAllDevices` runs the corrective per-device `fetchPreKeyBundles`. F1 only bites *after* a
> sender reconnect: `connect()` → `rebuildDeviceMap` poisons every friend device to `registrationId
> = 1`, and the now-non-empty map makes `sendToAllDevices` **skip** the corrective fetch
> (`MessageSender.kt:17`), so both devices are encrypted under one `(userId, 1)` session. **A 0.1
> test that does not reconnect the sender will pass and give false confidence.** If 0.1 passes *with*
> the reconnect, F1 is wrong — stop and re-plan.

> **0.4 finding (2026-07-14): Swift builds *partway* on Linux.** libsignal v0.40.0's Rust FFI builds
> on this Linux host — two environmental fixes, neither touching Signal's code: point `LIBCLANG_PATH`
> at the Android NDK's LLVM-18 libclang (clang 21's bindgen mis-parses vendored BoringSSL, failing on
> `GENERAL_NAME_new`), and put a `libxml2.so.2` shim on `LD_LIBRARY_PATH`. The **full kit does not
> build**: GRDB's bundled SQLCipher needs `CommonCrypto/CommonCrypto.h`, which is Apple-only — it is
> the at-rest cipher for the message store. `PITFALLS.md:21` blames the Linux drop on
> `URLSessionWebSocketTask`; the build actually walls at SQLCipher first, so the doc names the wrong
> cause. The Signal session store is GRDB-backed and the module is monolithic, so no crypto-only
> slice compiles. **Consequence:** the Swift session-addressing claims (`decrypt` defaults
> `senderRegId: 1`; outbound sessions at `(userId, realRegId)` → inbound/outbound addresses diverge)
> remain code-inspection only. The protocol *mechanism* can be proven with the libsignal that now
> builds (encrypt at one address, decrypt at another, observe failure); full end-to-end Swift
> verification needs macOS CI, or an OpenSSL-backend SQLCipher fork (real work, and a non-shipping
> build). **Decided (2026-07-16):** prove the addressing *mechanism* now with a libsignal-level test
> (no GRDB); rely on macOS CI for full end-to-end Swift verification later. No SQLCipher fork.

### Phase 1 — Stop the data loss

Client-only. No proto change, no server change. Small, safe, independently shippable.

- Persist-then-ack, **identically** in both kits.
- Never ack a decrypt failure. Never ack a rate-limited skip. Never ack before the message is
  durably written.
- Replace the droppable `trySend` / `tryEmit` on the receive path with something that cannot
  silently discard a message that is about to be acked.
- Write the rule into `SPEC.md`: **an ACK is a DELETE; ack only what you have durably persisted.**

**Acceptance:** 0.2 goes green on both kits. No behavioral difference between them on the receive path.

> **Status (2026-07-16).**
> - **Kotlin — DONE and verified.** `fix/phase1-persist-then-ack`. The loop had two acks (rate-limit
>   path + an unconditional ack outside the try/catch); now one ack, reached only after decrypt +
>   durable persist. `AckSemanticsTests` GREEN (was RED); `CoreFlowTests` and the two-device happy
>   path GREEN; the F1 probe still FAILS (F1 untouched). SPEC §0.9 codifies the rule.
> - **Swift — mostly already correct; one residual gap deferred to macOS.** F2/F3 were **Kotlin-only**:
>   Swift already acks *inside* the `do` block (a decrypt failure skips it) and its rate-limit path
>   already `return`s without acking, so it satisfies the primary invariant. The one gap: `routeMessage`
>   is non-throwing and its persistence calls (`await messages.add`, `await friends.add`) swallow
>   errors, so a persist failure *after* a good decrypt still acks — a latent violation of §0.9 rule 3.
>   Closing it means making the persistence path throwing (`routeMessage` → `async throws`, `try await`
>   at `ObscuraClient.swift:1779`). That is a signature change across the persistence actors and
>   **cannot be compiled or tested on the Linux box** (GRDB/SQLCipher needs CommonCrypto), so it is
>   **not** being done blind. Deferred to a macOS session / CI, where it must be compiled and a
>   persist-failure test added. Until then, Swift is safe against the *decrypt-failure* data loss;
>   only the rarer persist-failure path is unguarded.

### Phase 2 — One identifier, everywhere

The coordinated proto + server + both-kits change. Cures F1, F4, F5, F6, F9 and the `authorDeviceId`
lie together, because they are one disease.

> **Hard sequencing constraint (from proving F1).** F1 is latent only because F9 keeps friend device
> lists empty. **Fixing device-list propagation (F9/F6) and the `registrationId` addressing (F1)
> must land in the same change.** Ship the propagation fix first and multi-device delivery breaks the
> instant a friend's real device list reaches a peer — `rebuildDeviceMap` stamps every device
> `registrationId = 1` and one ciphertext gets fanned to all of them. `TwoDeviceSendTests`'
> `F1 mechanism probe` is the guard: it must be green before either half is considered done.

- `Envelope.sender_device_id` (16-byte device UUID) in `obscura/v1/obscura.proto`; server stops
  discarding `auth_user.device_id`.
- Populate the own-device registry (fix F9): `register` / `loginAndProvision` record the local
  device; `approveLink` ships the real device list. Once addresses key on the device UUID, the
  registration-id-less `DeviceInfo` is no longer a problem — propagation carries the identifier the
  address actually uses.
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

**Resolved (2026-07-16): no new endpoint.** We considered a non-destructive
`GET /v1/users/{userId}/devices`; decided against it. A prekey fetch on first contact is the designed,
legitimate way to learn a peer's devices and establish a session, and the clients honor prekey
replenishment, so consumption is self-correcting. Ongoing device discovery moves to the
`DeviceAnnounce` / friend-handshake path — which is reliably queued like any message *once Phase 1's
persist-then-ack lands* (until then, F2 can drop an announce). The cold-start re-fetch waste (F5) is
fixed client-side by persisting/rebuilding the device map. The server stays dumb; no cross-user
device-list endpoint. **Device-discovery source for Phase 2: the announce/handshake path, not the
server.**

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
