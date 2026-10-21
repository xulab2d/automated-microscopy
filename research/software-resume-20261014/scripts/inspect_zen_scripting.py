"""Read exported scripting interfaces; no application commands."""
from pathlib import Path
import sys
from pythonnet import load
base=Path(r'C:\Program Files\Carl Zeiss\ZEN core')
load('coreclr',runtime_config=str(base/'ZENcore.runtimeconfig.json'));sys.path.insert(0,str(base))
import clr
from System.Reflection import Assembly
for filename in ['Zeiss.Micro.Scripting.dll','Zeiss.Micro.LM.Scripting.dll','Zeiss.Micro.Scripting.Workflows.dll','Zeiss.Micro.LM.Scripting.Workflows.dll']:
    try:
        a=Assembly.LoadFrom(str(base/filename))
        for t in a.GetExportedTypes():
            if any(n in t.Name.lower() for n in ['wrapper','camera','application','jobresource']):
                print(t.FullName,flush=True)
                if 'wrapper' in t.Name.lower():
                    for m in t.GetMembers():print(' ',m,flush=True)
    except Exception as e:print(filename,str(e)[:300],flush=True)
