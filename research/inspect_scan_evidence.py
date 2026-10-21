"""Read-only, low-resident inspection of saved scan evidence; no hardware imports."""
import json
import sys
from pathlib import Path

PROJECT = Path(r"G:\Shared drives\Xu Lab - Microscopy and Instruments\Automated Microscopy")
SOURCE = Path(r"C:\Users\STM\Documents\ChatGPT\Automated Microscopy\research\software-next-20261009")
pointer = json.loads((PROJECT / "scan-control/current.json").read_text(encoding="utf-8-sig"))
print("POINTER", json.dumps(pointer))
run = PROJECT / "data" / pointer["id"]
for arg in sys.argv[1:]:
    root, _, rel = arg.partition(":")
    base = {"run": run, "source": SOURCE, "project": PROJECT}[root]
    bounds = None
    if "#lines=" in rel:
        rel, spec = rel.split("#lines=", 1)
        bounds = tuple(map(int, spec.split("-")))
    compact = rel.endswith("#compact")
    rel = rel.removesuffix("#compact")
    path = base / rel
    print("PATH", str(path))
    if path.is_dir():
        print(json.dumps([str(p.relative_to(path)) for p in path.iterdir()]))
    else:
        content = path.read_text(encoding="utf-8-sig")
        if bounds:
            print("\n".join(f"{i+1}: {line}" for i, line in enumerate(content.splitlines()) if bounds[0] <= i+1 <= bounds[1]))
        elif compact:
            value = json.loads(content)
            records = value.pop("records", [])
            print(json.dumps({k: value.get(k) for k in ("state", "rays", "outline", "operation_budget", "reason", "failure")}))
            for row in records:
                ev = row.get("edge_validation") or {}
                print(json.dumps({**{k: row.get(k) for k in ("name", "phase", "position_um", "classification", "fractions")},
                    "edge": bool(row.get("edge")), "validation": {k: ev.get(k) for k in ("accepted", "reason", "supported_profiles", "inlier_profiles", "step_polarity", "polarity_evidence")}}))
        else:
            print(content)
