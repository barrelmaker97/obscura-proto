# Kit reset — deletion inventory

**Status: proposal. Temporary document — delete it when the reset is done.**

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
| `sendText()` | Added by me this session to keep transport tests green. Exactly the "legacy breadcrumb" this reset exists to remove. Migrate those tests to the real send path. |
| `conversations` StateFlow, `messagesDomain` | Not on the bridge. The app builds conversation views from ORM entries in `ChatListScreen`/`ChatScreen`. |
| `ObscuraConfig.conversationModel` | `SPEC §0.4`: config that names an application concept is proof the boundary was already crossed. |
| `ProcessedCounts.pixCount` / `messageCount` (and `otherCount`) | Kit knowing app model names. Push counts by opaque model key; copy comes from templates the app registers. |
| `DatabaseMigrations` | Greenfield, no installed base, no external users. Bump the schema and wipe. (I wrote a correct version of this today. Correct-and-unnecessary is still a maintenance surface.) |
| `SessionSnapshot` / `saveSnapshot` / `loadSnapshot` | Unreferenced by production code; its `toMap()` writes `authToken` where the reader wants `token`; its KDoc urges adoption. Already removed this session. |
| Recovery phrase / BIP39 / encrypted backups / remote revocation | No bridge method exposes any of it. **Verify before deleting** — this may be intended product, not dead code. Flagged, not condemned. |
| `gatewayUrl` config field | Verified unused in kit source; already `@Deprecated`. |
| Binary-compat validator (`lib/api/lib.api`) as a *compatibility gate* | The kit has one consumer and no API-stability obligation (`SPEC §0.1`). **Keep the file, change its job** — see Guardrails. |

## Delete — `obscura-proto`

| Item | Why |
|---|---|
| `conformance/routing.json` + `SPEC §1` | A kit no longer resolves an audience, so it cannot misroute. Note these vectors pin a `recipient` audience for `pix` — but the app actually declares `conversation` for `pix` (`schema.ts`). The vectors were policing a configuration the app does not use. |
| `conformance/merge.json` + `SPEC §2.1-2.3` | See below: the app needs `APPEND` and `REPLACE`, not a CRDT. |
| `conformance/schema.json` + `SPEC §4` | Kits do not parse app schemas. Swift never adopted this vector, which is a fair signal of its value. |

**Keep:** `conformance/wire.json` + `SPEC §3` — load-bearing forever. Two kits must encode
and decode identically. **Keep** `SPEC §2.4` (future-timestamp clamp): it applies to any
peer-supplied timestamp, and belongs on the `REPLACE` rule and the device-announce guard.

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

For scale: `orm/` is **1,726 lines of 8,683** in the Kotlin kit. This is a deletion, not a rewrite.

## Real bugs that survive the reset — fix, don't just delete around

1. **`authorDeviceId` is a lie in both kits.** `senderDeviceId` is null over the wire, so
   `authorDeviceId = senderDeviceId ?: sourceUserId` silently yields a **userId**. Proven
   against a live server: Bob observed Alice's userId where her deviceId was expected.
   Suspected cause: `MessengerDomain.queueMessage` defaults `registrationId` to `1`, so the
   Signal session is keyed at `(userId, 1)` and the deviceId reverse-lookup misses.
   Swift has the identical defect (`ObscuraClient.swift:1894` passes `sourceUserId`;
   `ReceivedMessage.senderDeviceId` is hardcoded `nil` at `:1788`).
   **This is a security property being asserted falsely and must be fixed or dropped.**
2. **Swift has no schema migration mechanism at all** (every table is `CREATE TABLE IF NOT
   EXISTS`). Under greenfield rules this is fine — but it must be a *decision*, not an accident.
3. **Swift's `.friends` audience narrows a broadcast** when an entry happens to carry a
   `conversationId`/`recipientUsername` (`SyncManager.swift:187-193`). Deleted along with the
   routing engine — but note the routing vectors never caught it, because no `friends` case
   carried such a field.
4. **The integration suite was red and nobody knew** — 6 failures against a real server. It
   needs a container the agent sandbox couldn't reach, so every agent reported success.
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
