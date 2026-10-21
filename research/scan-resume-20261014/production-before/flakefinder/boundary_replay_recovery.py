"""Read-only, hash-exact recovery of missing derived coverage artifacts.

Original manifests, TIFFs and absent metadata stay unchanged. Vendor archives
can independently establish registration; they are never presented as a
reconstructed camera sidecar or an invented acquisition timestamp.
"""
from pathlib import Path
import base64,hashlib,io,json,re
import xml.etree.ElementTree as ET
import numpy as np

from .model import SafetyError
from .vendor_lifecycle import file_hash


def _read(path):return json.loads(Path(path).read_text(encoding='utf-8'))


class BoundaryArtifactRecovery:
    def __init__(self,run,chip_id,state,plan,manifest):
        self.run=Path(run);self.folder=self.run/chip_id;self.state=state
        self.plan=plan;self.manifest=manifest;self.events=[];self.reference=None
        request=_read(self.run/'scan-request.json');visited=set();release=None
        while True:
            contract=request.get('rectangle_handoff',{})
            releases=[request.get('source_release',{}),contract.get('source_release',{})]
            release=next((v for v in releases if v.get('files',{}).get('full_chip.py')==manifest.get('algorithm_source_sha256')),None)
            if release is not None:break
            prior=contract.get('prior_run_id','')
            if not re.fullmatch(r'scan-\d{8}-\d{6}-[a-f0-9]{8}',prior) or prior in visited:break
            path=self.run.parent/prior/'scan-request.json'
            expected=contract.get('prior_files_sha256',{}).get('scan-request.json')
            if not expected or not path.is_file() or file_hash(path)!=expected:
                raise SafetyError('Pinned acquisition source lineage changed.')
            visited.add(prior);request=_read(path)
        if release is None:raise SafetyError('Missing-artifact replay has no pinned acquisition source release.')
        self.acquisition_source=manifest.get('algorithm_source_sha256')
        self.sources={name:release['files'].get(name) for name in ('chip_discovery.py','layout.py','imaging.py','model.py')}
        self.missing=[{'path':relative,'expected_sha256':digest} for row in manifest['completed'].values()
                      for relative,digest in row.get('hashes',{}).items() if not (self.folder/relative).exists()]

    def bundled(self,relative,row):
        if not row.get('sidecar_backup'):return None
        from .tile_sidecar_backup import inspect_tile_sidecar_backup
        value=inspect_tile_sidecar_backup(self.folder,row).get(relative)
        if value is not None:
            self.events.append({'kind':'exact_original_sidecar_bundle_in_memory','missing_path':relative,
                'sha256':row['hashes'][relative],'bundle':row['sidecar_backup'],'original_files_modified':False})
        return value

    def verified(self,relative,row):
        path=(self.folder/relative).resolve()
        if not path.is_relative_to(self.folder.resolve()):raise SafetyError('Recovery evidence escapes chip folder.')
        digest=row.get('hashes',{}).get(relative)
        if not digest or not path.is_file() or file_hash(path)!=digest:
            raise SafetyError('Missing-artifact recovery input absent or changed: '+relative)
        return path,digest

    def registration(self,name,row):
        """Use independently retained vendor XYZ only when the sidecar is absent."""
        relative=f'tiles/{name}.json'
        if (self.folder/relative).exists():raise SafetyError('Existing metadata must use normal strict verification.')
        raw,raw_hash=self.verified(f'tiles/{name}.tiff',row)
        payload=self.bundled(relative,row)
        if payload is not None:return json.loads(payload)['position']
        archive,archive_hash=self.verified(f'tiles/{name}.zen-vendor.json',row)
        value=_read(archive);capture=value.get('capture',{})
        if (value.get('status')!='removed_after_verified_raw_save' or value.get('raw_tiff_sha256')!=raw_hash
                or value.get('raw_sidecar_sha256')!=row.get('hashes',{}).get(relative)
                or capture.get('job_completed') is not True or capture.get('job_unloaded') is not True
                or not capture.get('adapter_session_id') or not capture.get('capture_id')
                or capture.get('raw_shape')!=self.plan['camera_shape']):
            raise SafetyError('Vendor registration archive does not bind the original raw capture.')
        embedded=value.get('vendor_metadata_base64',{})
        names=[key for key in embedded if key.endswith('.czi.metadata')]
        if len(names)!=1:raise SafetyError('Vendor registration needs one exact original image metadata record.')
        data=base64.b64decode(embedded[names[0]],validate=True)
        inventory=next((v for v in value.get('inventory',[]) if v.get('name')==names[0]),{})
        if hashlib.sha256(data).hexdigest()!=inventory.get('sha256'):
            raise SafetyError('Archived vendor image metadata differs from its inventory.')
        props={item.attrib['key']:item.attrib.get('value','') for item in ET.fromstring(data).iter('Property')}
        def numeric(key):
            match=re.fullmatch(r'\s*([-+\d.,eE]+)\s*(?:\u00b5m|ms|%)?\s*',props.get(key,''))
            if not match:raise SafetyError('Missing numeric vendor registration property: '+key)
            return float(match[1].replace(',',''))
        position=dict(x=numeric('Image.StageXPosition'),y=numeric('Image.StageYPosition'),z=numeric('Image.FocusPosition'))
        if any(not np.isfinite(v) or abs(v-row['position'][axis])>.01 for axis,v in position.items()):
            raise SafetyError('Archived vendor XYZ differs from committed field registration.')
        settings=self.plan['acquisition_settings'];exposure=settings.get('camera_resources',{}).get('ff_exposure_ms')
        if (exposure is None or abs(numeric('Detector.ExposureTime')-exposure)>.001
                or numeric('Instrument.ObjectiveMagnification')!=20
                or abs(numeric('Image.Channel.LightSourceIntensity')-settings['illumination_percent'])>.02
                or [numeric('Image.SizeY'),numeric('Image.SizeX'),3]!=self.plan['camera_shape']):
            raise SafetyError('Archived vendor optics/profile differs from the committed coverage plan.')
        proof={'kind':'independent_archived_vendor_registration','tile':name,'missing_sidecar':relative,
               'original_sidecar_sha256':row['hashes'][relative],'raw_sha256':raw_hash,'archive_sha256':archive_hash,
               'vendor_metadata_sha256':inventory['sha256'],'position_um':position,
               'original_sidecar_restored':False,'acquisition_timestamp_synthesized':False}
        self.events.append(proof)
        return position

    def labels(self,name,row):
        """Recompute one missing label blob, accepting only its original byte hash."""
        from .imaging import load_frame
        from .layout import SubstrateReference
        from .chip_discovery import BoundaryOptions,classify_boundary
        relative=f'boundary-labels/{name}.npz'
        if (self.folder/relative).exists():raise SafetyError('Existing labels must use normal strict verification.')
        raw,raw_hash=self.verified(f'tiles/{name}.tiff',row)
        payload=self.bundled(relative,row)
        if payload is not None:
            with np.load(io.BytesIO(payload),allow_pickle=False) as stored:return stored['labels'].copy()
        for source_name,digest in self.sources.items():
            if not digest or file_hash(Path(__file__).with_name(source_name))!=digest:
                raise SafetyError('Missing-artifact replay source differs from acquisition: '+source_name)
        _,metadata_hash=self.verified(f'tiles/{name}.json',row)
        reference_path=Path(self.state['reference_path'])
        if file_hash(reference_path)!=self.state['reference_sha256']:
            raise SafetyError('Recovery reference raw differs from the campaign.')
        if self.reference is None:
            frame=load_frame(reference_path);reference=SubstrateReference.from_frame(frame)
            if reference.context!=self.state['reference_context']:
                raise SafetyError('Recovery reference optical context differs from the campaign.')
            self.reference=(reference,np.median(frame.rgb.reshape(-1,3),axis=0))
            del frame
        frame=load_frame(raw)
        if any(abs(getattr(frame.position,a)-row['position'][a])>.001 for a in 'xyz'):
            raise SafetyError('Recovery raw metadata position differs from committed tile.')
        cell=float(self.plan['support_evidence']['support_cell_um']);x0,x1,y0,y1=self.plan['scan_plan']['bounds_um']
        options=BoundaryOptions([x0-4*cell,x1+4*cell,y0-4*cell,y1+4*cell],cell_um=cell)
        labels=classify_boundary(frame,*self.reference,options);del frame
        stream=io.BytesIO();np.savez_compressed(stream,labels=labels)
        digest=hashlib.sha256(stream.getvalue()).hexdigest()
        if digest!=row.get('hashes',{}).get(relative):
            raise SafetyError('Recomputed boundary labels do not match their original committed hash.')
        self.events.append({'kind':'exact_original_boundary_labels_in_memory','tile':name,'missing_path':relative,
            'recomputed_sha256':digest,'raw_sha256':raw_hash,'metadata_sha256':metadata_hash,
            'reference_sha256':self.state['reference_sha256'],'classification_source_sha256':self.sources,
            'original_acquisition_source_sha256':self.acquisition_source,'original_files_modified':False})
        return labels

    def report(self):
        return {'missing_manifest_artifacts':self.missing,'replay_recoveries':self.events,
                'original_manifest_and_raw_modified':False,'missing_files_restored':False}
