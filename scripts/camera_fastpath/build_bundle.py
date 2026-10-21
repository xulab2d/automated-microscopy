"""Build an unregistered, disabled ZEN macro-job prototype; no hardware imports."""
from copy import deepcopy
from pathlib import Path
import json
import uuid
import xml.etree.ElementTree as ET

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
TITLE='FlakeFinder Request Snap Prototype v1'
SOURCE=Path(r'C:\Users\Public\Documents\Carl Zeiss\ZENcore\Documents\Macro Jobs\Demo - Snap and save user defined number of images')


def build():
    bundle=HERE/'bundle';macros=bundle/'Macros';macros.mkdir(parents=True,exist_ok=True)
    transport=HERE/'transport';transport.mkdir(exist_ok=True)
    job=ET.parse(SOURCE/'Demo - ZENCore - Snap and save user defined number of images.czjob').getroot()
    job.find('Metadata/Title').text=TITLE
    job.find('Metadata/Description').text='Disabled experimental request-driven stationary snapshots. No stage commands. Requires an explicit bounded session and verified camera readback profile.'
    job.find('.//Parameter[@Id="MacroName"]/Value').text=TITLE
    for element in job.iter():
        if 'UniqueId' in element.attrib:element.set('UniqueId',str(uuid.uuid4()))
    ET.indent(job);ET.ElementTree(job).write(bundle/(TITLE+'.czjob'),encoding='utf-8',xml_declaration=True)
    original=ET.parse(next((SOURCE/'Macros').glob('*.czmac'))).getroot()
    macro=deepcopy(original)
    macro.find('Text').text=(HERE/'macro.py').read_text(encoding='utf-8').replace('__TRANSPORT_ROOT__',repr(str(transport.resolve())))
    macro.find('Description').text='Experimental stationary snap benchmark; read-only profile verification; no automatic exposure or live start.'
    ET.indent(macro);ET.ElementTree(macro).write(macros/(TITLE+'.czmac'),encoding='utf-8',xml_declaration=True)
    config=json.loads((ROOT/'configs/zen-whole-chip-standardized-01.json').read_text())
    profile={'enabled':False,'camera_name':None,'exposure_ms':config['zen']['camera_resources']['ff_exposure_ms'],
        'expected_shape':[2056,2464,3],'expected_dtype':'uint16','expected_white_level':config['zen']['camera_white_level'],
        'readback_gates':{
            'white_balance':{'parameter_name':None,'expected':config['zen']['camera_resources']['ff_white_balance'],'kind':'rgb'},
            'auto_exposure':{'parameter_name':None,'expected':False,'kind':'boolean'},
            'auto_white_balance':{'parameter_name':None,'expected':False,'kind':'boolean'},
            'hdr':{'parameter_name':None,'expected':False,'kind':'boolean'}},
        'note':'Fill parameter_name only from the emitted runtime camera inventory. Missing bindings deliberately block acquisition. No camera settings are changed by this prototype.'}
    path=HERE/'profile.example.json'
    if not path.exists():path.write_text(json.dumps(profile,indent=2),encoding='utf-8')
    print(bundle)


if __name__=='__main__':build()
