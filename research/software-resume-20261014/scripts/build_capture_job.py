"""Build a camera-only ZEN core job from the user's existing camera settings.

Does not modify the source job. Removes all stage, focus and objective tools.
"""
from pathlib import Path
import xml.etree.ElementTree as ET
from copy import deepcopy
import argparse,uuid
parser=argparse.ArgumentParser()
parser.add_argument('--source')
parser.add_argument('--exposure-ms',type=float)
args=parser.parse_args()
src=Path.home()/'AppData/Local/Carl Zeiss/ZENCore/FreeModeWorkspace.czjob'
if args.source:src=Path(args.source)
root=ET.parse(src).getroot()
snap=deepcopy(root.find('.//Task[@Id="SnapAcquisitionTask"]'))
if snap is None:raise RuntimeError('This saved Free Mode workspace has no acquisition task. Pass --source with a saved acquisition job.')
tools=snap.find('Tools')
for tool in list(tools):
    if tool.get('Id')!='CameraTool': tools.remove(tool)
for e in snap.findall('.//Parameter[@Id="AutoExposure"]/Value'): e.text='false'
for e in snap.findall('.//Parameter[@Id="AutoExposureAutoWhiteBalance"]/Value'): e.text='false'
if args.exposure_ms is not None:
    if not 1<=args.exposure_ms<=10000:raise ValueError('Exposure must be 1..10000 ms')
    for e in snap.findall('.//Parameter[@Id="ExposureTime"]/Value'):e.text=str(args.exposure_ms)
for e in snap.findall('.//Parameter[@Id="SuppressExecution"]/Value'): e.text='False'
for e in snap.findall('.//SuppressSingleExecution'): e.text='false'
job=ET.Element('Job',Version='1.1')
metadata=ET.SubElement(job,'Metadata')
ET.SubElement(metadata,'Title').text='FlakeFinder Camera'
ET.SubElement(metadata,'Description').text='Single image with fixed existing camera settings; no stage, focus, or objective commands.'
ET.SubElement(metadata,'Job.Status').text='Draft'
attrs=ET.SubElement(metadata,'CustomAttributes')
ET.SubElement(attrs,'Category').text='FlakeFinder'
ET.SubElement(job,'OrganizationalStructure')
items=ET.SubElement(job,'Items')
item=ET.SubElement(items,'JobItem',Id='Capture',UniqueId=str(uuid.uuid4()))
ET.SubElement(item,'Tasks').append(snap)
ET.SubElement(item,'Children')
ET.SubElement(job,'Resources')
ET.indent(job)
out=Path('configs/FlakeFinder Camera.czjob')
ET.ElementTree(job).write(out,encoding='utf-8',xml_declaration=True)
print(out.resolve())
