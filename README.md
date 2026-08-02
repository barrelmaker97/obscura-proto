# Obscura transport protocol

The Protocol Buffer contract shared by `obscura-server` and
`obscura-native`. The server routes encrypted bytes and never parses the
client-to-client content inside them.

## Contents

| Artifact | Purpose |
|---|---|
| [`obscura/v1/obscura.proto`](obscura/v1/obscura.proto) | REST message submission and gateway WebSocket frames. |
| [`TRANSPORT.md`](TRANSPORT.md) | Normative server/native transport behavior. |
| [`HISTORY.md`](HISTORY.md) | Non-normative transport migration record. |
| [`buf.yaml`](buf.yaml) | STANDARD lint and FILE-level breaking checks. |

Client-to-client encrypted content belongs to
[`obscura-native/protocol`](https://github.com/barrelmaker97/obscura-native/tree/main/protocol).
The native/app facade belongs to
[`obscura-native/docs`](https://github.com/barrelmaker97/obscura-native/tree/main/docs).
Application routing and merge behavior belong to
[`obscura-pix/docs/DOMAIN_CONTRACT.md`](https://github.com/rhelsing/obscura-pix/blob/main/docs/DOMAIN_CONTRACT.md).

## Consumers

Both consumers pin this repository as a git submodule and generate code from
the same schema:

- `obscura-server`
- `obscura-native`

## Changes

Run the existing CI gates:

```bash
buf lint
buf breaking --against '.git#branch=main'
```

A breaking schema change requires a coordinated server/native migration.
Generated code lives in consumers, not here.

The package remains `obscura.v1` for compatibility. It predates the current
`obscura.<layer>.<version>` naming convention and should be read as the
transport package.
