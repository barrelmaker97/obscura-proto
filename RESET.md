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
| `TTLManager` | `story` declares `ttl: '24h'` in `schema.ts` and **nothing in `src/` ever references TTL or expiry**. Becomes an `expiresAt` field the app filters on. |
| Typed models (`TypedModel.wrap<T>`) | A Kotlin-only API. The app defines models as JSON through `defineModels`; it cannot reach this. |
| Legacy TEXT / IMAGE message path | Not on the bridge. The app never calls `send()`. |
| `sendText()` | Added by me this session to keep transport tests green. Exactly the "legacy breadcrumb" this reset exists to remove. Migrate those tests to the real send path. **(Verified gone from Kotlin `main` 2026-07-24.)** |
| `conversations` StateFlow, `messagesDomain` | Not on the bridge. The app builds conversation views from ORM entries in `ChatListScreen`/`ChatScreen`. |
| `ObscuraConfig.conversationModel` | `SPEC §0.4`: config that names an application concept is proof the boundary was already crossed. |
| `ProcessedCounts.pixCount` / `messageCount` (and `otherCount`) | Kit knowing app model names. Push counts by opaque model key; copy comes from templates the app registers. |
| `DatabaseMigrations` | Greenfield, no installed base, no external users. Bump the schema and wipe. (I wrote a correct version of this today. Correct-and-unnecessary is still a maintenance surface.) **(Verified gone from Kotlin `main` 2026-07-24.)** |
| `SessionSnapshot` / `saveSnapshot` / `loadSnapshot` | Unreferenced by production code; its `toMap()` writes `authToken` where the reader wants `token`; its KDoc urges adoption. Already removed this session. |
| Recovery phrase / BIP39 / encrypted backups / remote revocation | No bridge method exposes any of it. **Verify before deleting** — this may be intended product, not dead code. Flagged, not condemned. |
| `gatewayUrl` config field | Verified unused in kit source; already `@Deprecated`. |
| Binary-compat validator (`lib/api/lib.api`) as a *compatibility gate* | The kit has one consumer and no API-stability obligation (`SPEC §0.1`). **Keep the file, change its job** — see Guardrails. |

## Delete — `obscura-proto`

| Item | Why |
|---|---|
| `conformance/routing.json` + `SPEC §1` | A kit no longer resolves an audience, so it cannot misroute. Note these vectors pin a `recipient` audience for `pix` — but the app actually declares `conversation` for `pix` (`schema.ts`). The vectors were policing a configuration the app does not use. |
| `SPEC §2.1-2.3` (the CRDT prose) | See below: the app needs `APPEND` and `REPLACE`, not a CRDT. |
| `conformance/merge.json` | **DO NOT simply delete — it MIGRATES. See "The merge.json handover" below.** |
| `conformance/schema.json` + `SPEC §4` | Kits do not parse app schemas. Swift never adopted this vector, which is a fair signal of its value. |

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
- Device provisioning, linking, approval, revocation, takeover
- Transport: REST + gateway WebSocket, envelope ack, offline send queue (`network/`, 700 lines)
- Friend graph (needed to address devices *and* to resolve sender names — `SPEC §0.5`)
- Attachment encryption / upload / download
- The message store and the push-wake path
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
  **Establish what writes and verifies `signature` before deleting anything on that side**; if it is
  a real per-entry signature then iOS has an integrity check Android lacks, and the reset would
  delete the signer. Normalise at the bridge, not by migrating either table.

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
5. **Measure this phase in lines deleted.**
