"""Immutable human review events. No microscope, image-processing or database imports.

Drive is an eventually consistent filesystem, not a distributed database. A local
exclusive lock serializes this host; forked event chains from another host are
detected and never silently merged. The event files are authoritative.
"""
from contextlib import contextmanager
from pathlib import Path
import copy
import hashlib
import json
import math
import os
import re
import socket
import threading
import time
import uuid


class RevisionConflict(ValueError):
    pass


class SourceIdentityError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def identity(value):
    keys = ("dataset_id", "field_key", "source_sha256", "shape_hw", "group_id")
    result = {key: value.get(key) for key in keys}
    for key in ("dataset_id", "field_key", "group_id"):
        if not isinstance(result[key], str) or not 0 < len(result[key]) <= 300:
            raise SourceIdentityError("Missing source identity: " + key)
    if not re.fullmatch(r"[0-9a-f]{64}", str(result["source_sha256"])):
        raise SourceIdentityError("A recorded image SHA-256 is required before labeling")
    shape = result["shape_hw"]
    if not isinstance(shape, list) or len(shape) != 2 or any(type(v) is not int or v < 1 for v in shape):
        raise SourceIdentityError("Source image dimensions are unavailable")
    return result


def polygon(points, shape):
    if not isinstance(points, list) or not 3 <= len(points) <= 2000:
        raise ValueError("A polygon needs 3–2000 vertices")
    clean = []
    height, width = shape
    for point in points:
        if not isinstance(point, list) or len(point) != 2 or any(type(x) not in (int, float) or not math.isfinite(x) for x in point):
            raise ValueError("Polygon coordinates must be finite source pixels")
        x, y = map(float, point)
        if not 0 <= x <= width or not 0 <= y <= height:
            raise ValueError("Polygon extends outside the source image")
        clean.append([x, y])
    area = abs(sum(a[0]*b[1]-b[0]*a[1] for a, b in zip(clean, clean[1:]+clean[:1])))/2
    if area < 1:
        raise ValueError("Polygon area must be at least one source pixel")
    def cross(a, b, c):
        return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
    for i in range(len(clean)):
        a, b = clean[i], clean[(i+1) % len(clean)]
        for j in range(i+2, len(clean)):
            if i == 0 and j == len(clean)-1:
                continue
            c, d = clean[j], clean[(j+1) % len(clean)]
            if cross(a, b, c)*cross(a, b, d) < 0 and cross(c, d, a)*cross(c, d, b) < 0:
                raise ValueError("Self-intersecting polygons are not accepted")
    return clean


SEMANTICS = {"unreviewed", "flake_of_interest", "bulk", "residue", "dust", "substrate", "edge", "artifact", "uncertain"}
QUALITY = {"focus_limited", "clipped", "mixed", "edge_truncated", "ambiguous_boundary", "usable", "needs_revisit"}


def labels(value):
    if not isinstance(value, dict):
        raise ValueError("Labels must be an object")
    allowed = {"semantic", "material", "material_status", "layer_value", "evidence_method", "evidence_reference", "quality", "note"}
    if set(value)-allowed:
        raise ValueError("Unknown human label fields")
    result = copy.deepcopy(value)
    if "semantic" in result and result["semantic"] not in SEMANTICS:
        raise ValueError("Unknown semantic label")
    if result.get("material_status", "unconfirmed") not in ("unconfirmed", "confirmed"):
        raise ValueError("Unknown material evidence status")
    for key in ("material", "layer_value", "evidence_method", "evidence_reference", "note"):
        if key in result and (not isinstance(result[key], str) or len(result[key]) > (4000 if key == "note" else 500)):
            raise ValueError("Invalid label text: " + key)
    if "quality" in result and (not isinstance(result["quality"], list) or any(v not in QUALITY for v in result["quality"])):
        raise ValueError("Unknown quality evidence")
    if result.get("material_status") == "confirmed":
        if result.get("evidence_method") not in ("AFM", "Raman", "PL", "other_measurement") or not result.get("evidence_reference", "").strip():
            raise ValueError("Confirmed identity requires a measurement method and reference; visual appearance alone is provisional")
    return result


class ReviewEventStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.lock = threading.RLock()

    def folder(self, source):
        source = identity(source)
        return self.root / "events" / digest([source["dataset_id"], source["field_key"]])[:32]

    def _events(self, source):
        folder = self.folder(source)
        events = []
        for file in sorted(folder.glob("*.json")):
            event = json.loads(file.read_text(encoding="utf-8"))
            checksum = event.pop("event_sha256", None)
            if digest(event) != checksum:
                raise RevisionConflict("Review event checksum mismatch: " + file.name)
            event["event_sha256"] = checksum
            if event.get("source_identity") != identity(source):
                raise SourceIdentityError("Saved review belongs to different source pixels or grouping")
            events.append(event)
        events.sort(key=lambda e: (e["revision"], e["event_id"]))
        previous = None
        for revision, event in enumerate(events, 1):
            if event["revision"] != revision or event.get("parent_sha256") != previous:
                raise RevisionConflict("Divergent or incomplete review history. Preserve both branches; explicit reconciliation is required")
            previous = event["event_sha256"]
        return events

    @staticmethod
    def _materialize(events):
        active = []
        for event in events:
            ops = event["operations"]
            if len(ops) == 1 and ops[0]["kind"] == "undo":
                if not active or active[-1]["event_id"] != ops[0]["target_event_id"]:
                    raise RevisionConflict("Undo target does not match the active review history")
                active.pop()
            else:
                active.append(event)
        regions = {}
        for event in active:
            for op in event["operations"]:
                region = regions.setdefault(op["region_id"], {"region_id": op["region_id"], "labels": {}, "human_geometry": None})
                if op["kind"] == "annotate":
                    region["labels"].update(op["labels"])
                elif op["kind"] in ("create_region", "set_geometry"):
                    region["human_geometry"] = op["polygons_px"]
                    region["is_human_region"] = op["kind"] == "create_region" or region.get("is_human_region", False)
                    region["parent_ids"] = op.get("parent_ids", region.get("parent_ids", []))
                elif op["kind"] == "set_hidden":
                    region["hidden"] = op["hidden"]
                region["last_event_id"] = event["event_id"]
                region["reviewed_at"] = event["created_at"]
                if op.get("source_proposal"):
                    region["source_proposal"] = op["source_proposal"]
                if op.get("prior_human_review"):
                    region["prior_human_review"] = op["prior_human_review"]
        return regions, active

    def read(self, source):
        source = identity(source)
        with self.lock:
            events = self._events(source)
            regions, active = self._materialize(events)
        return {"source_identity": source, "revision": len(events), "head_sha256": events[-1]["event_sha256"] if events else None,
                "regions": regions, "last_undoable_event_id": active[-1]["event_id"] if active else None,
                "history": [{k: e[k] for k in ("event_id", "revision", "created_at", "actor", "operations")} for e in events],
                "durability": "Immutable local Drive files; cloud synchronization is not verified"}

    @contextmanager
    def _exclusive(self, folder):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ".writer.lock"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise RevisionConflict("Another writer holds this field, or an interrupted writer needs inspection") from exc
        try:
            os.write(fd, canonical({"pid": os.getpid(), "host": socket.gethostname(), "created_at": time.time()}))
            os.fsync(fd)
            yield
        finally:
            os.close(fd)
            path.unlink(missing_ok=True)

    def append(self, source, *, expected_revision, operations, actor, client_event_id, machine_region_ids=()):
        source = identity(source)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Invalid expected revision")
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 80:
            raise ValueError("A reviewer name is required")
        if not isinstance(client_event_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", client_event_id):
            raise ValueError("A stable client event identifier is required")
        if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
            raise ValueError("Select 1–100 deliberate operations")
        request_hash = digest({"source": source, "operations": operations, "actor": actor, "client_event_id": client_event_id})
        with self.lock, self._exclusive(self.folder(source)):
            events = self._events(source)
            for old in events:
                if old["client_event_id"] == client_event_id:
                    if old["request_sha256"] != request_hash:
                        raise RevisionConflict("Client event identifier was reused with different content")
                    return self.read(source)
            if expected_revision != len(events):
                raise RevisionConflict("Review changed elsewhere; reload before saving")
            state, active = self._materialize(events)
            known = set(machine_region_ids) | set(state)
            clean = []
            for op in operations:
                kind = op.get("kind")
                if kind == "undo":
                    if len(operations) != 1 or not active or op.get("target_event_id") != active[-1]["event_id"]:
                        raise RevisionConflict("Only the latest active edit can be undone")
                    clean.append({"kind": "undo", "target_event_id": op["target_event_id"]})
                    continue
                rid = op.get("region_id")
                if not isinstance(rid, str) or not 0 < len(rid) <= 200:
                    raise ValueError("Invalid region identifier")
                if kind == "create_region":
                    if not rid.startswith("human-") or rid in known:
                        raise ValueError("New human regions need a unique human- identifier")
                    parents = op.get("parent_ids", [])
                    if not isinstance(parents, list) or any(p not in known for p in parents):
                        raise ValueError("Unknown parent region")
                    known.add(rid)
                elif rid not in known:
                    raise SourceIdentityError("Region is not present in this source field")
                if kind == "annotate":
                    update = labels(op.get("labels"))
                    combined = {**state.get(rid, {}).get("labels", {}), **update}
                    labels(combined)
                    clean.append({"kind": kind, "region_id": rid, "labels": update})
                elif kind in ("create_region", "set_geometry"):
                    loops = op.get("polygons_px")
                    if not isinstance(loops, list) or not 1 <= len(loops) <= 100:
                        raise ValueError("Geometry needs outer polygon and optional holes")
                    clean.append({"kind": kind, "region_id": rid, "polygons_px": [polygon(p, source["shape_hw"]) for p in loops], "parent_ids": op.get("parent_ids", [])})
                elif kind == "set_hidden":
                    if not state.get(rid, {}).get("is_human_region") or type(op.get("hidden")) is not bool:
                        raise ValueError("Machine proposals cannot be deleted or hidden by a geometry event")
                    clean.append({"kind": kind, "region_id": rid, "hidden": op["hidden"]})
                else:
                    raise ValueError("Unknown review operation")
                for provenance_key in ("source_proposal", "prior_human_review"):
                    if op.get(provenance_key):
                        clean[-1][provenance_key] = copy.deepcopy(op[provenance_key])
            event = {"schema_version": 1, "event_id": uuid.uuid4().hex, "client_event_id": client_event_id, "request_sha256": request_hash,
                     "source_identity": source, "revision": len(events)+1, "parent_sha256": events[-1]["event_sha256"] if events else None,
                     "created_at": time.time(), "actor": actor.strip(), "host": socket.gethostname(), "operations": clean}
            event["event_sha256"] = digest(event)
            target = self.folder(source) / f"{event['revision']:08d}--{event['event_id']}.json"
            temporary = target.with_suffix(".pending")
            raw = canonical(event)
            with temporary.open("xb") as stream:
                stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, target)
            if target.read_bytes() != raw:
                raise OSError("Review event readback failed")
            return self.read(source)

    def export(self):
        rows, warnings = [], []
        for folder in sorted((self.root / "events").glob("*")):
            files = sorted(folder.glob("*.json"))
            if not files:
                continue
            try:
                source = json.loads(files[0].read_text(encoding="utf-8"))["source_identity"]
                state = self.read(source)
                for region in state["regions"].values():
                    rows.append({**copy.deepcopy(region), "source_identity": source, "group_id": source["group_id"],
                                 "revision": state["revision"], "head_sha256": state["head_sha256"],
                                 "label_origin": "human_review", "split_policy": "Keep every observation with this group_id in the same ML partition"})
            except (OSError, ValueError, KeyError) as error:
                warnings.append({"stream": folder.name, "error": str(error)})
        return {"schema_version": 1, "exported_at": time.time(), "regions": rows, "warnings": warnings,
                "qualification": "Human appearance labels are not automatic material or layer ground truth. Preserve source/group identities in ML splits."}
