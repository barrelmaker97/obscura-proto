# History — what happened and why

`PLAN.md` (887 lines) and `RESET.md` (354 lines) were the working documents for Phases 1–3. Both
were **plans**, both are **done**, and by 2026-08-01 both had become actively misleading: their
status blocks still said the reset had not started, and `RESET.md` was still issuing instructions
(delete `SPEC §1`, `§2.1-2.3`, `§4`) that would have undone the settlement `SPEC.md` had just made.
They were deleted rather than patched again.

This file exists for one reason: **code and docs across four repos cite them**, and a citation that
lands nowhere is worse than no citation. What is preserved here is exactly what is still referenced —
the F-finding numbers and the shape of the reset. Everything else went, because it described work
that is finished.

The full text is in git: `git show bb9259c:PLAN.md` and `git show bb9259c:RESET.md`.

---

## The F-findings

A 2026-07-14 audit of the two kits against a live server. Numbered because ~56 comments in kit source
cite them by number when explaining why something is the way it is. Do not renumber.

| # | Finding | Outcome |
|---|---|---|
| **F1** | Multi-device sending is broken; the sender encrypts for an arbitrary device. Sessions were addressed by `registrationId`, and `decrypt` defaulted `senderRegId: 1`, so the two directions filed sessions at different addresses. | **Fixed, Phase 2.** Sessions key on the **device UUID** in both directions (`SPEC.md` §0.10). `registrationId` addresses nothing. Pinned by `TwoDeviceSendTests` in both kits. Reaching for `registrationId` to address a session is re-introducing F1. |
| **F2** | Kotlin acks messages it failed to decrypt; the server then deletes them. | **Fixed, Phase 1.** `SPEC.md` §0.9 rule 1. An ACK is a DELETE. |
| **F3** | Kotlin acks rate-limited senders unread. | **Fixed, Phase 1.** §0.9 rule 2 — skipped, not acked. |
| **F4** | `Envelope` carries no sender device, so `authorDeviceId = senderDeviceId ?: sourceUserId` silently yielded a **userId** in a field documented as a device id — a security property asserted falsely. | **Fixed, Phase 2.** `Envelope.sender_device_id` is stamped server-side from the device-scoped JWT, and `authorDeviceId` is derived from the address of the session that decrypted (§0.10 rule 4). Pinned by `AuthorDeviceIdTests`. |
| **F5** | Cold-start re-discovery re-fetches prekey bundles it doesn't need. | Downgraded at triage; not a defect. |
| **F6** | A friend's *new* device never receives your messages. | Addressed by device-UUID addressing + the own-device registry (F1, F9). |
| **F7** | Server hygiene (minor). | Server-side, out of kit scope. |
| **F8** | The push cannot wake an iOS Notification Service Extension. | Prerequisites recorded in `ObscuraKit-swift/docs/NSE_PREREQUISITES.md`. The NSE itself is not built. |
| **F9** | The own-device registry is never populated, so `DeviceAnnounce` is inert — which is why F1 was *latent* rather than constantly visible. | **Fixed, Phase 2.** Pinned by `TwoDeviceSendTests.testLinkApprovalPopulatesTheApproverRegistry`. |
| **F10** | The push-drain swallows a failed `connect()` and reports success — woken by a push, silently report no messages, leave them on the server. | **Fixed.** One short retry (250 ms) then an explicit log; the zero-count return is unchanged and making it distinguishable from "could not connect" is still open. |

## The reset (Phase 3)

Both kits had grown a schema-driven **ORM, CRDT engine, query DSL, audience-routing engine and
schema parser** — implemented *twice*, once per kit, to serve five flat models in one app that
reached almost none of it. It was deleted, not improved.

**Where the logic went.** Merge and audience resolution now live once, in `obscura-pix`
(`src/domain/merge.ts`, `src/domain/audience.ts`), which is also where the deleted conformance
vectors went: `routing.json`'s five leak guards are transcribed verbatim into
`src/domain/__tests__/audience.guards.test.ts`, and `merge.json` is vendored to
`src/domain/__fixtures__/merge.json`. `schema.json` had no successor — kits do not parse
application schemas (§0.4).

**What the kits kept.** Signal protocol, device provisioning/linking, transport, friend graph,
attachment bytes, ephemeral signals, and two storage surfaces: a durable inbox (`KIT_API.md` §3) and
a blind entry store (§8.1). `wire.json` is the only conformance vector left, because encoding is the
one thing two kits are *forced* to implement twice.

**The migration order, and why it was that order.** `obscura-pix` has one TypeScript surface for two
platforms, with both kits consumed from source. "Kotlin ships first" would therefore have broken iOS
for the whole duration of the Swift port. So: (1) pix takes ownership of the entry table, (2) *both*
kits gain `inbox` + `send` alongside the old engine, (3) pix switches, (4) the old surface is
deleted per kit. Deletion last. The kit commit was pinned in pix CI for the duration and unpinned
after step 4, which is what re-armed the cross-repo toolchain-drift check.

**What the ordering did not protect against.** Step 4's Swift half found that
`ObscuraSchema.swift`'s own recorded procedure for dropping the ORM tables — "remove them from `v1`
and let the erase-on-schema-change tripwire rebuild" — would have **erased the database**, because
`model_entries` is the app's entry store and `inbox_rows` shares the same `v1`, and after an ack an
inbox row is the only copy of a message anywhere. Ordering protects against stranding a consumer; it
does not protect against a wrong instruction written down at the time the plan was made. The
tripwire is now off and `v2` is a real data-preserving migration.

**The lesson worth keeping.** Every dangerous defect found during the reset was in the **ordering of
effects**, not in the deletions — a write acked before it was durable, an ORM throw inside the ack
gate that wedged the receiver permanently, an unvalidated `Envelope.id` collapsing every short id
onto one dedupe key, an audience taken from peer-supplied payload. Subtraction was the safe half.

## Known gaps carried forward

These were open when the planning documents were retired, and are not recorded anywhere else:

- **Nothing expires.** TTL went with the engine and has not been rebuilt, on either platform.
  Stories never expire, for author or recipient.
- **`FRIEND_SYNC` was deleted, not fixed.** `FriendSync` carries no `user_id`, so both kits keyed the
  synced friend on the sender's own id and wrote the user into their own friends list. A second
  device therefore does not learn about friends added later; `DEVICE_LINK_APPROVAL` still carries the
  friends export at link time. Building multi-device friend sync properly needs a proto field.
- **`ProcessedCounts` hard-codes `"pix"` and `"directMessage"`** in kit source, which §0.4 forbids.
  Both kits, identically. The fix changes a bridge-facing type on both platforms at once.
- **Swift sends `DEVICE_LINK_APPROVAL` but cannot receive one**, so a newly-linked device discards
  the p2p keypair, recovery key, friends export and approver device list. Kotlin routes it.
- **The six unimplemented payload arms** (`history_chunk`, `settings_sync`, `read_sync`,
  `content_reference`, `chunked_content_reference`, `sync_request`) still exist in `client.proto`.
  `content_reference` and `text` are **not** safe to remove as-is — see `KIT_API.md` §11.
