from pathlib import Path
p=Path(__file__).parents[1]/'software-next-20261009/flakefinder/scan_console.html'
s=p.read_text(encoding='utf-8')
s=s.replace("if(initial){applySettings(data.defaults);", "if(initial){const savedDraft=data.run&&['draft','planned','ready'].includes(data.run.state);applySettings(savedDraft?data.defaults:{...data.defaults,sample_id:'',width_mm:null,height_mm:null,hours:2,auto_exposure:true});")
s=s.replace('<section class="panel run-head"><div class="run-heading">','<section class="panel run-head"><div id="runContext" class="small muted" style="margin-bottom:8px">Saved acquisition evidence</div><div class="run-heading">')
s=s.replace("renderResources(s.resources||{});$('grapheneLink')", "renderResources(s.resources||{});$('runContext').textContent=active(r)?'Current run':r?'Previous run · saved evidence':'No saved run';$('grapheneLink')")
s=s.replace("const currentSettings=r?.settings||{};", "const currentSettings=r?.settings||{};")
p.write_text(s,encoding='utf-8')
