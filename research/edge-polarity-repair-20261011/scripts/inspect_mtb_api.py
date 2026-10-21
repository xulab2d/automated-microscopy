"""Inspect installed vendor API contracts without initializing or moving hardware."""
from pathlib import Path
import sys
from pythonnet import load
base=Path(r'C:\Program Files\Carl Zeiss\ZEN core')
load('coreclr',runtime_config=str(base/'ZENcore.runtimeconfig.json'))
import clr
sys.path.insert(0,str(base))
from System.Reflection import Assembly
api=Assembly.LoadFrom(str(base/'MTBApi.dll'))
for t in api.GetExportedTypes():
    if t.Name in ('MTBConnection','MTBConnectionClass','MTBConnectionProvider','IMTBConnection','IMTBComponent','IMTBChanger','IMTBObjectiveChanger'):
        print(t.FullName,flush=True)
        for member in t.GetMembers():
            if not str(member).startswith(('Void add_','Void remove_')):print('  ',member,flush=True)
