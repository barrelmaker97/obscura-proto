# Transport contract

This document is normative for `obscura-server` and `obscura-native`.
`obscura/v1/obscura.proto` defines shape; this document defines behavior.

## Scope

The transport carries encrypted client bytes. The server may authenticate,
queue, route, timestamp, and delete those bytes, but MUST NOT parse their
client-to-client content.

## Message submission

`POST /v1/messages` accepts a `SendMessageRequest` containing independently
encrypted submissions addressed to device UUIDs.

- `submission_id` and `device_id` are 16-byte UUIDs.
- `message` is non-empty opaque encrypted content.
- HTTP idempotency is carried by the `Idempotency-Key` header.
- Failed submissions are reported individually; an empty failure list means
  the batch was accepted.

## Gateway frames

The WebSocket exchanges `WebSocketFrame` values:

- `EnvelopeBatch`: queued encrypted messages from server to native client;
- `AckMessage`: destructive acknowledgement from native client to server; and
- `PreKeyStatus`: advisory one-time-prekey inventory.

Unknown or malformed frames are protocol errors and MUST NOT be interpreted as
another frame type.

## Acknowledgement is deletion

When the server accepts an envelope ID in `AckMessage`, it deletes that queued
message. There is no server tombstone or second delivery path.

The native client therefore MUST:

1. not acknowledge a decryption failure;
2. not acknowledge deferred processing;
3. complete durable handling before acknowledgement; and
4. order each receive as `decrypt -> persist/handle -> optional wake -> ack`.

A duplicate already present in durable native storage counts as handled and may
be acknowledged.

## Envelope identity

The server stamps both identity hints from the sender's device-scoped
authentication:

| Field | Meaning |
|---|---|
| `sender_id` | Sending user UUID. |
| `sender_device_id` | Sending device UUID used to select the inbound Signal session. |

Neither field is a cryptographic trust root. Successful Signal decryption
proves possession of the selected device session. The native client MUST NOT
guess a missing device ID or fall back to `registrationId`.

The native client derives device attribution from the Signal session that
decrypts the message. Display names come from local trusted state, never from
transport or encrypted payload claims.

When local state already knows the owner of `sender_device_id`, native clients
SHOULD compare it with `sender_id` and report a mismatch.

## Envelope IDs and timestamps

- `Envelope.id` is a 16-byte UUID and is the acknowledgement/deduplication key.
- `Envelope.timestamp` is server-generated receipt time in epoch milliseconds.
- Client-content timestamps are outside this transport contract.

## Compatibility

Field numbers and wire types are stable within `obscura.v1`. Removing or
renumbering a field is breaking. Additive fields require regenerated server,
Kotlin, and Swift bindings before relying on them.

Client-content schema and semantics are intentionally outside this repository.
