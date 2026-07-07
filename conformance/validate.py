#!/usr/bin/env python3
"""Self-contained well-formedness validator for the L3 conformance vectors.

This is obscura-proto's *upstream* CI responsibility: guarantee the vector
artifacts are coherent (valid JSON, expected structure, no orphan files, spec
cross-referenced) WITHOUT any knowledge of how a kit builds. Whether a kit
*satisfies* a vector is the consumer's job, verified in each kit's own CI when
it adopts a new proto commit (submodule bump). See conformance/README.md.

Runs with only the Python stdlib. Collects every violation, then exits non-zero
so an author sees all problems at once.

    python3 conformance/validate.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

CONFORMANCE_DIR = Path(__file__).resolve().parent
SPEC = CONFORMANCE_DIR.parent / "SPEC.md"

# The vector files this validator knows how to check. Adding a new behavior
# class is a deliberate act: register its file + a checker here (fail-loud).
KNOWN_FILES = {"routing", "merge", "wire", "schema"}

SYNC_STRATEGIES = {"gset", "lww"}
AUDIENCE_KINDS = {"friends", "self", "recipient", "conversation"}
FIELD_TYPES = {"string", "number", "boolean", "timestamp"}
APPLY_ORDERS = {"forward", "reverse"}
ROUTING_ERRORS = {"DIRECT_ROUTING_UNRESOLVED"}
SCHEMA_ERRORS = {"INVALID_SCHEMA"}


class Report:
    """Accumulates violations with a file/context path prefix."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def err(self, where: str, msg: str) -> None:
        self.errors.append(f"{where}: {msg}")

    def is_str(self, where: str, obj: dict, key: str, *, allow_none: bool = False) -> bool:
        if key not in obj:
            self.err(where, f"missing required key '{key}'")
            return False
        v = obj[key]
        if v is None and allow_none:
            return True
        if not isinstance(v, str) or (v == "" and not allow_none):
            self.err(where, f"'{key}' must be a non-empty string, got {v!r}")
            return False
        return True

    def is_dict(self, where: str, obj: dict, key: str) -> bool:
        if key not in obj:
            self.err(where, f"missing required key '{key}'")
            return False
        if not isinstance(obj[key], dict):
            self.err(where, f"'{key}' must be an object, got {type(obj[key]).__name__}")
            return False
        return True

    def is_nonempty_list(self, where: str, obj: dict, key: str) -> bool:
        if key not in obj:
            self.err(where, f"missing required key '{key}'")
            return False
        if not isinstance(obj[key], list) or not obj[key]:
            self.err(where, f"'{key}' must be a non-empty array")
            return False
        return True

    def only_keys(self, where: str, obj: dict, allowed: set[str]) -> None:
        extra = set(obj) - allowed
        if extra:
            self.err(where, f"unexpected key(s) {sorted(extra)}; allowed: {sorted(allowed)}")


def _check_common(rep: Report, where: str, doc: dict, expected_kind: str) -> None:
    if doc.get("version") != 1:
        rep.err(where, f"'version' must be the integer 1, got {doc.get('version')!r}")
    if doc.get("kind") != expected_kind:
        rep.err(where, f"'kind' must be '{expected_kind}', got {doc.get('kind')!r}")
    if "description" in doc and not isinstance(doc["description"], str):
        rep.err(where, "'description' must be a string when present")


def _check_expect_error_xor(rep: Report, where: str, expect: dict, allowed_errors: set[str]) -> bool:
    """expect is either an error outcome or a value outcome, never both. Returns True if error."""
    if "error" in expect:
        if set(expect) != {"error"}:
            rep.err(where, "when 'error' is present, 'expect' must contain ONLY 'error'")
        if expect["error"] not in allowed_errors:
            rep.err(where, f"'expect.error' must be one of {sorted(allowed_errors)}, got {expect['error']!r}")
        return True
    return False


def _check_audience(rep: Report, where: str, aud: object) -> None:
    if not isinstance(aud, dict):
        rep.err(where, "'audience' must be an object")
        return
    kind = aud.get("kind")
    if kind not in AUDIENCE_KINDS:
        rep.err(where, f"audience.kind must be one of {sorted(AUDIENCE_KINDS)}, got {kind!r}")
    if kind in ("recipient", "conversation") and not aud.get("field"):
        rep.err(where, f"audience.kind '{kind}' requires a non-empty 'field'")


def _unique_names(rep: Report, where: str, cases: list) -> None:
    seen: set[str] = set()
    for i, case in enumerate(cases):
        name = case.get("name") if isinstance(case, dict) else None
        if isinstance(name, str) and name in seen:
            rep.err(where, f"duplicate case name {name!r} (index {i}); names must be unique")
        if isinstance(name, str):
            seen.add(name)


def check_routing(rep: Report, doc: dict) -> None:
    f = "routing.json"
    _check_common(rep, f, doc, "routing")
    rep.only_keys(f, doc, {"version", "kind", "description", "topology", "cases"})
    if rep.is_dict(f, doc, "topology"):
        topo = doc["topology"]
        rep.is_str(f + " topology", topo, "selfUserId")
        if rep.is_nonempty_list(f + " topology", topo, "friends"):
            for i, fr in enumerate(topo["friends"]):
                w = f"{f} topology.friends[{i}]"
                if not isinstance(fr, dict):
                    rep.err(w, "must be an object")
                    continue
                rep.is_str(w, fr, "userId")
                rep.is_str(w, fr, "username")
                rep.is_str(w, fr, "status")
    self_id = doc.get("topology", {}).get("selfUserId") if isinstance(doc.get("topology"), dict) else None
    if not rep.is_nonempty_list(f, doc, "cases"):
        return
    _unique_names(rep, f, doc["cases"])
    for i, case in enumerate(doc["cases"]):
        w = f"{f} cases[{i}] ({case.get('name', '?')})"
        if not isinstance(case, dict):
            rep.err(w, "case must be an object")
            continue
        rep.only_keys(w, case, {"name", "schema", "entry", "expect"})
        rep.is_str(w, case, "name")
        if rep.is_dict(w, case, "schema"):
            sch = case["schema"]
            if "sync" in sch and sch["sync"] not in SYNC_STRATEGIES:
                rep.err(w, f"schema.sync must be one of {sorted(SYNC_STRATEGIES)}, got {sch['sync']!r}")
            if "audience" in sch:
                _check_audience(rep, w, sch["audience"])
        if rep.is_dict(w, case, "entry"):
            rep.is_str(w + " entry", case["entry"], "id")
            rep.is_dict(w + " entry", case["entry"], "data")
        if rep.is_dict(w, case, "expect"):
            expect = case["expect"]
            if not _check_expect_error_xor(rep, w, expect, ROUTING_ERRORS):
                if "recipients" not in expect:
                    rep.err(w, "'expect' must contain either 'recipients' or 'error'")
                elif not isinstance(expect["recipients"], list) or not all(
                    isinstance(x, str) for x in expect["recipients"]
                ):
                    rep.err(w, "'expect.recipients' must be an array of userId strings")
                elif self_id is not None and self_id not in expect["recipients"]:
                    rep.err(w, f"'expect.recipients' must include selfUserId {self_id!r} (a write reaches the author's own devices)")


def check_merge(rep: Report, doc: dict) -> None:
    f = "merge.json"
    _check_common(rep, f, doc, "merge")
    rep.only_keys(f, doc, {"version", "kind", "description", "cases"})
    if not rep.is_nonempty_list(f, doc, "cases"):
        return
    _unique_names(rep, f, doc["cases"])
    for i, case in enumerate(doc["cases"]):
        w = f"{f} cases[{i}] ({case.get('name', '?')})"
        if not isinstance(case, dict):
            rep.err(w, "case must be an object")
            continue
        rep.only_keys(w, case, {"name", "sync", "ops", "applyOrders", "expect"})
        rep.is_str(w, case, "name")
        if case.get("sync") not in SYNC_STRATEGIES:
            rep.err(w, f"'sync' must be one of {sorted(SYNC_STRATEGIES)}, got {case.get('sync')!r}")
        if "applyOrders" in case:
            ao = case["applyOrders"]
            if not isinstance(ao, list) or not ao or any(x not in APPLY_ORDERS for x in ao):
                rep.err(w, f"'applyOrders' must be a non-empty subset of {sorted(APPLY_ORDERS)}")
        if rep.is_nonempty_list(w, case, "ops"):
            for j, op in enumerate(case["ops"]):
                ow = f"{w} ops[{j}]"
                if not isinstance(op, dict):
                    rep.err(ow, "op must be an object")
                    continue
                rep.is_str(ow, op, "id")
                rep.is_str(ow, op, "authorDeviceId")
                rep.is_dict(ow, op, "data")
                if not isinstance(op.get("ts"), (int, float)) or isinstance(op.get("ts"), bool):
                    rep.err(ow, f"'ts' must be a number, got {op.get('ts')!r}")
        if rep.is_dict(w, case, "expect"):
            if rep.is_nonempty_list(w + " expect", case["expect"], "entries"):
                for j, ent in enumerate(case["expect"]["entries"]):
                    ew = f"{w} expect.entries[{j}]"
                    if not isinstance(ent, dict):
                        rep.err(ew, "entry must be an object")
                        continue
                    rep.is_str(ew, ent, "id")
                    if "deleted" in ent and not isinstance(ent["deleted"], bool):
                        rep.err(ew, "'deleted' must be a boolean when present")


def check_wire(rep: Report, doc: dict) -> None:
    f = "wire.json"
    _check_common(rep, f, doc, "wire")
    rep.only_keys(f, doc, {"version", "kind", "description", "messageTypes", "modelSyncOps", "signalKinds", "roundTrip"})
    for mapping in ("messageTypes", "modelSyncOps", "signalKinds"):
        if rep.is_nonempty_list(f, doc, mapping):
            for i, m in enumerate(doc[mapping]):
                w = f"{f} {mapping}[{i}]"
                if not isinstance(m, dict):
                    rep.err(w, "must be an object")
                    continue
                rep.only_keys(w, m, {"wire", "app"})
                rep.is_str(w, m, "wire")
                rep.is_str(w, m, "app")
    if rep.is_nonempty_list(f, doc, "roundTrip"):
        _unique_names(rep, f + " roundTrip", doc["roundTrip"])
        for i, rt in enumerate(doc["roundTrip"]):
            w = f"{f} roundTrip[{i}] ({rt.get('name', '?')})"
            if not isinstance(rt, dict):
                rep.err(w, "must be an object")
                continue
            rep.only_keys(w, rt, {"name", "modelSync"})
            rep.is_str(w, rt, "name")
            if rep.is_dict(w, rt, "modelSync"):
                ms = rt["modelSync"]
                rep.is_str(w + " modelSync", ms, "model")
                rep.is_str(w + " modelSync", ms, "id")
                rep.is_str(w + " modelSync", ms, "op")
                rep.is_dict(w + " modelSync", ms, "data")
                if not isinstance(ms.get("timestamp"), (int, float)) or isinstance(ms.get("timestamp"), bool):
                    rep.err(w + " modelSync", f"'timestamp' must be a number, got {ms.get('timestamp')!r}")


def check_schema(rep: Report, doc: dict) -> None:
    f = "schema.json"
    _check_common(rep, f, doc, "schema")
    rep.only_keys(f, doc, {"version", "kind", "description", "cases"})
    if not rep.is_nonempty_list(f, doc, "cases"):
        return
    _unique_names(rep, f, doc["cases"])
    for i, case in enumerate(doc["cases"]):
        w = f"{f} cases[{i}] ({case.get('name', '?')})"
        if not isinstance(case, dict):
            rep.err(w, "case must be an object")
            continue
        rep.only_keys(w, case, {"name", "config", "expect"})
        rep.is_str(w, case, "name")
        if rep.is_dict(w, case, "config"):
            rep.is_dict(w + " config", case["config"], "fields")
        if not rep.is_dict(w, case, "expect"):
            continue
        expect = case["expect"]
        if _check_expect_error_xor(rep, w, expect, SCHEMA_ERRORS):
            continue
        # Value outcome: the normalized model definition.
        if expect.get("sync") not in SYNC_STRATEGIES:
            rep.err(w, f"'expect.sync' must be one of {sorted(SYNC_STRATEGIES)}, got {expect.get('sync')!r}")
        if "ttl" in expect and expect["ttl"] is not None and not isinstance(expect["ttl"], str):
            rep.err(w, "'expect.ttl' must be a string or null")
        else:
            if "ttl" not in expect:
                rep.err(w, "'expect.ttl' is required (use null for no ttl)")
        if "audience" in expect:
            _check_audience(rep, w, expect["audience"])
        else:
            rep.err(w, "'expect.audience' is required")
        if rep.is_dict(w, expect, "fields"):
            for name, fld in expect["fields"].items():
                fw = f"{w} expect.fields.{name}"
                if not isinstance(fld, dict):
                    rep.err(fw, "must be an object { type, optional }")
                    continue
                rep.only_keys(fw, fld, {"type", "optional"})
                if fld.get("type") not in FIELD_TYPES:
                    rep.err(fw, f"'type' must be one of {sorted(FIELD_TYPES)}, got {fld.get('type')!r}")
                if not isinstance(fld.get("optional"), bool):
                    rep.err(fw, "'optional' must be a boolean")


CHECKERS = {
    "routing": check_routing,
    "merge": check_merge,
    "wire": check_wire,
    "schema": check_schema,
}


def main() -> int:
    rep = Report()

    present = {p.stem for p in CONFORMANCE_DIR.glob("*.json")}
    orphans = present - KNOWN_FILES
    for stem in sorted(orphans):
        rep.err(f"{stem}.json", "unknown vector file — register it in validate.py (KNOWN_FILES + a checker) and SPEC.md")
    missing = KNOWN_FILES - present
    for stem in sorted(missing):
        rep.err(f"{stem}.json", "expected vector file is missing")

    spec_text = SPEC.read_text() if SPEC.exists() else ""
    if not SPEC.exists():
        rep.err("SPEC.md", "canonical spec not found next to conformance/")

    for stem in sorted(KNOWN_FILES & present):
        path = CONFORMANCE_DIR / f"{stem}.json"
        try:
            doc = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            rep.err(f"{stem}.json", f"invalid JSON: {e}")
            continue
        if not isinstance(doc, dict):
            rep.err(f"{stem}.json", "top level must be a JSON object")
            continue
        CHECKERS[stem](rep, doc)
        if f"{stem}.json" not in spec_text:
            rep.err(f"{stem}.json", "not referenced anywhere in SPEC.md (spec drift / orphan)")

    if rep.errors:
        print(f"conformance vectors INVALID — {len(rep.errors)} problem(s):\n", file=sys.stderr)
        for e in rep.errors:
            print(f"  ✗ {e}", file=sys.stderr)
        return 1

    print(f"conformance vectors OK — {len(KNOWN_FILES & present)} files well-formed and spec-referenced")
    return 0


if __name__ == "__main__":
    sys.exit(main())
