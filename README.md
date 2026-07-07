# obscura-proto

The shared contract for Obscura: the Protocol Buffer schemas plus the
language-neutral conformance vectors that every Obscura component builds
against. This repo is the single source of truth, consumed as a git submodule
by the server and every client kit.

## Layers

Obscura's wire contract has three layers. This repo owns the two schema layers
and pins the third (behavior) with prose + vectors.

| Layer | Artifact | Who speaks it |
|---|---|---|
| **L1 — transport** | [`obscura/v1/obscura.proto`](obscura/v1/obscura.proto) | server ⇄ kits: the envelope the server routes. Its encrypted `content` is opaque to the server. |
| **L2 — client content** | [`obscura/client/v1/client.proto`](obscura/client/v1/client.proto) | kit ⇄ kit: the E2E payload *inside* `content`. The server never parses it. |
| **L3 — semantics** | [`SPEC.md`](SPEC.md) + [`conformance/`](conformance/) | how kits **behave**: routing, CRDT merge, wire mapping, schema parsing. |

The protos pin the **shape**; `SPEC.md` and the vectors pin the **behavior**.
Where a rule is testable, the vector is the authority and `SPEC.md` explains
*why*.

## Package naming

Packages are `obscura.<layer>.<version>` — the name states the *layer*, and the
suffix is a genuine version (`buf`'s `STANDARD` lint requires a version suffix).
A breaking redesign of a layer is a real version bump (`…v1` → `…v2`), never a
new layer name.

- **`obscura.client.v1`** — the L2 client-content layer.
- **`obscura.v1`** — *legacy exception.* This is the L1 **transport** layer; by
  the convention it would be `obscura.transport.v1`, but it predates the
  convention and is consumed by `obscura-server`, so it is left as-is until a
  coordinated server change can rename it. Read `obscura.v1` as "transport", not
  "version 1 of everything".

## Consumers

| Repo | Role | Consumes |
|---|---|---|
| `obscura-server` | zero-knowledge relay | L1 |
| `ObscuraKit-Kotlin` | Android kit | L1 + L2 + L3 |
| `ObscuraKit-swift` | iOS/macOS kit | L1 + L2 + L3 |
| `obscura-client-web` | throwaway PoC (non-shipping) | — |

Each shipping consumer pins this repo as a git submodule and generates code from
the `.proto` files; the kits additionally run the L3 vectors in their own test
suites. `obscura-client-web` is a proof-of-concept and is **not** a normative
conformance target.

## Working here

- **CI gates** (`.github/workflows/ci.yml`): `buf lint` (STANDARD), `buf
  breaking` (vs `main`, on PRs), and `python3 conformance/validate.py`
  (vector well-formedness). Run the validator locally the same way.
- **Generated code lives in the consumers, not here.** This repo ships only
  `.proto` + vectors. Kotlin generates via the Gradle protobuf plugin; Swift
  generates with `protoc-gen-swift` into a checked-in `*.pb.swift`.
- **Changing the contract:** edit the `.proto` (shape) and/or a vector + `SPEC.md`
  (behavior). Each consumer adopts by bumping its `proto` submodule in a PR —
  that PR's build/conformance suite is the gate. A breaking change to a
  client-only layer means all clients ship together. See
  [`conformance/README.md`](conformance/README.md#enforcement-ci).

## Layout

```
obscura/
  v1/obscura.proto          # L1 transport (server ⇄ kits)
  client/v1/client.proto    # L2 client content (kit ⇄ kit)
conformance/
  *.json                    # L3 behavior vectors
  validate.py               # upstream well-formedness gate
  README.md                 # vector formats + rationale
SPEC.md                     # L3 prose contract
buf.yaml                    # lint STANDARD + breaking FILE
```
