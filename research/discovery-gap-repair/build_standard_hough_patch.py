"""Build an isolated source candidate and unified diff; never edit production."""
from pathlib import Path
import difflib
import hashlib
import json

root = Path(__file__).resolve().parents[2]
out = Path(__file__).resolve().parent
source = root/'flakefinder/sparse_edges.py'
before = source.read_text(encoding='utf-8')
helper = (out/'standard_hough_fallback.py').read_text(encoding='utf-8')
helper = helper[helper.index('def _standard_hough_candidate'):]
after = before.replace('def coherent_edge(', helper+'\n\ndef coherent_edge(', 1)
early = '    if lines is None:\n        return None\n    yy, xx = np.mgrid[:h, :w]\n    best = None\n    for x1, y1, x2, y2 in lines.reshape(-1, 4):'
assert after.count(early) == 1
after = after.replace(early, '    yy, xx = np.mgrid[:h, :w]\n    best = None\n    for x1, y1, x2, y2 in (() if lines is None else lines.reshape(-1, 4)):', 1)
hook = '    if best is None:\n        return None\n    _, points, normal, ps, ph, residual = best'
assert after.count(hook) == 1
after = after.replace(hook, '    if best is None:\n        best = _standard_hough_candidate(edges, s, b)\n'+hook, 1)
compile(after, str(source), 'exec')
(out/'proposed_sparse_edges.py').write_text(after, encoding='utf-8')
(out/'sparse-edges-standard-hough.patch').write_text(''.join(difflib.unified_diff(
    before.splitlines(True), after.splitlines(True), fromfile='a/flakefinder/sparse_edges.py',
    tofile='b/flakefinder/sparse_edges.py')), encoding='utf-8')
record = dict(baseline_source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
              candidate_source_sha256=hashlib.sha256((out/'proposed_sparse_edges.py').read_bytes()).hexdigest(),
              changed_production_file=False, separate_gap_patch_target='flakefinder/sparse_acquisition.py',
              fallback_max_proposals=12, production_source=str(source))
(out/'standard-hough-patch-metadata.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
print(json.dumps(record, indent=2))
