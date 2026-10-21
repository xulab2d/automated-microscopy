"""Summarize saved acquisition timings and benchmark local frame decoding only."""
from pathlib import Path
import json
import re
import sys
import time
from datetime import datetime
import xml.etree.ElementTree as ET

import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'generated'))
from flakefinder.imaging import load_frame
from flakefinder.zen import decode_frame
from zen_api.acquisition.v1beta.experiment_streaming_service_pb2 import ExperimentStreamingServiceMonitorAllExperimentsResponse


def stats(values):
    return {'n':len(values),'median_s':float(np.median(values)),'p90_s':float(np.percentile(values,90))}


def vendor_log_timings():
    folder=Path(r'C:\ProgramData\Carl Zeiss\Logging')
    jobs=[];current=None
    for filename in ('ZENCore.log.xml.1','ZENCore.log.xml'):
        path=folder/filename
        if not path.exists():continue
        for line in path.read_text(encoding='utf-8-sig',errors='replace').splitlines():
            if 'WorkflowApiManager' not in line:continue
            try:entry=ET.fromstring(line)
            except ET.ParseError:continue
            message=entry.findtext('message') or '';stamp=entry.attrib.get('timestamp')
            if 'ExecuteJobAsync(): Started job: FlakeFinder Quantitative Camera' in message:
                current={'start':stamp}
            elif current is not None:
                if 'job execution has completed successfully' in message:current['execution_done']=stamp
                elif 'Purging unreferenced results' in message:current['purge_start']=stamp
                elif 'Finalizing the job results at path:' in message:current['finalize_start']=stamp
                elif 'Finalization has been completed' in message:
                    current['finalize_done']=stamp
                    if all(k in current for k in ('execution_done','purge_start','finalize_start')):
                        points={k:datetime.fromisoformat(v) for k,v in current.items()}
                        current['seconds']={
                            'job_execution':(points['execution_done']-points['start']).total_seconds(),
                            'purge_before_file_finalization':(points['finalize_start']-points['purge_start']).total_seconds(),
                            'file_finalization':(points['finalize_done']-points['finalize_start']).total_seconds(),
                            'post_execution_finalization':(points['finalize_done']-points['execution_done']).total_seconds(),
                        }
                        jobs.append(current)
                    current=None
    jobs=jobs[-100:]
    camera=[];pending=None;path=folder/'axcam.log'
    if path.exists():
        with path.open('rb') as stream:
            stream.seek(0,2);stream.seek(max(0,stream.tell()-4*1024*1024))
            lines=stream.read().decode('utf-8',errors='replace').splitlines()
        for line in lines:
            if 'CAM#0 ' not in line:continue
            try:stamp=datetime.strptime(line[:23],'%Y-%m-%d %H:%M:%S.%f')
            except ValueError:continue
            if 'McammAcquisitionEx setup:' in line:
                match=re.search(r'exposure=(\d+)',line)
                pending=(stamp,int(match.group(1))) if match else None
            elif pending and 'axcamPollEndOfAcq ' in line:
                duration=(stamp-pending[0]).total_seconds()
                if 0<duration<10:camera.append({'start':str(pending[0]),'end':str(stamp),'exposure_ms':pending[1]/1000,'seconds':duration})
                pending=None
    camera=[r for r in camera if r['exposure_ms']==200][-100:]
    return {'source_logs':[str(folder/x) for x in ('ZENCore.log.xml.1','ZENCore.log.xml','axcam.log')],
        'job_summary':{k:stats([r['seconds'][k] for r in jobs]) for k in jobs[0]['seconds']} if jobs else {},
        'camera_200ms_setup_to_poll_complete':stats([r['seconds'] for r in camera]) if camera else {},
        'jobs':jobs,'camera_intervals':camera,
        'qualification':'Sequential events paired from saved local logs, most recent 100 complete intervals. AXCAM interval includes exposure and readout/driver completion; not a pure exposure measurement. Job finalization is counted after workflow execution completes.'}


def main():
    campaign=ROOT/'data/whole-chip-standardized-01'
    rows=[]
    for path in campaign.rglob('*.json'):
        if not path.with_suffix('.tiff').is_file():continue
        try:d=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError):continue
        meta=d.get('metadata',{});timings=meta.get('acquisition_timing_s',{})
        if timings:rows.append({'file':str(path.relative_to(ROOT)),'exposure_ms':meta.get('camera_resources',{}).get('ff_exposure_ms'),'timings':timings})
    summary={key:stats([r['timings'][key] for r in rows if key in r['timings']])
             for key in sorted({k for r in rows for k in r['timings']})}
    by_exposure={str(exposure):{key:stats([r['timings'][key] for r in rows if r['exposure_ms']==exposure and key in r['timings']])
        for key in summary if any(key in r['timings'] for r in rows if r['exposure_ms']==exposure)}
        for exposure in sorted({r['exposure_ms'] for r in rows if r['exposure_ms'] is not None})}
    original=json.loads((ROOT/'data/raster-performance-audit-01/report.json').read_text())
    frame=load_frame(ROOT/original['source'])
    message=ExperimentStreamingServiceMonitorAllExperimentsResponse();f=message.frame_data
    f.experiment_id='offline-saved-frame';f.frame_number=frame.sequence
    f.frame_size.width=frame.rgb.shape[1];f.frame_size.height=frame.rgb.shape[0]
    f.pixel_data.size.CopyFrom(f.frame_size);f.pixel_data.pixel_type=5
    f.pixel_data.raw_data=frame.rgb[:,:,::-1].tobytes()
    for axis in ('x','y','z'):getattr(f.frame_stage_position,axis).value=getattr(frame.position,axis)/1e6
    f.scaling.x=frame.pixel_size_um[0]/1e6;f.scaling.y=frame.pixel_size_um[1]/1e6
    payload=message.SerializeToString();decode_times=[];parse_decode_times=[]
    for _ in range(2):
        start=time.perf_counter();decoded=decode_frame(f);decode_times.append(time.perf_counter()-start)
        start=time.perf_counter();parsed=ExperimentStreamingServiceMonitorAllExperimentsResponse.FromString(payload)
        decoded=decode_frame(parsed.frame_data);parse_decode_times.append(time.perf_counter()-start)
    report={'qualification':'Offline file reads and in-memory protobuf only. No connection, RPC, camera interaction or acquisition. Existing timers combine acquisition, transfer and vendor job finalization; decoding benchmark does not measure transport.',
        'capture_count':len(rows),'acquisition_timings':summary,'by_exposure_ms':by_exposure,
        'decode_benchmark':{'source':original['source'],'payload_bytes':len(payload),'decode_s':decode_times,
            'protobuf_parse_and_decode_s':parse_decode_times,'exact_rgb_equal':bool(np.array_equal(frame.rgb,decoded.rgb))},'captures':rows}
    vendor=vendor_log_timings()
    out=ROOT/'research/camera-latency-audit-01';out.mkdir(parents=True,exist_ok=True)
    (out/'saved-timings.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    (out/'vendor-log-timings.json').write_text(json.dumps(vendor,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='captures'},indent=2))
    print(json.dumps({k:v for k,v in vendor.items() if k not in ('jobs','camera_intervals')},indent=2))


if __name__=='__main__':main()
