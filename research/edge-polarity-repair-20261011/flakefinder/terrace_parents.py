"""Read checksummed, previously reviewed source masks without changing them."""
from pathlib import Path
import hashlib
import json

import cv2
import numpy as np

from .model import AcquisitionError
from .terrace_domain import digest


def _member(root,relative):
    value=Path(relative)
    if value.is_absolute():raise AcquisitionError('Parent-analysis member must be relative.')
    target=(root/value).resolve()
    if not target.is_relative_to(root):raise AcquisitionError('Parent-analysis member escapes its folder.')
    return target


def load_parent_field(analysis_folder,item,frame):
    root=Path(analysis_folder).resolve();summary_path=root/'analysis.json'
    summary=json.loads(summary_path.read_text(encoding='utf-8'))
    matches=[t for t in summary['tiles'] if t['key']==item['key']]
    if len(matches)!=1:raise AcquisitionError('No unique successful accepted parent field; no global-foreground fallback.')
    tile=matches[0];shard_path=_member(root,tile['observations_file'])
    shard=json.loads(shard_path.read_text(encoding='utf-8'))
    if shard['tile']['key']!=item['key']:raise AcquisitionError('Parent shard tile identity mismatch.')
    source_hashes=shard.get('provenance',{}).get('source_files_sha256',{})
    for suffix in ('.tiff','.json'):
        if source_hashes.get(suffix)!=item['source_hashes'][suffix]:raise AcquisitionError('Parent mask was derived from different source bytes.')
    if tile.get('sha256')!=hashlib.sha256(frame.rgb.tobytes()).hexdigest():raise AcquisitionError('Parent raw-pixel identity mismatch.')
    mask_path=_member(root,tile['instance_mask']);mask_sha=digest(mask_path)
    if mask_sha!=shard.get('mask_sha256'):raise AcquisitionError('Accepted parent mask checksum mismatch.')
    labels=cv2.imread(str(mask_path),cv2.IMREAD_UNCHANGED)
    if labels is None or labels.ndim!=2 or labels.shape!=frame.rgb.shape[:2] or labels.dtype.kind not in 'ui':
        raise AcquisitionError('Invalid accepted source mask shape/type.')
    rows=shard['rows'];row_labels=[int(r['instance_label']) for r in rows]
    if len(set(row_labels))!=len(rows) or set(row_labels)!=(set(np.unique(labels))-{0}):
        raise AcquisitionError('Parent rows do not match exact source instance labels.')
    provenance={'analysis_folder_name':root.name,'analysis_dataset_id':summary.get('dataset_id'),
        'analysis_fingerprint':shard.get('fingerprint'),'field_key':item['key'],
        'field_sha256':digest(shard_path),'instance_mask_sha256':mask_sha,
        'instance_mask_relative_path':tile['instance_mask'],
        'source_identity':shard.get('source_identity'),'source_files_sha256':source_hashes,
        'geometry_preserved':'Exact accepted mask labels, parent IDs, polygons and holes; child sieves cannot modify them.'}
    return np.int32(labels),rows,provenance
