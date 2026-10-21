"""Standalone sparse preflight must not open hardware on invalid contracts."""
from dataclasses import asdict
import importlib.util,json,sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from sparse_profile_fixture import ImageStage,profile_for
from test_chip_discovery import RECT
from flakefinder.imaging import save_frame
from flakefinder.model import SafetyError,Cancelled

@pytest.fixture
def cli_case(tmp_path,monkeypatch):
    path=Path(__file__).resolve().parents[1]/'scripts/discover_chip_boundaries.py'
    spec=importlib.util.spec_from_file_location('isolated_boundary_cli',path)
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    scope=ImageStage([RECT]);frame=scope.capture();save_frame(tmp_path/'reference','raw',frame)
    cfg={'schema':1,'backend':'simulator','limits':asdict(scope.limits),'camera_transform':asdict(scope.transform)}
    cfgpath=tmp_path/'config.json';cfgpath.write_text(json.dumps(cfg))
    args=['discover_chip_boundaries.py','edges','--config',str(cfgpath),'--reference',str(tmp_path/'reference/raw.tiff'),
        '--output',str(tmp_path/'output'),'--bounds','20','1200','20','1100']
    monkeypatch.setattr(sys,'argv',args)
    monkeypatch.setattr(cli,'connect',lambda cfg:pytest.fail('Rejected standalone request opened hardware'))
    return cli,cfg,cfgpath,frame,args,tmp_path/'output'

@pytest.mark.parametrize('kind',['missing','bright','wrong_objective','context_drift'])
def test_invalid_profile_precedes_hardware_connection(cli_case,kind):
    cli,cfg,path,frame,args,out=cli_case
    if kind!='missing':
        profile=profile_for(frame)
        if kind=='bright':profile['holder_model']='bright_holder'
        if kind=='wrong_objective':profile['objective_magnification']=10
        if kind=='context_drift':profile['optical_context']['illumination_percent']=20
        cfg['chip_discovery']={'edge_validation_profile':profile};path.write_text(json.dumps(cfg))
    with pytest.raises(SafetyError,match='before connection'):cli.main()
    assert not out.exists()

@pytest.mark.parametrize('kind',['stop','legacy','budget'])
def test_stop_legacy_identity_and_budget_precede_connection(cli_case,kind):
    cli,cfg,path,frame,args,out=cli_case
    cfg['chip_discovery']={'edge_validation_profile':profile_for(frame)};path.write_text(json.dumps(cfg))
    if kind=='stop':out.mkdir();(out/'STOP').write_text('operator')
    if kind=='legacy':
        out.mkdir();(out/'discovery.json').write_text(json.dumps({'algorithm':'sparse_coarse_bracket_halfplanes_v1'}));args.append('--resume')
    if kind=='budget':args.extend(['--max-probes','33'])
    exception,match={'stop':(Cancelled,'STOP'),'legacy':(SafetyError,'cannot resume'),'budget':(ValueError,'32')}[kind]
    with pytest.raises(exception,match=match):cli.main()
    if kind=='stop':assert (out/'STOP').read_text()=='operator'

def test_explicit_profile_file_passes_to_discovery_and_adapter_closes(cli_case,monkeypatch):
    cli,cfg,path,frame,args,out=cli_case;profile=profile_for(frame)
    profilepath=path.with_name('profile.json');profilepath.write_text(json.dumps(profile))
    args.extend(['--edge-validation-profile',str(profilepath)]);closed=[];calls=[]
    scope=SimpleNamespace(close=lambda:closed.append(True))
    monkeypatch.setattr(cli,'connect',lambda cfg:scope);monkeypatch.setattr(cli,'arm',lambda *args:None)
    def discover(*args,**kwargs):
        calls.append(kwargs);return {'state':'unresolved','records':[]}
    monkeypatch.setattr(cli,'discover_sparse_outline',discover)
    cli.main()
    assert calls[0]['edge_validation_profile']==profile and closed==[True]
