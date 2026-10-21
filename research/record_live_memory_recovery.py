"""Record verified saved evidence only; never control acquisition."""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

project = Path(r"G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy")
source = Path(r"C:\Users\STM\Documents\ChatGPT\Automated Microscopy\research\software-next-20261009")
def read(path): return json.loads(path.read_text(encoding="utf-8-sig"))
def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".supervision.tmp")
    temp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    os.replace(temp, path)
identifier = read(project / "scan-control/current.json")["id"]
if identifier != "scan-20261011-130111-bf685ad5":
    raise RuntimeError("Run changed; reassess before recording this recovery")
run = project / "data" / identifier
status = read(run / "scan-status.json")
receipt_path = run / "discovery/candidate001/edge-focus/attempt_000/focus-reuse.json"
receipt = read(receipt_path)
assert receipt["accepted"] is True
report_path = project / "research/session-20261011-memory-footprint/repair-report.json"
report = read(report_path)
source_matches = {p: hashlib.sha256((source / p).read_bytes()).hexdigest() == hashes["after"]
                  for p, hashes in report["source_hashes"].items()}
now = datetime.now(timezone.utc).isoformat()
snapshot = read(run / "supervision/low-memory-snapshot.json")
discoveries = snapshot["discoveries"]
verification = {"at": now, "snapshot_at": snapshot["at"], "worker_pid": status["pid"],
    "phase": status["phase"], "state": status["state"], "initial_focus_reused": True,
    "local_focus_reused": receipt, "fresh_confirmation_images": 1,
    "previous_local_search_capture_count": 23,
    "guard_working_set_bytes": snapshot["guard_working_set_bytes"],
    "available_bytes": snapshot["available_bytes_now"], "source_hashes_unchanged": source_matches,
    "memory_recovery": snapshot["memory"], "discoveries": discoveries,
    "qualified_conclusion": "Focus reuse and acquisition recovery are verified. RAM remains marginal and complete chip geometry/raster coverage is unresolved."}
report.update(state="acquisition_recovered_geometry_unresolved", live_verification=verification)
write(report_path, report)
diagnosis = {"at": now, "run_id": identifier,
    "candidate001_outcome": "registered_substrate_contradicts_halfplanes",
    "observations": [
        "Candidate001 completed a bounded 29-probe search without an accepted chip outline.",
        "Seed west-side edge was accepted at Z15674.765; later west-side frames at Z15743.124 are visibly blurred and fail raw support.",
        "East local focus independently confirmed at Z15743.124; its bright bevel/dark transition produces mixed raw profile support and no accepted straight line.",
        "North and south rays reached their 9mm local search limits without an outer holder bracket.",
        "Candidate002 began at a different XY and accepted a fresh seed edge; it has not yet exhausted its own mapping budget."],
    "source_findings": [
        "sparse_acquisition.focus_if_needed currently permits only one local focus attempt per candidate.",
        "Subsequent XY probes inherit that last focus height; the confirmed local height is not a measured chip-wide surface.",
        "Registered-substrate contradiction is a geometry failure; it must not be relabeled successful coverage."],
    "evidence": [str(run / "discovery/candidate001/frames" / (name + ".jpg")) for name in
                 ("probe_000000", "probe_000005", "probe_000020")],
    "next_action": "Inspect candidate002 within its saved budget. On repeated unresolved geometry, cooperatively checkpoint/close the sole worker, then replay saved frames and validate spatial focus selection and bevel-edge handling offline before continuation. Never loosen raw gates merely to count a chip.",
    "hardware_action_taken": False, "live_source_edited": False}
write(run / "supervision/geometry-diagnosis.json", diagnosis)
repair_status = {"at": now, "state": report["state"], "run_id": identifier,
    "worker_pid": status["pid"], "phase": status["phase"],
    "summary": "Acquisition resumed. Saved local edge focus passed one fresh confirmation, avoiding a repeated 23-image search. Native watcher measured about20MB. First candidate geometry is unresolved; a different second candidate is being mapped. RAM remains marginal.",
    "completed_chip_outlines": 0, "raster_tiles": 0, "interval_minutes": 1,
    "verification_report": str(report_path), "geometry_diagnosis": str(run / "supervision/geometry-diagnosis.json")}
write(run / "supervision/repair-status.json", repair_status)
cadence = read(run / "supervision/cadence.json")
cadence.update(at=now, minutes=1, reason="Memory remains marginal and first candidate geometry failed; second candidate still within saved mapping budget.",
               previous_observation={"at": snapshot["at"], "discoveries": [{k:d.get(k) for k in
                  ("candidate", "state", "records", "accepted_edges", "outline", "budget")} for d in discoveries]})
write(run / "supervision/cadence.json", cadence)
print(json.dumps({"state": report["state"], "all_source_hashes_match": all(source_matches.values()),
                  "status_file": str(run / "supervision/repair-status.json")}))
