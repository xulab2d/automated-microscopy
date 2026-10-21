from pathlib import Path
import sys,json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from google.protobuf.json_format import MessageToDict
from flakefinder.zen import ZenMicroscope
s=ZenMicroscope()
try:
    pb,stub=s._service('zen_api.workflows.v3beta.workflow_service','WorkflowService')
    r=s._rpc(stub,'GetAvailableJobTemplates',pb.WorkflowServiceGetAvailableJobTemplatesRequest())
    print('jobs',MessageToDict(r,preserving_proto_field_name=True))
    pb,stub=s._service('zen_api.acquisition.v1beta.experiment_service','ExperimentService')
    previous=json.loads(Path('data/first-live-frame.json').read_text())['experiment_id']
    try:
        r=s._rpc(stub,'Export',pb.ExperimentServiceExportRequest(experiment_id=previous))
        Path('data/current-experiment.xml').write_text(r.xml,encoding='utf-8')
        print('exported',len(r.xml))
    except Exception as e: print('export',e)
finally:s.close()
