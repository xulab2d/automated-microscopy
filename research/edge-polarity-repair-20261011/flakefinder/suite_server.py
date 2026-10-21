"""Loopback-only offline workbench. No hardware modules or mutation routes."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit
import hashlib
import json
import math
import mimetypes
import secrets
import threading
import time
import uuid

from .suite_labels import ReviewEventStore, RevisionConflict, SourceIdentityError, identity


def inside(root, relative):
    candidate = Path(relative)
    if candidate.is_absolute() or candidate.drive or ".." in candidate.parts:
        raise ValueError("Path must remain inside the configured project")
    target = (root / candidate).resolve()
    if not target.is_relative_to(root):
        raise ValueError("Path escapes configured project")
    return target


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def contrast_selection(query):
    """Parse an optional view-only RGB rectangle; incomplete gates are errors."""
    names = ('contrast_x', 'contrast_y', 'x_min', 'x_max', 'y_min', 'y_max')
    if not any(name in query for name in names):
        return None
    if any(name not in query or len(query[name]) != 1 for name in names):
        raise ValueError('A contrast selection requires two axes and four bounds')
    x, y = query['contrast_x'][0], query['contrast_y'][0]
    if x not in ('R', 'G', 'B') or y not in ('R', 'G', 'B') or x == y:
        raise ValueError('Choose two different RGB contrast axes')
    box = {name: float(query[name][0]) for name in names[2:]}
    if not all(math.isfinite(v) for v in box.values()):
        raise ValueError('Contrast bounds must be finite')
    if box['x_min'] >= box['x_max'] or box['y_min'] >= box['y_max']:
        raise ValueError('Contrast bounds must have positive width and height')
    return dict(x=x, y=y, **box)


def rgb_contrast(row):
    """Do not turn absent or nonnumeric measurements into substrate zero."""
    values = row.get('contrast_rgb')
    if not isinstance(values, (list, tuple)) or len(values) != 3:
        return None
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
        return None
    return values


def in_contrast_selection(values, box):
    return values is not None and (box['x_min'] <= values['RGB'.index(box['x'])] <= box['x_max']
                                  and box['y_min'] <= values['RGB'.index(box['y'])] <= box['y_max'])


class SuiteCatalog:
    """Thin integration adapter; scientific catalogs remain independently owned."""
    def __init__(self, project_root, campaign_rel="data/whole-chip-standardized-01", provider=None):
        self.root = Path(project_root).resolve()
        self.campaign = inside(self.root, campaign_rel)
        self.campaign_rel = campaign_rel
        if provider is None:
            try:
                from .project_catalog import ProjectCatalog
                provider = ProjectCatalog(self.root, campaign_rel=campaign_rel)
            except ImportError:
                provider = None
        self.provider = provider
        self._fallback = None
        self._index_cache = (0, None)
        self._ledger_cache = (None, 0, {})
        self._live_cache = {}
        self._live_warnings = {}
        self._lock = threading.RLock()
        from .candidate_index import SavedCandidateIndex
        self._candidate_index = SavedCandidateIndex(self.campaign / 'analysis-residue-sieves-campaign-01')

    def _live_json(self, path, required_key=None):
        """Atomic publisher gaps retain the last successfully parsed snapshot."""
        path = Path(path)
        with self._lock:
            old = self._live_cache.get(path)
            try:
                stat = path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
                if old and old[0] == stamp:
                    self._live_warnings.pop(path, None)
                    return old[1]
                value = json.loads(path.read_text(encoding="utf-8-sig"))
                if not isinstance(value, dict) or required_key and required_key not in value:
                    raise ValueError("Incomplete live JSON snapshot")
                self._live_cache[path] = (stamp, value)
                self._live_warnings.pop(path, None)
                return value
            except (OSError, ValueError) as error:
                self._live_warnings[path] = {"source": path.relative_to(self.root).as_posix(),
                                             "error": str(error), "retained_snapshot": bool(old)}
                return old[1] if old else {}

    def catalog(self):
        with self._lock:
            if time.monotonic()-self._index_cache[0] < 10 and self._index_cache[1] is not None:
                return self._index_cache[1]
            if self.provider:
                value = self.provider.catalog()
            else:
                from .gallery import FieldGallery
                self._fallback = self._fallback or FieldGallery(self.campaign)
                index = self._fallback.index()
                dataset = hashlib.sha256(self.campaign_rel.encode()).hexdigest()[:20]
                fields = []
                for field in index["fields"]:
                    rel = Path(self.campaign_rel) / field["chip_id"] / "tiles" / (field["tile_id"]+".jpg")
                    fields.append({**field, "field_key": field["key"], "preview_rel": rel.as_posix()})
                value = {**index, "dataset_id": dataset, "project_id": "offline-project", "fields": fields}
            value = dict(value)
            fields = []
            for original in value.get("fields", []):
                field = dict(original)
                field["field_key"] = field.get("field_key") or field.get("key")
                if field.get("preview_rel"):
                    field["preview_url"] = "/files/"+quote(field["preview_rel"], safe="/")
                fields.append(field)
            value["fields"] = fields
            value["image_count"] = len(fields)
            self._index_cache = (time.monotonic(), value)
            return value

    def field(self, field_key):
        index = self.catalog()
        summary = next((r for r in index["fields"] if r["field_key"] == field_key), None)
        if summary is None:
            raise KeyError("Unknown saved field")
        if self.provider:
            value = dict(self.provider.get_field(field_key))
        else:
            chip, tile = field_key.split("/", 1)
            value = self._fallback.field(chip, tile)
        result = {**summary, **value, "field_key": field_key}
        if result.get("preview_rel"):
            result["preview_url"] = "/files/"+quote(result["preview_rel"], safe="/")
        else:
            result["preview_url"] = summary.get("preview_url")
        shape = result.get("shape_hw") or result.get("source_shape_hw") or result.get("shape")
        supplied = result.get("source_identity") or result.get("identity") or {}
        source = {"dataset_id": index["dataset_id"], "field_key": field_key,
                  "source_sha256": supplied.get("source_sha256") or result.get("source_sha256") or result.get("source_tiff_sha256"),
                  "shape_hw": supplied.get("shape_hw") or shape,
                  "group_id": supplied.get("group_id") or index["dataset_id"]+":"+field_key.split("/")[0]}
        try:
            result["source_identity"] = identity(source)
            result["labeling_available"] = True
        except SourceIdentityError as error:
            result["source_identity"] = source
            result["labeling_available"] = False
            result["labeling_error"] = str(error)
        rows = result.get("rows", result.get("candidates", []))
        prepared = []
        for original in rows:
            row = dict(original)
            row["id"] = str(row.get("id") or "instance-"+str(row.get("instance_label")))
            loops = row.get("polygons_px")
            if loops is None:
                loops = ([row["polygon_px"]] if row.get("polygon_px") else [])+row.get("holes_px", [])
            row["polygons_px"] = loops
            measurement = row.get("local_contrast_measurement") or {}
            row["display_contrast_rgb"] = measurement.get("contrast_rgb") or row.get("contrast_rgb")
            row["proposal_fingerprint"] = hashlib.sha256(json.dumps({"id": row["id"], "loops": loops, "bbox": row.get("bbox_px"), "mask_sha256": row.get("mask_sha256")}, sort_keys=True).encode()).hexdigest()
            prepared.append(row)
        result["rows"] = prepared
        # Previous direct-user reviews are evidence, never model-generated labels.
        prior = read_json(self.campaign / "analysis-residue-sieves-02/human-review.json", {}) or {}
        matched = []
        for review in prior.get("reviews", []):
            if review.get("field") != field_key or review.get("source_tiff_sha256") != source.get("source_sha256"):
                continue
            row = next((r for r in prepared if r["id"] == review.get("id")), None)
            if row and all(review.get(k) and review[k] == row.get(k) for k in ("mask_sha256", "source_mask_sha256")):
                matched.append({**review, "semantic": {"tape_residue": "residue"}.get(review.get("label"), review.get("label")),
                                "verified_against": "source TIFF, source mask, exact region mask", "origin": "prior_direct_user_review"})
        result["prior_human_reviews"] = matched
        return result

    def status(self):
        try:
            saved = self.provider.status() if self.provider and hasattr(self.provider, "status") else {}
        except (OSError, ValueError):
            saved = {}
        status_path = self.campaign / "analysis-residue-sieves-campaign-01/status.json"
        published = self._live_json(status_path, "state")
        analysis = dict(published or saved.get("analysis") or {})
        ledger = self.campaign / "analysis-residue-sieves-campaign-01/ledger.json"
        records = self._live_json(ledger)
        stamp = self._live_cache.get(ledger, (None,))[0]
        if stamp != self._ledger_cache[0]:
            if records:
                records = [v for k, v in records.items() if isinstance(v, dict) and "/" in k]
                totals = {}
                for record in records:
                    for category, count in record.get("summary", {}).get("counts", {}).items():
                        if isinstance(count, int) and count >= 0:
                            totals[category] = totals.get(category, 0) + count
                self._ledger_cache = (stamp, len(records), totals)
        committed = self._ledger_cache[1]
        raw_processed = int(analysis.get("processed_fields") or 0)
        analysis["raw_processed_fields"] = raw_processed
        analysis["committed_fields"] = committed
        analysis["index_rebuilding"] = committed > raw_processed
        if committed > raw_processed:
            analysis.update(processed_fields=committed,
                            saved_fields_verified=raw_processed, saved_fields_to_verify=committed,
                            counts=dict(self._ledger_cache[2]), counts_source="committed_ledger")
            if analysis.get("state") == "running":
                analysis["phase"] = "verifying_saved_fields"
        # Known status files only; this endpoint cannot read arbitrary paths.
        transfer = []
        for name in ("TRANSFER-STATUS.json", "TRANSFER-COMPLETION.json"):
            value = read_json(inside(self.root, name))
            if value is not None:
                transfer.append({"source": name, "status": value})
        return {**saved, "analysis": analysis, "transfer": transfer, "observed_at": time.time(), "hardware_available": False,
                "live_read_warnings": list(self._live_warnings.values()),
                "operator_hold": "Offline workbench; no microscope endpoints exist"}

    def candidates(self, review_store=None, include_all=False):
        """Small live survivor index; only reviewed matches need full-field reads."""
        analysis = self.campaign / "analysis-residue-sieves-campaign-01"
        saved = self._live_json(analysis / "catalog.json", "rows")
        activity = self.status()["analysis"]
        verifying = activity.get("index_rebuilding", False)
        retained = self._live_json(analysis / "resume-retained-catalog.json", "rows") if verifying else {}
        combined = {(r.get("field"), r.get("id")): r for r in retained.get("rows", [])}
        combined.update({(r.get("field"), r.get("id")): r for r in saved.get("rows", [])})
        index = self.catalog()
        fields = {f["field_key"]: f for f in index["fields"]}
        index_warnings = []
        if include_all:
            all_rows, index_warnings = self._candidate_index.rows(self._live_json(analysis / 'ledger.json'), fields)
            combined = {(r['field'], r['id']): r for r in all_rows}
        prior = read_json(self.campaign / "analysis-residue-sieves-02/human-review.json", {}) or {}
        prior_keys = {(r.get("field"), r.get("id")) for r in prior.get("reviews", [])}
        exported = review_store.export() if review_store else {"regions": [], "warnings": []}
        human = {(r["source_identity"]["field_key"], r["region_id"]): r for r in exported["regions"]}
        full_fields, rows, warnings = {}, [], list(exported.get("warnings", [])) + index_warnings
        exclusions = {"residue", "dust", "substrate", "edge", "artifact", "bulk"}
        for original in combined.values():
            row = dict(original)
            key = row.get("field")
            summary = fields.get(key)
            if not summary or (not include_all and row.get("disposition") not in ("shortlist", "uncertain")):
                continue
            reference = row.get("source_field_reference")
            try:
                if not isinstance(reference, str) or not reference:
                    raise ValueError("Missing source field reference")
                # Wide-index references were constructed and confined once per
                # verified shard. Resolving its Drive path for every region can
                # cost tens of seconds for thousands of rows.
                source = analysis / reference if include_all else inside(analysis, reference)
                row["source_record_rel"] = source.relative_to(self.root).as_posix()
            except ValueError as error:
                warnings.append({"field": key, "id": row.get("id"), "error": str(error)})
                continue
            row.update(field_key=key, chip_id=summary.get("chip_id"),
                       preview_url=summary.get("preview_url"), source_identity=summary.get("source_identity"),
                       human_excluded=False, human_review=None)
            pair = (key, row.get("id"))
            if pair in prior_keys or pair in human:
                try:
                    if key not in full_fields:
                        full_fields[key] = self.field(key)
                    full = full_fields[key]
                    current = next((r for r in full["rows"] if r["id"] == row["id"]), None)
                    if current:
                        reviewed = next((r for r in full.get("prior_human_reviews", []) if r["id"] == row["id"]), None)
                        if reviewed:
                            row["human_review"] = {"semantic": reviewed["semantic"], "origin": "hash_verified_prior_user_review",
                                                   "quote": reviewed.get("quote"), "reviewed_at": reviewed.get("reviewed_at")}
                        event = human.get(pair)
                        if event:
                            matches = event["source_identity"] == full["source_identity"] and event.get("source_proposal", {}).get("proposal_fingerprint") == current["proposal_fingerprint"]
                            if matches and event.get("labels", {}).get("semantic") not in (None, "unreviewed"):
                                row["human_review"] = {"semantic": event["labels"]["semantic"], "origin": "immutable_human_review",
                                                       "reviewed_at": event.get("reviewed_at"), "revision": event["revision"]}
                            elif not matches:
                                row["human_review_warning"] = "Saved review refers to a different source or proposal; it has not been applied"
                        row["human_excluded"] = bool(row["human_review"] and row["human_review"]["semantic"] in exclusions)
                except (OSError, ValueError, KeyError) as error:
                    row["human_review_warning"] = "Review identity could not be verified: " + str(error)
            rows.append(row)
        rows.sort(key=lambda r: (r["human_excluded"], r["disposition"] != "shortlist", -float(r.get("rank") or 0), r["field"], r["id"]))
        return {"schema": 1, "state": activity.get("state", saved.get("state", "not_started")),
                "phase": activity.get("phase"), "updated_at": activity.get("updated_at", saved.get("updated_at")),
                "index_rebuilding": verifying,
                "catalog_updated_at": saved.get("updated_at"), "retained_committed_catalog": bool(retained),
                "saved_fields_verified": activity.get("saved_fields_verified"),
                "saved_fields_to_verify": activity.get("saved_fields_to_verify"),
                "processed_fields": activity.get("processed_fields", saved.get("processed_fields", 0)),
                "indexed_fields": saved.get("processed_fields", 0), "total_fields": saved.get("total_fields", 0),
                "algorithm_counts": retained.get("counts", saved.get("counts", {})), "source_count": len(rows),
                "potential_count": sum(not r["human_excluded"] and r['disposition'] in ('shortlist', 'uncertain') for r in rows),
                "human_excluded_count": sum(r["human_excluded"] for r in rows), "rows": rows, "warnings": warnings,
                "live_read_warnings": list(self._live_warnings.values()),
                "scope": "Saved region-level sieve results. Tiny or off-domain regions omitted before measurement and unassessable fields are not in this region index." if include_all else "Saved shortlist and uncertain proposals; categories are provisional, not confirmed flakes"}

    def candidate_page(self, review_store, category='potential', offset=0, limit=48, chip='', reason='', *, plot=False, selection=None):
        categories = ('potential', 'shortlist', 'uncertain', 'rejected', 'bulk_or_clipped', 'all', 'excluded')
        if category not in categories or not 0 <= offset <= 10_000_000 or not 1 <= limit <= 96:
            raise ValueError('Invalid candidate filter or page bounds')
        if reason not in ('', 'COLOR', 'EDGE', 'TEXTURE', 'GEOMETRY', 'COLOR_UNAVAILABLE', 'COLOR_UNCERTAIN'):
            raise ValueError('Invalid sieve-reason filter')
        # Wide review queues are built lazily. Subsequent refreshes read only new
        # committed field JSON, not all raw images or the complete result set.
        include_all = category in ('rejected', 'bulk_or_clipped', 'all', 'excluded') or self._candidate_index.has_cached_fields
        result = self.candidates(review_store, include_all=include_all)
        rows = result['rows']
        chip_ids = sorted({f['chip_id'] for f in self.catalog()['fields']})
        if chip and chip not in chip_ids:
            raise ValueError('Unknown acquired chip')
        chosen = [r for r in rows if not chip or r.get('chip_id') == chip]

        def selected(row, key):
            if key == 'all': return True
            if key == 'excluded': return row['human_excluded']
            if key == 'potential': return not row['human_excluded'] and row['disposition'] in ('shortlist', 'uncertain')
            # A human review does not erase the recorded algorithm decision.
            if key in ('rejected', 'bulk_or_clipped'): return row['disposition'] == key
            return not row['human_excluded'] and row['disposition'] == key

        counts = {k: sum(selected(r, k) for r in chosen) for k in categories}
        if not include_all:
            # Fast survivor views need not open thousands of rejected shards to
            # report counts already committed in the ledger.
            ledger = self._live_json(self.campaign / 'analysis-residue-sieves-campaign-01/ledger.json')
            totals = {}
            for key, entry in ledger.items():
                if chip and not key.startswith(chip + '/'): continue
                for disposition, count in entry.get('summary', {}).get('counts', {}).items():
                    totals[disposition] = totals.get(disposition, 0) + count
            for key in ('rejected', 'bulk_or_clipped'):
                counts[key] = totals.get(key, 0)
            counts['all'] = sum(totals.values()) or len(chosen)

        def reason_matches(row):
            if not reason: return True
            name, status = ('COLOR', 'unavailable') if reason == 'COLOR_UNAVAILABLE' else ('COLOR', 'uncertain') if reason == 'COLOR_UNCERTAIN' else (reason, 'fail')
            return any(s.get('name') == name and s.get('status') == status for s in row.get('sieves') or [])

        filtered = [r for r in chosen if selected(r, category) and reason_matches(r)]
        # Plot exactly the gallery population before brushing, across every
        # page. Reusing saved contrasts requires no image decoding or analysis.
        # Keep this context unchanged when selecting an empty/small rectangle.
        result['unbrushed_count'] = len(filtered)
        result['contrast_selection'] = selection
        if plot:
            points = []
            for row in filtered:
                values = rgb_contrast(row)
                if values is not None:
                    color = next((s for s in row.get('sieves') or [] if s.get('name') == 'COLOR'), {})
                    points.append(dict(id=row['id'], field=row['field'], c=values,
                                       disposition=row['disposition'], color_status=color.get('status', 'unavailable')))
            result['contrast_plot'] = dict(points=points, population_count=len(filtered), plotted_count=len(points),
                missing_count=len(filtered)-len(points), convention='(substrate - feature) / substrate; positive means darker')
        if selection:
            filtered = [r for r in filtered if in_contrast_selection(rgb_contrast(r), selection)]
        # Keep the strongest saved ranks first so borderline rejections are easy
        # to audit. No score, threshold or disposition is changed by this view.
        filtered.sort(key=lambda r: (-float(r.get('rank') or 0), r['field'], r['id']))
        if category in ('potential', 'all'):
            filtered.sort(key=lambda r: r['disposition'] != 'shortlist')
        count = len(filtered)
        offset = min(offset, ((count - 1) // limit) * limit if count else 0)
        result.update(rows=filtered[offset:offset+limit], filtered_count=count,
                      offset=offset, limit=limit, filter=category, filter_counts=counts,
                      chip_ids=chip_ids, reason=reason, chip=chip,
                      indexed_source_count=result['source_count'])
        return result


def serve_suite(project_root, port=8770, campaign_rel="data/whole-chip-standardized-01", *, provider=None, scan_service=None):
    catalog = SuiteCatalog(project_root, campaign_rel, provider)
    root = catalog.root
    session_token = secrets.token_urlsafe(32)
    template = Path(__file__).with_name("suite.html")
    stores = {}
    def scans():
        nonlocal scan_service
        if scan_service is None:
            from .scan_service import ScanService
            scan_service = ScanService(root)
        return scan_service
    def store():
        dataset = catalog.catalog()["dataset_id"]
        # Dataset identity comes from the trusted catalog but still cannot form a path.
        if not isinstance(dataset, str) or dataset in ("", ".", "..") or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for c in dataset):
            raise ValueError("Unsafe dataset identifier")
        if dataset not in stores:
            stores[dataset] = ReviewEventStore(root / "annotations" / dataset)
        return stores[dataset]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, value, kind="application/json; charset=utf-8"):
            body = value if isinstance(value, bytes) else json.dumps(value, allow_nan=False).encode()
            self.send_response(status)
            for key, val in {"Content-Type": kind, "Content-Length": str(len(body)), "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin", "X-Frame-Options": "DENY"}.items():
                self.send_header(key, val)
            self.end_headers(); self.wfile.write(body)

        def trusted_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            if not self.trusted_host():
                return self.reply(403, {"error": "Loopback Host required"})
            path = unquote(urlsplit(self.path).path)
            query = parse_qs(urlsplit(self.path).query)
            try:
                if path == "/":
                    return self.reply(200, template.read_bytes(), "text/html; charset=utf-8")
                if path == "/scan":
                    return self.reply(200, template.with_name('scan_console.html').read_bytes(), "text/html; charset=utf-8")
                if path == "/graphene":
                    return self.reply(200, template.with_name('graphene_review.html').read_bytes(), "text/html; charset=utf-8")
                if path == "/api/graphene":
                    from .graphene_review import GrapheneReview
                    if not hasattr(self.server, 'graphene_review'):
                        self.server.graphene_review = GrapheneReview(root)
                    return self.reply(200, self.server.graphene_review.data(query.get('run',[''])[0],
                        category=query.get('filter',['shortlist'])[0], chip=query.get('chip',[''])[0],
                        field=query.get('field',[''])[0], offset=int(query.get('offset',['0'])[0]),
                        limit=int(query.get('limit',['48'])[0]), minimum_area=float(query.get('min_area',['25'])[0])))
                if path == "/api/scan":
                    return self.reply(200, scans().snapshot())
                if path == "/api/session":
                    return self.reply(200, {"token": session_token, "read_only_hardware": True, "label_sync": "local Drive filesystem; remote sync unverified"})
                if path == "/api/catalog":
                    return self.reply(200, catalog.catalog())
                if path == "/api/candidates":
                    if 'filter' in query:
                        plot = query.get('plot', ['0'])[0]
                        if plot not in ('0', '1'):
                            raise ValueError('Invalid contrast plot option')
                        return self.reply(200, catalog.candidate_page(store(), query['filter'][0],
                            int(query.get('offset', ['0'])[0]), int(query.get('limit', ['48'])[0]),
                            query.get('chip', [''])[0], query.get('reason', [''])[0],
                            plot=plot == '1', selection=contrast_selection(query)))
                    return self.reply(200, catalog.candidates(store()))
                if path == "/api/field":
                    result = catalog.field(query["key"][0])
                    if result["labeling_available"]:
                        result["review"] = store().read(result["source_identity"])
                        current = {r["id"]: r for r in result["rows"]}
                        for rid, review in result["review"]["regions"].items():
                            original = review.get("source_proposal", {}).get("proposal_fingerprint")
                            review["stale_source_proposal"] = bool(original and original != current.get(rid, {}).get("proposal_fingerprint"))
                    return self.reply(200, result)
                if path == "/api/status":
                    return self.reply(200, catalog.status())
                if path == "/api/contrast":
                    from .contrast_plot import ContrastDistribution
                    if not hasattr(self.server, "distribution"):
                        self.server.distribution = ContrastDistribution(catalog.campaign)
                    return self.reply(200, self.server.distribution.data(query.get("since", [None])[0]))
                if path == "/api/export":
                    return self.reply(200, store().export())
                if path == "/api/material-profiles":
                    config = Path(__file__).resolve().parent.parent / "configs/materials"
                    registry = read_json(config / "registry.json", {})
                    profiles = []
                    for material in registry.get("materials", {}).values():
                        for name in material.get("available_profiles", []):
                            data = read_json(inside(config, name))
                            if data:
                                profiles.append(data)
                    return self.reply(200, {"registry": registry, "profiles": profiles, "use": "Next-dataset planning only; current data are unchanged"})
                if path.startswith("/files/"):
                    target = inside(root, path[len("/files/"):])
                    if target.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp") or not target.is_file():
                        raise KeyError("Display asset unavailable")
                    return self.reply(200, target.read_bytes(), mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                return self.reply(404, {"error": "Unknown offline route"})
            except (RevisionConflict, SourceIdentityError) as error:
                return self.reply(409, {"error": str(error)})
            except KeyError as error:
                return self.reply(404, {"error": str(error)})
            except (OSError, ValueError, TypeError) as error:
                return self.reply(400, {"error": str(error)})

        def do_POST(self):
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if not self.trusted_host() or self.headers.get("Origin") != origin or self.headers.get("X-Review-Token") != session_token:
                return self.reply(403, {"error": "Same-origin review token required"})
            if self.path not in ("/api/review-events", "/api/training-export", "/api/scan/preflight", "/api/scan/plan", "/api/scan/start", "/api/scan/check", "/api/scan/stop"):
                return self.reply(404, {"error": "No such offline operation"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 2_000_000 or not self.headers.get("Content-Type", "").startswith("application/json"):
                    raise ValueError("Expected bounded JSON request")
                body = json.loads(self.rfile.read(size))
                if self.path.startswith('/api/scan/'):
                    if not isinstance(body, dict): raise ValueError('Expected a scan request object')
                    operation = self.path.rsplit('/', 1)[1]
                    if operation == 'preflight': result = scans().preflight(body.get('settings'))
                    elif operation == 'plan': result = scans().plan(body.get('settings'))
                    elif operation == 'start': result = scans().start(body.get('run_id'), release_hold=body.get('release_hold',False))
                    elif operation == 'check': result = scans().start(body.get('run_id'), release_hold=body.get('release_hold',False), prepare_only=True)
                    else: result = scans().stop(body.get('run_id'))
                    return self.reply(200, result)
                if self.path == "/api/training-export":
                    if not catalog.provider:
                        raise ValueError("Training export requires the identity-checked project catalog")
                    include = body.get("include_previews", False)
                    if type(include) is not bool:
                        raise ValueError("include_previews must be boolean")
                    from .training_bundle import export_bundle
                    relative = "training/"+time.strftime("%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:12]
                    result = export_bundle(catalog.provider, store().export(), inside(root, relative), include_previews=include)
                    return self.reply(200, {"output_rel": relative, "summary": result, "sync": "Local write; cloud synchronization unverified"})
                source = identity(body["source_identity"])
                current = catalog.field(source["field_key"])
                if not current["labeling_available"] or source != current["source_identity"]:
                    raise SourceIdentityError("Source identity changed; reopen this field")
                by_id = {row["id"]: row for row in current["rows"]}
                for operation in body["operations"]:
                    operation.pop("source_proposal", None)
                    operation.pop("prior_human_review", None)
                    region = by_id.get(operation.get("region_id"))
                    if region and operation.get("kind") in ("annotate", "set_geometry"):
                        if operation.get("proposal_fingerprint") != region["proposal_fingerprint"]:
                            raise SourceIdentityError("Machine proposal geometry changed; reopen before labeling")
                        operation["source_proposal"] = {k: region.get(k) for k in ("id", "polygons_px", "bbox_px", "proposal_fingerprint", "mask_sha256", "source_mask_sha256", "instance_label")}
                    if operation.get("import_prior_review"):
                        prior = next((r for r in current.get("prior_human_reviews", []) if r["id"] == operation.get("region_id")), None)
                        if not prior or operation.get("labels", {}).get("semantic") != prior["semantic"]:
                            raise SourceIdentityError("No matching hash-verified direct-user review")
                        operation["prior_human_review"] = prior
                result = store().append(source, expected_revision=body["expected_revision"], operations=body["operations"],
                                        actor=body["actor"], client_event_id=body["client_event_id"], machine_region_ids=by_id)
                return self.reply(200, result)
            except (RevisionConflict, SourceIdentityError) as error:
                return self.reply(409, {"error": str(error), "reload_required": True})
            except (OSError, ValueError, TypeError, KeyError) as error:
                return self.reply(400, {"error": str(error)})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/"


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Offline image and label workbench; no hardware endpoints")
    parser.add_argument("project_root")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--campaign", default="data/whole-chip-standardized-01")
    args = parser.parse_args()
    server, url = serve_suite(args.project_root, args.port, args.campaign)
    print(url, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown(); server.server_close()
