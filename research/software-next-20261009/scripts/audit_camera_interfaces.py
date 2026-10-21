"""Offline-only ZEN metadata inspection; never instantiate services or call hardware.

Read method signatures and IL from installed assemblies. This deliberately has
no gRPC, COM GetActiveObject, camera object construction, or method invocation.
"""
from pathlib import Path
import json
import struct
import sys

from pythonnet import load


def main():
    base=Path(r'C:\Program Files\Carl Zeiss\ZEN core')
    load('coreclr',runtime_config=str(base/'ZENcore.runtimeconfig.json'))
    sys.path.insert(0,str(base))
    import clr
    from System.Reflection import Assembly,BindingFlags
    from System.Reflection.Emit import OpCodes
    flags=BindingFlags.Public|BindingFlags.NonPublic|BindingFlags.Instance|BindingFlags.Static|BindingFlags.DeclaredOnly
    opcodes={int(f.GetValue(None).Value)&65535:f.GetValue(None)
             for f in clr.GetClrType(OpCodes).GetFields(BindingFlags.Public|BindingFlags.Static)}

    def body(method):
        b=method.GetMethodBody()
        if b is None:return []
        data=bytes(b.GetILAsByteArray());i=0;result=[]
        while i<len(data):
            start=i;code=data[i];i+=1
            if code==254:code=(code<<8)|data[i];i+=1
            op=opcodes[code];kind=str(op.OperandType);operand=None
            if kind in ('InlineString','InlineMethod','InlineField','InlineType','InlineTok','InlineSig'):
                token=struct.unpack_from('<I',data,i)[0];i+=4
                try:
                    if kind=='InlineString':operand=method.Module.ResolveString(token)
                    else:
                        member=method.Module.ResolveMember(token)
                        owner=str(member.DeclaringType.FullName) if member.DeclaringType else ''
                        operand=owner+' :: '+str(member)
                except Exception:operand=f'token:0x{token:08x}'
            elif kind=='InlineSwitch':
                count=struct.unpack_from('<I',data,i)[0];i+=4+4*count;operand=f'{count} branches'
            elif kind in ('InlineI','InlineBrTarget','ShortInlineR'):
                operand=struct.unpack_from('<i',data,i)[0];i+=4
            elif kind in ('InlineI8','InlineR'):i+=8
            elif kind=='InlineVar':operand=struct.unpack_from('<H',data,i)[0];i+=2
            elif kind in ('ShortInlineI','ShortInlineVar','ShortInlineBrTarget'):
                operand=struct.unpack_from('<b',data,i)[0];i+=1
            elif kind!='InlineNone':raise ValueError(kind)
            result.append({'offset':start,'op':str(op.Name),'operand':operand})
        return result

    selections={
        'Zeiss.Micro.Acquisition.Api.dll':{
            'Zeiss.Micro.Acquisition.Api.ExperimentApiManager':None,
            'Zeiss.Micro.Acquisition.Api.ExperimentApiExecutionService':None,
            'Zeiss.Micro.Acquisition.Api.V1beta.ExperimentApi':None,
        },
        'Zeiss.Micro.Workflows.Common.dll':{
            'Zeiss.Micro.Workflows.Api.WorkflowApiManager':None,
        },
        'Zeiss.Micro.LM.Scripting.dll':{
            'Zeiss.Micro.LM.Scripting.ZenAcquisitionLM':{'StartLive','StartContinuous','AcquireImage','StopLive'},
        },
        'Zeiss.Micro.LM.Hardware.Api.dll':{
            'Zeiss.Micro.LM.Hardware.Api.V1.StageApi':None,
            'Zeiss.Micro.LM.Hardware.Api.V2.StageApi':None,
        },
        'Zeiss.Micro.Api.Infrastructure.dll':{
            'Zeiss.Micro.Api.Infrastructure.Server.ApiControllerWrapper+ContextAwareApiCallInterceptor':None,
        },
        'Zeiss.Micro.AcquisitionEngine.Api.dll':{
            'Zeiss.Micro.AcquisitionEngine.Api.AcquisitionEngineApiDataService':None,
            'Zeiss.Micro.AcquisitionEngine.Api.ExperimentFrameDataMonitor':None,
            'Zeiss.Micro.AcquisitionEngine.Api.ZenApiSinkAdapterFilter':None,
            'Zeiss.Micro.AcquisitionEngine.Api.V1beta.ExperimentStreamingApi':None,
        },
    }
    report={'qualification':'Offline reflection metadata only. No vendor instances, method invocation, COM binding, RPC or hardware connection.','assemblies':{}}
    for filename,types in selections.items():
        assembly=Assembly.LoadFrom(str(base/filename));records={}
        for type_name,methods in types.items():
            t=assembly.GetType(type_name,True)
            for current in [t,*t.GetNestedTypes(BindingFlags.Public|BindingFlags.NonPublic)]:
                for method in current.GetMethods(flags):
                    if current==t and methods and str(method.Name) not in methods:continue
                    if current!=t and not any(x in str(current.Name) for x in ('RunJob','ExecuteJob','StartLive','StartContinuous','RunSnap','StartSnap','RunExperiment','StartExperiment','MoveTo','ReadAndTransform')):continue
                    try:instructions=body(method)
                    except Exception as e:instructions=[{'error':str(e)}]
                    records[str(current.FullName)+' :: '+str(method)]=instructions
        report['assemblies'][str(base/filename)]={'version':str(assembly.GetName().Version),'methods':records}
    destination=Path('research/camera-latency-audit-01')
    destination.mkdir(parents=True,exist_ok=True)
    (destination/'installed-methods.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(destination/'installed-methods.json')


if __name__=='__main__':main()
