# Obscura client contract — SPEC

**Spec version: 1**

The prose companion to the `.proto` files and the `conformance/` vectors. The
protos pin the **shape** of the client-to-client contract; this document and
the vectors pin its **behavior**. Where a rule is testable, it is backed by a
conformance vector and that vector is the authority — this document explains
*why*.

Scope: the C2 (kit ↔ kit) contract — the E2E payload the server never sees.
Layers:

- **L1 transport** — `obscura/v1/obscura.proto`. Server + kits. Out of scope here.
- **L2 content** — `obscura/v2/client.proto`. The message shapes.
- **L3 semantics** — this document. What the content *means* and how kits act on it.

All three kits (ObscuraKit-Kotlin, ObscuraKit-swift, obscura-client-web) MUST
conform. "MUST" / "MUST NOT" are normative.

---

## 1. Routing (delivery targeting)

*Vectors: [`conformance/routing.json`](conformance/routing.json).*

Every model declares an **audience** in its schema config that determines which
recipients a write is delivered to. A kit MUST resolve the audience using only
the declared configuration — it MUST NOT hard-code application field names.

### 1.1 Audience modes

| `audience` | Meaning | Recipients |
|---|---|---|
| omitted / `{"kind":"friends"}` | Broadcast | The author's own devices + every accepted friend's devices. |
| `{"kind":"self"}` | Private | The author's own devices only. MUST never leave the account. |
| `{"kind":"recipient","field":"<f>"}` | 1:1 by username | Own devices + the devices of the single username in `data[f]`. |
| `{"kind":"conversation","field":"<f>"}` | 1:1 by conversation | Own devices + the devices of both participants encoded in `data[f]`. |

The author's own devices are **always** included, regardless of audience
(self-sync). `selfUserId` therefore always appears in a vector's
`expect.recipients`.

### 1.2 Fail-loud rule (confidentiality)

A misrouted 1:1 payload is a confidentiality breach. Therefore, for the
`recipient` and `conversation` audiences, if the recipient cannot be resolved
the kit MUST raise `DIRECT_ROUTING_UNRESOLVED` and send **nothing**. It MUST
NOT fall back to a broadcast. Specifically:

- `recipient`: the named field is **missing or blank** → raise. (A field that
  is present and non-blank but names a non-friend is *not* an error: it resolves
  to zero external recipients, so the write reaches own devices only — fail-safe,
  never a broadcast.)
- `conversation`: the named field does not resolve to **exactly two**
  participants (missing, blank, or not a canonical two-party value) → raise.

### 1.3 Canonical `conversationId`

A `conversation` audience value is the canonical two-party id: the two
participants' userIds sorted lexicographically and joined with a single
underscore, `"userIdA_userIdB"`. Splitting on `_` MUST yield exactly two
non-empty parts. This form makes a 1:1 conversation address the same regardless
of which participant composed the write, so a reply/receipt resolves in either
direction.

### 1.4 Error codes

Fail-loud outcomes are identified by a stable `code` string (not message text),
so vectors and cross-platform error handling can match on it.

| Code | Raised when |
|---|---|
| `DIRECT_ROUTING_UNRESOLVED` | A `recipient`/`conversation` audience cannot be resolved (see §1.2). |

---

## 2. Merge (CRDT resolution)

*Planned — Increment 2. Will define GSet union semantics, LWW timestamp
resolution and the tie-break rule for equal timestamps, tombstone
representation (`_deleted`), and the future-timestamp clamp. Backed by
`conformance/merge.json`.*

## 3. Wire (canonical encoding)

*Planned — Increment 3. Will define the canonical `ModelSync` encoding,
including how model `data` is serialized and whether/how it is canonicalized.
Backed by `conformance/wire.json`.*

---

## Changelog

- **v1** — Initial spec. §1 Routing (audience modes, fail-loud rule, canonical
  `conversationId`, `DIRECT_ROUTING_UNRESOLVED`), backed by `routing.json`.
  §2/§3 scaffolded.
