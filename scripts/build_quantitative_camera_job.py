"""Build a camera-only job with typed exposure and white-balance resources.

This writes a separate job; it does not import it, execute it, or contact hardware.
The initial white balance is read from the installed ZEISS camera preset table.
"""
from pathlib import Path
import argparse
import uuid
import xml.etree.ElementTree as ET


def build_job(source, target, presets_xml, preset='T-5500K'):
    camera = 'Axiocam305c.Default'
    presets = ET.parse(presets_xml).getroot()
    value = presets.findtext(f'{camera}/{preset}')
    if value is None:
        raise ValueError(f'No {camera}/{preset} white-balance preset in {presets_xml}')
    channels = [float(part.strip()) for part in value.split(',')]
    if len(channels) != 3 or any(not 0 < channel <= 1 for channel in channels):
        raise ValueError('Unexpected camera white-balance preset.')
    white_balance = ','.join(format(channel, '.12g') for channel in channels)

    root = ET.parse(source).getroot()
    # New identities keep the imported job independent of the existing camera jobs.
    for element in root.iter():
        if 'UniqueId' in element.attrib:
            element.set('UniqueId', str(uuid.uuid4()))
    root.find('Metadata/Title').text = 'FlakeFinder Quantitative Camera'
    root.find('Metadata/Description').text = (
        f'Camera-only fixed acquisition; ff_exposure_ms (double) and '
        f'ff_white_balance (string). Initial {camera} {preset} vendor preset. '
        'Auto exposure, auto white balance and HDR disabled. No motion tools.'
    )
    resources = root.find('Resources')
    if resources is None:
        resources = ET.SubElement(root, 'Resources')

    def resource(parameter_id, resource_id, data_type, initial_value):
        parameter = root.find(f'.//Tool[@Id="CameraTool"]/Parameters/Parameter[@Id="{parameter_id}"]')
        if parameter is None:
            raise ValueError(f'Source job has no camera parameter {parameter_id}.')
        parameter.set('ResourceId', resource_id)
        parameter.set('DataType', data_type)
        parameter.find('Value').text = initial_value
        node = resources.find(f'Parameter[@Id="{resource_id}"]')
        if node is None:
            node = ET.SubElement(resources, 'Parameter', Id=resource_id, UniqueId=str(uuid.uuid4()))
            ET.SubElement(node, 'Value')
        node.set('DataType', data_type)
        node.find('Value').text = initial_value

    resource('ExposureTime', 'ff_exposure_ms', 'double', '200')
    resource('WhiteBalance', 'ff_white_balance', 'string', white_balance)
    for parameter_id in ('AutoExposure', 'EnableHDR', 'AutoExposureAutoWhiteBalance'):
        for parameter in root.findall(f'.//Parameter[@Id="{parameter_id}"]'):
            parameter.find('Value').text = 'false'
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root)
    ET.ElementTree(root).write(target, encoding='utf-8', xml_declaration=True)
    return {'path': str(target.resolve()), 'white_balance': white_balance, 'preset': preset}


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=root/'configs/FlakeFinder Profile Camera.czjob')
    parser.add_argument('--output', type=Path, default=root/'configs/FlakeFinder Quantitative Camera.czjob')
    parser.add_argument('--presets-xml', type=Path,
                        default=Path(r'C:\Program Files\Carl Zeiss\ZEN core\AxioCamWhiteBalance.xml'))
    parser.add_argument('--preset', choices=['T-5500K', 'T-3200K'], default='T-5500K')
    arguments = parser.parse_args()
    print(build_job(arguments.source, arguments.output, arguments.presets_xml, arguments.preset))
