# obscura-proto

The shared contract for Obscura: Protocol Buffer schemas, normative behavior,
and language-neutral wire-encoding vectors. This repo is consumed as a git
submodule by the server and client kits.

## Layers

Obscura's wire contract has three layers. This repo owns the two schema layers
and pins the third (behavior) with prose + vectors.

| Layer | Artifact | Who speaks it |
|---|---|---|
| **Transport** | [`obscura/v1/obscura.proto`](obscura/v1/obscura.proto) | server ⇄ kits: the envelope the server routes. Its encrypted `content` is opaque to the server. |
| **Content** | [`obscura/client/v1/client.proto`](obscura/client/v1/client.proto) | kit ⇄ kit: the E2E payload *inside* `content`. The server never parses it. |
| **Semantics** | [`SPEC.md`](SPEC.md), [`KIT_API.md`](KIT_API.md), and [`conformance/`](conformance/) | The app/kit boundary, receive durability, routing, merge, and wire mapping. |

The protos pin the **shape**. `SPEC.md` and `KIT_API.md` define behavior;
`wire.json` is the executable cross-kit encoding contract. App-owned routing
and merge are implemented and tested once in `obscura-pix`.

## Package naming

Packages are `obscura.<layer>.<version>` — the name states the *layer*, and the
suffix is a genuine version (`buf`'s `STANDARD` lint requires a version suffix).
A breaking redesign of a layer is a real version bump (`…v1` → `…v2`), never a
new layer name.

- **`obscura.client.v1`** — the client-content layer.
- **`obscura.v1`** — *legacy exception.* This is the **transport** layer; by
  the convention it would be `obscura.transport.v1`, but it predates the
  convention and is consumed by `obscura-server`, so it is left as-is until a
  coordinated server change can rename it. Read `obscura.v1` as "transport", not
  "version 1 of everything".

## Consumers

| Repo | Role | Consumes |
|---|---|---|
| `obscura-server` | zero-knowledge relay | transport |
| `ObscuraKit-Kotlin` | Android kit | transport + content + semantics |
| `ObscuraKit-swift` | iOS/macOS kit | transport + content + semantics |
| `obscura-pix` | React Native application | app-owned routing, merge, and payload semantics |
| `obscura-client-web` | throwaway PoC (non-shipping) | — |

Each shipping protocol consumer pins this repo as a git submodule and generates
code from the `.proto` files; the kits additionally run the wire vectors in
their own test suites. `obscura-client-web` is a proof-of-concept and is
**not** a normative conformance target.

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
  [`conformance/README.md`](conformance/README.md#enforcement).

## Layout

```
obscura/
  v1/obscura.proto          # transport layer (server ⇄ kits)
  client/v1/client.proto    # client content (kit ⇄ kit)
conformance/
  wire.json                 # cross-kit encoding vectors
  validate.py               # upstream well-formedness gate
  README.md                 # vector contract
SPEC.md                     # behavior (semantics) prose contract
KIT_API.md                  # kit/application boundary
HISTORY.md                  # non-normative migration record
buf.yaml                    # lint STANDARD + breaking FILE
```
