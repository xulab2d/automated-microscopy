"""Read-only MTB objective diagnostic. No adapter, motion, or settings writes."""
import json
import sys
from pathlib import Path
import xml.etree.ElementTree as ET
from pythonnet import load
base=Path(r'C:\Program Files\Carl Zeiss\ZEN core')
load('coreclr',runtime_config=str(base/'ZENcore.runtimeconfig.json'))
sys.path.insert(0,str(base))
import clr
clr.AddReference(str(base/'Microsoft.Extensions.Logging.Abstractions.dll'))
clr.AddReference(str(base/'MTBApi.dll'))
from ZEISS.MTB.Api import MTBConnection,IMTBChanger
connection=MTBConnection()
client=None
try:
    client=connection.Login('en','')
    component=connection.GetRoot(client).GetComponent('MTBObjectiveChanger')
    changer=IMTBChanger(component)
    config=Path(str(connection.ActiveConfigurationFile))
    node=ET.parse(config).getroot().find('.//Component[@MTBId="MTBObjectiveChanger"]')
    entries=[{'position':int(e.get('Position'))+1,'name':e.get('DisplayName'),
              'magnification':e.findtext('Magnification')}
             for e in node.findall('./ChangerElements/Element')]
    result={'version':str(connection.MTBVersion),'configuration_changed':bool(connection.ConfigurationHasChanged),
            'position':int(changer.Position),'motorization':str(component.Motorization),
            'accessibility':str(component.Accessibility),'objectives':entries}
    print(json.dumps(result,indent=2))
    Path(__file__).with_name('live-optics.json').write_text(json.dumps(result,indent=2))
finally:
    if client: connection.Logout(client)
    connection.Close()
