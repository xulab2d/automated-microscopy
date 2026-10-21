"""Build a camera-only ZEN job with runtime exposure, preserving camera calibration."""
from pathlib import Path
import xml.etree.ElementTree as ET
import uuid

source=Path(__file__).resolve().parents[1]/'configs/FlakeFinder Camera.czjob'
root=ET.parse(source).getroot()
root.find('Metadata/Title').text='FlakeFinder Profile Camera'
root.find('Metadata/Description').text='Camera-only acquisition. Exposure is supplied through ff_exposure_ms; fixed white balance. No motion tools.'
parameter=root.find('.//Parameter[@Id="ExposureTime"]')
parameter.set('ResourceId','ff_exposure_ms');parameter.set('DataType','double')
parameter.find('Value').text='200'
resources=root.find('Resources')
if resources is None:resources=ET.SubElement(root,'Resources')
resource=ET.SubElement(resources,'Parameter',Id='ff_exposure_ms',UniqueId=str(uuid.uuid4()))
ET.SubElement(resource,'Value').text='200'
target=source.with_name('FlakeFinder Profile Camera.czjob')
ET.indent(root);ET.ElementTree(root).write(target,encoding='utf-8',xml_declaration=True)
print(target)
