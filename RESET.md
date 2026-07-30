# Kit reset — deletion inventory

**Status: proposal. Temporary document — delete it when the reset is done.**

**Not started as of 2026-07-25 — but now UNBLOCKED.** This is [`PLAN.md`](PLAN.md) Phase 3, and its
entry condition is **met**: Phase 2 acceptance was signed off on 2026-07-25 with CI evidence on both
kits. Nothing in the inventory below has been deleted yet — the ORM, CRDT engine, query DSL, schema
parser and routing engine are all still present in both kits, and
`conformance/{routing,merge,schema}.json` are all still shipped. Two items below have been overtaken
by Phase 0–2 findings and are corrected in place.

**Read the four gaps recorded at Phase 2 sign-off before deleting anything** (`PLAN.md`, Phase 2
status block). Two bear directly on this phase:

- **Swift acks `MODEL_SYNC` before durably persisting it** (`SPEC.md` §0.9 rule 3). The fix was
  deliberately deferred *to this phase*, because it lives in the ORM/CRDT engine being deleted here.
  Deleting that engine closes the hole by construction — **verify that it actually did**, rather than
  assuming the deletion was sufficient.
- **Swift cannot receive a `DEVICE_LINK_APPROVAL`** (no `case .deviceLinkApproval` in `routeMessage`).
  That is in device linking, which SPEC §0.3 says a kit **keeps** — so it is *not* on this list, will
  *not* be resolved by the reset, and needs its own decision.

The target architecture is [`SPEC.md` §0](SPEC.md). This file is the evidence for
getting there: what goes, what stays, and *why* — so no item rests on anyone's say-so.

## How "unused" was determined

The app can only reach the kit through the React Native bridge. Anything in the kit
with no path through `ObscuraBridgeModule.kt` is unreachable by construction; anything
exposed on the bridge but never called from `src/` is dead in practice.

- **Kit surface reachable from the app** — every `@ReactMethod` in
  `obscura-pix/android/app/src/main/java/com/obscuraapp/ObscuraBridgeModule.kt`.
- **What the app actually calls** — every `Obscura.<method>(` call site under `obscura-pix/src/`.
- **What the app's data actually is** — `obscura-pix/src/models/schema.ts` (all five models).

The headline number: **the entire ORM surface the app uses is four calls** —
`defineModels`, `createEntry`, `upsertEntry`, `allEntries`. Reads are
event → refetch-everything (`src/state/store.ts`). There are no queries, no deletes,
no relationships, and no reactive observation of entries.

---

## Delete — both kits

| Item | Evidence it is dead |
|---|---|
| Query DSL — `QueryBuilder`, 11 operators, `orderBy`, `limit` | `queryEntries` is exposed on the bridge and wrapped in `ObscuraModule.ts:122`, and has **zero callers** anywhere in `src/`. The app reads with `allEntries` and filters in zustand. |
| Relationships — `hasMany`, `belongsTo`, `include()` | Never bridged at all — no `@ReactMethod` exposes them. The Kotlin README already lists `include()` under "what doesn't work yet". |
| Deletes + tombstones (`SPEC §2.3`) | `deleteEntry` is exposed and wrapped (`ObscuraModule.ts:128`) with **zero callers**. The app never deletes an entry. The whole tombstone-ordering design is dead on arrival. |
| CRDT engine — `GSet`, `LWWMap`, deterministic `authorDeviceId` tie-break | Replaced by two rules declared per-message on the wire: `APPEND` (dedupe by `id`) and `REPLACE` (higher `timestamp` wins). See below for what the app's five models actually need. |
| Reactive observation of entries (`model.observe()`, filtered observation) | No `observeEntries` on the bridge. The app re-fetches `allEntries` on a `messageReceived`/`entriesChanged` event (`store.ts:205-214`). The Flow/ValueObservation layer is unreachable. |
| Schema engine — `ModelConfig`, `FieldTypes`, `fromWire`, field validation | `SPEC §0.4`: a kit does not act on application fields, so it does not parse an application schema. The schema stays in `schema.ts`. |
| Audience / routing engine — `Audience`, `SyncManager` targeting | `SPEC §0.4`: the caller names recipients. Resolution moves to TS, where it exists once. |
| `TTLManager` | `story` declares `ttl: '24h'` in `schema.ts` and **nothing in `src/` ever references TTL or expiry**. Becomes an `expiresAt` field the app filters on. **See the correction below — this is weaker than it reads.** |

> **Correction (2026-07-30): stories already never expire, and the receive path never expired them
> at all.** Deleting `TTLManager` removes nothing that works, but the row above implies a working
> feature is being traded for one to be rebuilt. It is not. Three findings from the readiness review:
>
> 1. TTL was only ever scheduled in `Model.create` — the **authoring** device. The receive path
>    (`GSet.merge` → `ModelStore.put`) hard-codes `ttl_expires_at = null`, so **a received story never
>    carried an expiry on Kotlin, ever.**
> 2. Since pix switched (§10 step 3), reads go through `EntryStore.all`, which does not filter on
>    expiry, and writes go through `EntryStore.put`, which writes `ttl_expires_at = null` via
>    `INSERT OR REPLACE` — so it also clears any TTL a pre-switch row happened to carry.
> 3. obscura-pix has zero references to expiry outside the schema literal; `StoriesScreen` sorts by
>    timestamp and never filters by age.
>
> So: **stories currently never expire, on either platform, for author or recipient.** Track
> `expiresAt` + a filter in `loadEntries` as a FEATURE TO BUILD, not a regression to avoid. Related:
> attachment blobs expire server-side at 30 days (`KIT_API.md` §5), so old stories will accumulate as
> broken media until this is built.
| Typed models (`TypedModel.wrap<T>`) | A Kotlin-only API. The app defines models as JSON through `defineModels`; it cannot reach this. |
| Legacy TEXT / IMAGE message path | Not on the bridge. The app never calls `send()`. |
| `sendText()` | Added by me this session to keep transport tests green. Exactly the "legacy breadcrumb" this reset exists to remove. Migrate those tests to the real send path. **(Verified gone from Kotlin `main` 2026-07-24.)** |
| `conversations` StateFlow, `messagesDomain` | Not on the bridge. The app builds conversation views from ORM entries in `ChatListScreen`/`ChatScreen`. |
| `ObscuraConfig.conversationModel` | `SPEC §0.4`: config that names an application concept is proof the boundary was already crossed. |
| `ProcessedCounts.pixCount` / `messageCount` (and `otherCount`) | Kit knowing app model names. Push counts by opaque model key; copy comes from templates the app registers. |
| `DatabaseMigrations` (the hand-rolled Kotlin class) | Greenfield, no installed base, no external users. (I wrote a correct version of this today. Correct-and-unnecessary is still a maintenance surface.) **(Verified gone from Kotlin `main` 2026-07-24.)** ⚠️ **This row is about one deleted class, not about migrations as such — do not read "bump the schema and wipe" as the current policy.** `KIT_API.md` P1 reversed that, and both kits now HAVE a migration mechanism: SQLDelight `.sqm` on Kotlin, `ObscuraSchema` + `DatabaseMigrator` on Swift. That is what makes this phase's table *deletions* expressible at all. *(Clarified 2026-07-26.)* |
| `SessionSnapshot` / `saveSnapshot` / `loadSnapshot` | Unreferenced by production code; its `toMap()` writes `authToken` where the reader wants `token`; its KDoc urges adoption. Already removed this session. |
| Recovery phrase / BIP39 / encrypted backups / remote revocation | No bridge method exposes any of it. ~~**Verify before deleting**~~ — **verified 2026-07-26: KEEP. This is intended product, not dead code.** The server side is fully built and E2E-encrypted (`KIT_API.md` §8.4), and the client half is gated behind `ObscuraConfig.enableRecoveryPhrase`, which defaults to `false` and which pix never sets — so "no bridge method" reflects a feature that is **off**, not one that is dead. Moved to the Keep list; see the `device_recovery_announce` row below. |
| `requestSync()` / `ClientSyncManager` sync-request path; `sendEncryptedAttachment` / `sendAttachmentReference` | *Added 2026-07-26.* The senders for the deleted `sync_request` and `content_reference` arms (`KIT_API.md` §4.2). Both are kit-public with **no caller** — not on the bridge, not used by the app. `sync_request`'s pull half is redundant with `pushHistoryToDevice` → `SYNC_BLOB`, which is handled and does fire on device link. **Does not touch the attachment bytes path** — `uploadAttachment` / `downloadAttachment` / `AttachmentCrypto` / the attachment cache all stay; pix's attachments ride inside a `model_sync` entry. |
| `gatewayUrl` config field | Verified unused in kit source; already `@Deprecated`. |
| Binary-compat validator (`lib/api/lib.api`) as a *compatibility gate* | The kit has one consumer and no API-stability obligation (`SPEC §0.1`). **Keep the file, change its job** — see Guardrails. |

## Delete — `obscura-proto`

| Item | Why |
|---|---|
| `conformance/routing.json` + `SPEC §1` | **DO NOT simply delete — five cases HAND OVER. See "The routing.json leak guards" below.** The original reasoning — "a kit no longer resolves an audience, so it cannot misroute" — is a non-sequitur: the risk *moves* to pix, it does not evaporate. (The observation that these vectors pin a `recipient` audience for `pix` while `schema.ts` declares `conversation` still stands, and is why only five of ten cases carry.) |
| `SPEC §2.1-2.3` (the CRDT prose) | See below: the app needs `APPEND` and `REPLACE`, not a CRDT. |
| `conformance/merge.json` | **DO NOT simply delete — it MIGRATES. See "The merge.json handover" below.** |
| `conformance/schema.json` + `SPEC §4` | Kits do not parse app schemas. Swift never adopted this vector, which is a fair signal of its value. |
| **Six `client.proto` payload arms** — `settings_sync` (41), `read_sync` (42), `history_chunk` (40), `sync_request` (47), `content_reference` (45), `chunked_content_reference` (46) | *Added 2026-07-26.* Decided in `KIT_API.md` §4.2/§4.3 from a sender/receiver sweep. `settings_sync`, `read_sync`, `history_chunk` and `chunked_content_reference` have **no implementation on either side, anywhere**. `sync_request` and `content_reference` are sent by both kits, received by neither, and called by nothing outside the kits. **`reserved` the field numbers** — do not recycle them. With `text` (below) this takes the wire from **18 arms to 11**. |

**Keep:** `conformance/wire.json` + `SPEC §3` — load-bearing forever. Two kits must encode
and decode identically. **Keep** `SPEC §2.4` (future-timestamp clamp): it applies to any
peer-supplied timestamp, and belongs on the `REPLACE` rule and the device-announce guard.

### The `merge.json` handover

**A vector file is a contract between implementations. It stops being one when there is only one
implementation left — at which point it is a test fixture, and it belongs with the code it tests.**

`merge.json` is mid-handover right now, so deleting it on the old schedule would break a live suite:

| | implementations of these semantics | what `merge.json` is | where it lives |
|---|---|---|---|
| **Today** | 3 — Kotlin ORM, Swift ORM, `obscura-pix` (`src/domain/merge.ts`, pix PR #56) | a **contract** | here; pix reads it through its `proto/` submodule |
| **After this phase** | 1 — pix alone | a **fixture** | move the file into `obscura-pix`, delete it here |

So the sequence is: **pix's copy of the tests must be reading a local fixture BEFORE this file is
deleted**, or the deletion breaks `obscura-pix`'s only test suite the day it lands. Concretely:

1. pix vendors the surviving cases into its own repo and switches
   `src/domain/__tests__/merge.vectors.test.ts` off the submodule path.
2. Only then does `conformance/merge.json` get deleted here, along with `SPEC §2.1-2.3`.

**Only four of the six cases carry over.** GSet union, GSet idempotence, LWW higher-timestamp, and
the LWW equal-timestamp tie-break — the last surviving only because `KIT_API.md` §8.2 decided to
keep a total order, keyed on the *authenticated* device id. The two tombstone cases retire with
`deleteEntry`. Say which are which when vendoring; a port that silently drops a third of a contract
is how a contract stops being one.

**Note the vectors are not sufficient on their own.** Mutation-testing pix's port found that
flipping APPEND from first-wins to last-wins passes every case in this file, because the idempotence
case uses a duplicate carrying identical data. pix supplements them (`merge.test.ts`); whatever
vendors these cases must carry that supplement too.

**Does `obscura-pix` keep the `proto/` submodule afterwards?** Yes — it still needs `SPEC.md` as the
normative contract for the kit boundary and the inbox, even once it holds its own merge fixtures.
What changes is that it stops depending on a *vector file* that no longer has a second implementation
to hold to account.

> **Sequencing note (2026-07-25).** pix reads the vectors through a **pinned submodule**, so
> deleting `merge.json` here breaks pix when someone next bumps the pointer — not "the day it
> lands", as this section previously said. The ordering requirement is unchanged and the urgency is
> lower; the pin is what buys the time, so do not treat it as slack that has already been spent.

### The `routing.json` leak guards

*(Added 2026-07-25, after review.)* The same argument as `merge.json`, reached from the opposite
direction. The original justification for deleting these vectors was "a kit no longer resolves an
audience, so it cannot misroute" — but **the risk moves to `obscura-pix`, it does not evaporate**.
After the reset, pix resolves every audience, and pix's entire test suite is 12 tests over
`merge.ts`. Deleting the vectors on that reasoning drops the guards exactly as the code they guard
arrives somewhere new and untested.

Of the ten cases, **five are fail-safe guards** and must be honoured by the app:

| Case | Why it must survive |
|---|---|
| LEAK GUARD: conversation with a malformed 3-party id must fail loud, never broadcast | The named failure mode. A 1:1 payload reaching everyone. |
| conversation audience with a missing id field must fail loud | Same class — an unresolvable audience must never widen. |
| recipient audience with a missing recipient field must fail loud | Same. |
| recipient audience with a blank recipient field must fail loud | Blank is not "everyone". |
| recipient audience naming a non-friend fails safe to **self only** (never broadcasts) | Fail *safe*, not merely loud. |

The other five pin resolution behaviour for `conversation` / `recipient` / `friends` / `self`
audiences, which is exactly the resolution logic being deleted from the kits — those retire with the
engine.

**`SPEC §1.2`'s fail-loud rule must be restated as an application-level rule before `SPEC §1` is
deleted**, or the normative statement disappears along with the vectors. Sequence as for
`merge.json`: **pix vendors the five guards and has them passing first; only then delete here.**

> This is not hypothetical. The typing-signal leak fixed on 2026-07-25 (see *Keep*) was precisely
> "1:1 payload, audience not resolved, broadcast to everyone" — the exact shape of the LEAK GUARD
> case — living on a code path `routing.json` never covered.

## Delete — `obscura-pix`

| Item | Evidence |
|---|---|
| The `settings` model | Declared in `schema.ts`; **never written and never read** anywhere in `src/`. Purely aspirational. |
| `senderUsername` fields (`directMessage`, `pix`, `story.authorUsername`) | Sender-supplied display names. `SPEC §0.5`: the name comes from the local friend graph, keyed on the authenticated envelope. Payload-supplied names let a peer choose their own on-screen label. |
| `queryEntries` / `deleteEntry` wrappers (`ObscuraModule.ts:122,128`) | Zero callers. |

## What the app's data actually needs

Derived from `schema.ts` and the write call-sites — this is the whole thing:

| Model | Written by | Merge rule actually required |
|---|---|---|
| `directMessage` | `createEntry` | `APPEND` — immutable, dedupe by id |
| `story` | `createEntry` | `APPEND` + `expiresAt` |
| `pix` | `createEntry`, then `upsertEntry` (recipient writes `viewedAt`) | `REPLACE` — higher timestamp wins |
| `profile` | `upsertEntry` | `REPLACE` |
| `settings` | *never written* | — (delete) |

Note `pix` is genuinely mutated by a **second user** (the recipient sets `viewedAt`),
so `REPLACE` is real and the clamp (`SPEC §2.4`) matters. But that is a timestamp
comparison, not a CRDT. It is also a viewed-**receipt** wearing a CRDT costume, and is
probably better modelled as an explicit receipt message.

## Keep — do not let the reset eat this

Roughly 70% of each kit, and the part whose tests pass:

- Signal protocol: sessions, identity, prekeys, encrypt/decrypt (`crypto/`, 2,944 lines in Kotlin)

> **Correction (2026-07-30): `MonotonicClock` is on this Keep list and should not be.** A
> deletion-readiness review found it has **zero non-ORM callers in either kit** — Kotlin `Model.kt`
> and `LWWMap.kt`, Swift `Model.swift` and `LWWMap.swift`, all of which are deletions. Entry
> timestamps are now stamped app-side (`obscura-pix/src/state/writeEntry.ts`, `nextSentAt`) and by
> `send(sentAt:)`'s default, and `nextSentAt` already provides the strictly-increasing property the
> clock existed for. Keeping it would preserve dead code inside a phase whose point is removing dead
> code. **Delete it with the ORM.**
>
> **`WireCodec` genuinely is a pure move** in both kits (Kotlin's `ModelOp` lives in the same file),
> with live non-ORM callers in `ObscuraClient` and `MessagingManager`.
>
> **`ModelSignal.swift` is NOT a pure move, and "relocate" under-describes it.** `extension
> TypedModel` and `extension Model` in that file reference ORM types, so it must be **split**: keep
> `SignalType`, `ModelSignalPayload`, `SignalStore`, `SignalObservation`, `SignalThrottle`,
> `SignalStoreRegistry`; delete the two extensions with the ORM. Kotlin's `SignalManager.kt` *is* a
> pure move (it imports only coroutines).
- Device provisioning, linking, approval, revocation, takeover
- Transport: REST + gateway WebSocket, envelope ack, offline send queue (`network/`, 700 lines)
- Friend graph (needed to address devices *and* to resolve sender names — `SPEC §0.5`)
- Attachment encryption / upload / download — **the bytes path.** The `content_reference` *message*
  is a deletion (above); `uploadAttachment` / `downloadAttachment` / `AttachmentCrypto` / the
  attachment cache are not. pix's attachments ride inside a `model_sync` entry, so deleting the arm
  does not touch a single line the app depends on. Do not conflate the two.
- The message store and the push-wake path
  > **Contradiction, flagged 2026-07-30:** the Delete list below has "`conversations` StateFlow,
  > `messagesDomain`", which is that store. Resolution: **`MessageDomain` goes.** obscura-pix reads
  > neither, and `SyncBlob`'s `messages` slot ships empty (`KIT_API.md` §8.4). What this Keep entry
  > is really protecting is the **push-wake path**, which does not depend on it.
- **Recovery: the `device_recovery_announce` arm, `RecoveryManager`, `RecoveryKeys`, `BackupCrypto`,
  `SyncBlob`.** *(Added 2026-07-26.)* The receive half is **unimplemented in both kits** — the arm
  falls through `routeMessage` and is acked — which reads like dead code and is not. The sender is
  gated behind `enableRecoveryPhrase = false` and pix ships no recovery UI, so nothing can currently
  emit it; the server side is fully built and E2E-encrypted (`KIT_API.md` §8.4). **Deliberately
  deferred, not deleted** (`KIT_API.md` §4.2). Two things go with that decision:
  > **The tests are false-green.** `RecoveryMessagingTests` exists in both kits and asserts only that
  > the wire message *arrives* — never that the recipient's friend graph or device list changed. It
  > passes identically with no handler, and there is no handler. Rename and annotate it; a green tick
  > over an unimplemented feature is the exact pattern the F-findings were about.
  >
  > **The handler has a trap waiting.** `device_recovery_announce` carries `recovery_public_key`
  > *inside the message whose signature it authenticates*, so the naive implementation verifies an
  > attacker's signature with the attacker's own key. Verify against the **stored** key; TOFU only on
  > the first. `handleDeviceAnnounce` shows the shape to avoid — its `if let` falls through, so it
  > **accepts an unsigned device list when no key is stored yet**.
- **Ephemeral signals — typing and read indicators.** *(Added 2026-07-25, after review.)* These were
  on **no** inventory, and they are **live**: `ObscuraBridgeModule.kt:528` calls
  `orm.modelOrNull("directMessage")?.typing(conversationId)`, with iOS equivalents at
  `ObscuraBridge.swift:447/468/474`. The "how unused was determined" method above counts *calls*, and
  it missed these because they are reached through the ORM object rather than through the four
  entry-store calls. `SignalManager.kt` / `ModelSignal.swift` therefore live inside `orm/` while
  being keep-forever code — **relocate them out of that package before the deletion**, and give
  `KIT_API.md` §6 a concrete signature rather than the one line it has now.
  > While confirming this, a live metadata leak was found and fixed in both kits: the signal send
  > path fanned every `MODEL_SIGNAL` out to `friends.getAccepted()` while `contextId` carried the
  > 1:1 conversation id, so every accepted friend learned in real time that you were typing to a
  > *named* third party. It was also a §0.4 violation — the kit resolved an audience nobody gave it.
  > Fixed by Kotlin PR #47 / Swift PR #15, which apply the two-participant rule the entry path
  > already used and **fail closed**. Three-party tests pin it; two-party tests cannot see it.
- **`WireCodec` and `MonotonicClock` — inside `orm/`, and keep-forever.**
  `WireCodec.decodeType` is on the main receive path (`ObscuraClient.kt:915`) and `wire.json` +
  `SPEC §3` are explicitly "load-bearing forever" (below). `MonotonicClock` stamps entry timestamps.
  Swift has the same layout at `Sources/ObscuraKit/ORM/WireCodec.swift`. **Move both out of the
  package before deleting anything**, or Guardrail 5 invites `rm -r orm/` and takes the wire codec
  with it.
- **The `ModelEntry` table itself — the engine dies, the table does not.** *(Added 2026-07-25.)*
  `ModelEntry.sq` already has exactly the columns the thin design needs
  (`model_name`, `entry_id`, `data`, `timestamp`, `author_device_id`), and `ModelStore.kt` writes
  `data` as plain application JSON with the merge metadata in columns beside it — so ownership moves
  across the bridge to pix with **no migration, no new schema and no data transform**. Keep
  `timestamp` and `author_device_id`: they cost nothing and are the only thing a future backup
  restore could be rebuilt from (`KIT_API.md` §8.1, §8.4). What *does* go with the engine:
  `ModelAssociation` entirely (relationships were never bridged), and `deleted` / `ttl_expires_at`,
  which die with tombstones and `TTLManager` and can be dropped in a later migration.
  On iOS the equivalent table is `model_entries`, which differs — `id` not `entry_id`, a
  `signature BLOB NOT NULL` Kotlin has no counterpart for, and TTL in a separate `ttl` table.
  Normalise at the bridge, not by migrating either table.
  > **`signature` is dead — resolved 2026-07-25, and it is not an integrity check Android lacks.**
  > `Model.swift:318` hashes `"\(name):\(id):\(timestamp):\(deviceId)"` with keyless SHA-256. It is
  > unkeyed, so anyone can compute it; it does **not cover `data`**, so it cannot detect tampering
  > with the entry; it is never verified anywhere; and `LWWMap.swift:121` fabricates an empty `Data()`
  > for tombstones. It is the same construct `SPEC §3.3` already removed from the wire. Delete it
  > with the engine.
  >
  > **The real problem it exposes is the column's `NOT NULL`, and Swift has no way to drop it.**
  > Every Swift store is `CREATE TABLE IF NOT EXISTS` — there is no migration mechanism at all — while
  > prerequisite **P1 gave migrations to Kotlin only**. Swift is the platform whose table shape
  > actually has to change. **Swift needs P1's equivalent before Phase 3 touches its store**; until
  > then the only options are "leave a dead NOT NULL column and keep writing a hash nobody reads" or
  > "wipe the database". With no real users the wipe is acceptable — but it must be a decision, not a
  > discovery.

For scale: `orm/` is **1,726 lines of 8,683** in the Kotlin kit. This is a deletion, not a rewrite.
(Re-measured on Kotlin `main` 2026-07-24, after Phases 1–2: **1,679 of 8,390**. The inventory below
is otherwise unchanged — `settings`, `senderUsername`, and the `queryEntries`/`deleteEntry` wrappers
are all still present in `obscura-pix`, which still has no test suite.)

## Real bugs that survive the reset — fix, don't just delete around

1. **`authorDeviceId` is a lie in both kits.** `senderDeviceId` is null over the wire, so
   `authorDeviceId = senderDeviceId ?: sourceUserId` silently yields a **userId**. Proven
   against a live server: Bob observed Alice's userId where her deviceId was expected.
   Suspected cause: `MessengerDomain.queueMessage` defaults `registrationId` to `1`, so the
   Signal session is keyed at `(userId, 1)` and the deviceId reverse-lookup misses.
   Swift has the identical defect (`ObscuraClient.swift:1894` passes `sourceUserId`;
   `ReceivedMessage.senderDeviceId` is hardcoded `nil` at `:1788`).
   **This is a security property being asserted falsely and must be fixed or dropped.**
   > **Update (2026-07-24): fixed in Kotlin by Phase 2, still open in Swift.** The envelope now
   > carries `sender_device_id`; Kotlin derives `authorDeviceId` from the address of the session
   > that decrypted, and `AuthorDeviceIdTests` asserts it against the sender's real device UUID.
   > The rule is normative in `SPEC.md` §0.10. Swift's fix is written but unmerged and unverified
   > (`swift/phase2-device-uuid`), so the false assertion is still shipping on Swift `main`.
2. **Swift has no schema migration mechanism at all** (every table is `CREATE TABLE IF NOT
   EXISTS`). Under greenfield rules this is fine — but it must be a *decision*, not an accident.
3. **Swift's `.friends` audience narrows a broadcast** when an entry happens to carry a
   `conversationId`/`recipientUsername` (`SyncManager.swift:187-193`). Deleted along with the
   routing engine — but note the routing vectors never caught it, because no `friends` case
   carried such a field.
4. ~~**The integration suite was red and nobody knew** — 6 failures against a real server. It
   needs a container the agent sandbox couldn't reach, so every agent reported success.~~
   > **Retracted (triage, 2026-07-14 — see `PLAN.md` 0.3).** This was wrong. Both named classes pass
   > (`ORMMessageTests` 6/6, `SignalECSTests` 9/9). Against a *default* local server the suite
   > suffers ~63 **environmental** failures — 57 × HTTP 429 (the auth limiter defaults to 1/s and the
   > suite fires faster) and 6 × HTTP 500 (`docker-compose.yml` declares `test-bucket` but never
   > creates it). Seed the bucket, raise the limit, and there are zero code failures. The real
   > problem was never red tests: it is a **coverage gap** on exactly the invariants the reset must
   > not break (the F1 send path, F2/F3 ack semantics, F4 `authorDeviceId`), plus a compose file that
   > cannot run the suite as shipped. Phases 0–2 closed part of that gap (`AckSemanticsTests`,
   > `AuthorDeviceIdTests`, `IdentityFromEnvelopeTests`, `TwoDeviceSendTests`); keep it closed
   > through the deletion.
5. **`obscura-pix` has no test suite at all.** CI runs `tsc`, `eslint`, and an Android release
   build. Compile breaks are caught; every semantic regression is not.

## Guardrails

1. **`SPEC.md` §0 is the brief.** Hand it to an agent *instead of* "improve this repo." That
   instruction presupposes the repo, and an agent scoped to one kit cannot see that the engine
   is unnecessary — the evidence lives in the app.
2. **Treat `lib/api/lib.api` as a budget, not a compatibility gate.** Every public symbol should
   have a caller in the app. Surface growth is a smell, not progress. The file is already
   generated; start reading it as a bill.
3. **An agent that cannot run the tests cannot say "done."** Make the dockerized server the
   default test path.
4. **A doc comment asserting a safety property must cite the test that proves it.** Every lying
   comment found in this audit would have been caught by that one review question.
5. **Measure this phase in lines deleted — but never by package.** *(Requalified 2026-07-25.)* The
   metric is right and the shortcut it invites is not: `orm/` contains `WireCodec`, `MonotonicClock`
   and `SignalManager`, all of which the kit keeps (see *Keep*). `rm -r orm/` deletes the wire codec
   the receive path calls and a shipped product feature. Move the keepers out first, then count.
6. **A capability with zero *entry-store* callers is not necessarily dead.** The method above
   counts `Obscura.<method>(` call sites, which is how typing indicators — reached through the ORM
   object, not through `createEntry`/`allEntries` — sat on no inventory while being live on both
   platforms. Before deleting a subsystem, grep the **bridge** for it as well as `src/`.
