from pathlib import Path

path = Path(__file__).parents[1]/'software-next-20261009/flakefinder/scan_console.html'
text = path.read_text(encoding='utf-8')
text = text.replace('Save setup & draft', 'Check & save draft')
text = text.replace('>Start scan</button>', '>Check, save & start scan</button>')
text = text.replace('Enter the search area, check the setup, and save a draft.', 'Confirm the setup and microscope readiness, then start.')
text = text.replace('After checking, save a new draft for the full scan.', 'Start scan creates a fresh draft automatically when needed.')
text = text.replace('||!same||!ready||!space||!s.operator_ready', '||!space||!s.operator_ready')
text = text.replace("if(!same)hints.push('Save this setup as a new draft.');if(!ready)hints.push('Pass the setup check.');", '')
text = text.replace("'Ready to start this saved draft. The server checks resources and authorization again.'", "'Start checks resources, saves this setup if needed, then launches. Old coordinates are never restored.'")
start = text.index('async function start(prepareOnly=false)')
end = text.index('\nasync function stop()', start)
text = text[:start]+'''async function start(prepareOnly=false){
 gate();if($(prepareOnly?'qualifyBtn':'startBtn').disabled)return;
 const s=settings(),sig=signature(s),authorizedHold=JSON.stringify(snapshot?.hold||{});
 busy=true;gate();
 try{
  check=await api('/api/scan/preflight',{settings:s});checkSignature=sig;
  if(check.resources)snapshot.resources=check.resources;
  if(check.hold)snapshot.hold=check.hold;
  preflightText(check);
  if(!checkPass())throw new Error((check.blockers||[]).join(' · ')||'Setup check failed.');
  if(JSON.stringify(snapshot.hold)!==authorizedHold){$('release_hold').checked=false;throw new Error('Operator hold changed. Review the updated hold before starting.');}
  let run=selectedDraft();
  if(!run||draftSignature!==sig){
   const planned=await api('/api/scan/plan',{settings:s});
   if(!planned.run?.id)throw new Error('The server did not return a saved draft.');
   draft=planned.run;draftSignature=sig;run=draft;
  }
  const response=await api(prepareOnly?'/api/scan/check':'/api/scan/start',{run_id:run.id,release_hold:snapshot.hold?.active===true&&$('release_hold').checked});
  if(response.run)snapshot={...snapshot,run:response.run};
  notice(prepareOnly?'Stationary microscope check requested. Start scan will prepare a fresh draft after the check finishes.':'Start accepted. Progress appears as images and measurements are saved.');
  $('release_hold').checked=false;draft=null;draftSignature='';await refresh();
 }catch(e){notice((prepareOnly?'Microscope check':'Start')+' was not confirmed: '+e.message+' Check the run status before retrying.',true);await refresh();}
 finally{busy=false;gate();}
}'''+text[end:]
text = text.replace('<h2>Scan setup</h2>', '<h2>Next scan</h2>')
text = text.replace('<form id="setupForm">', '<div class="notice" style="margin-bottom:12px">1. Confirm sample and area<br>2. Confirm microscope readiness<br>3. Check, save &amp; start scan</div><form id="setupForm">')
text = text.replace('<div class="form-note" id="runCountsNote">', '<div class="form-note" id="runCountsNote" aria-live="polite">')
text = text.replace("$('deadline').textContent=deadline!==null?'Deadline '+new Date(deadline*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}):'';", "$('deadline').textContent=deadline!==null?'Deadline '+new Date(deadline*1000).toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})+(deadline<Date.now()/1000?' · expired':''):'';")
path.write_text(text, encoding='utf-8')
