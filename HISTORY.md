# Transport history

> Non-normative. Use `TRANSPORT.md` and `obscura/v1/obscura.proto` for current
> behavior.

## Device identity on envelopes

`Envelope.sender_device_id` was added so recipients can select the exact
pairwise Signal session. Before this field existed, clients fell back to
`registrationId` or a default address, which broke multi-device delivery and
could misattribute an author device.

The server now stamps both `sender_id` and `sender_device_id` from the
device-scoped token. Native clients address Signal sessions by device UUID.

## Destructive acknowledgement

The receive contract was clarified after clients acknowledged decrypt failures
and deferred messages. An ACK deletes the server row, so durable handling must
complete first.

## Client contract relocation

In 2026-08, the Kotlin and Swift implementations moved into one
`obscura-native` repository. The client-to-client schema, wire vectors, native
API contract, and implementation history moved with their only remaining
consumer:

- `obscura-native/protocol/`
- `obscura-native/docs/`

Application routing and merge rules moved to
`obscura-pix/docs/DOMAIN_CONTRACT.md`. Git history before the relocation remains
available in this repository.
