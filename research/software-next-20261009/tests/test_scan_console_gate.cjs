// Run with node tests/test_scan_console_gate.cjs. No server or hardware access.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../flakefinder/scan_console.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script); // Check the entire deployed script parses.
const phaseSource=script.slice(script.indexOf('function runPhaseLabel('),script.indexOf('function render(){'));
const phaseContext=vm.createContext({human:value=>value});
vm.runInContext(phaseSource,phaseContext);
assert.equal(vm.runInContext("runPhaseLabel({state:'running',phase:'autonomous_reference'},{phase_group:'reference_preparation'})",phaseContext),'Preparing focus and substrate reference');
assert.equal(vm.runInContext("runPhaseLabel({state:'failed',phase:'finished'},{phase_group:'reference_preparation'})",phaseContext),'Focus and substrate reference preparation failed');
assert.equal(vm.runInContext("runPhaseLabel({state:'running',phase:'tile'},{phase_group:'campaign'})",phaseContext),'tile');
const dots=[];
const mapCtx={beginPath(){},moveTo(){},lineTo(){},stroke(){},fill(){},closePath(){},fillText(){},
  arc(x,y,r){dots.push({x,y,r,color:this.fillStyle});}};
const mapContext=vm.createContext({snapshot:{progress:{stage_map:{preparation_fields:[{x:10,y:20},{x:30,y:40}]}}},
  canvas:()=>({ctx:mapCtx,w:400,h:300}),xy:p=>Array.isArray(p)?p:p?[p.x,p.y]:null,
  num:Number.isFinite,placeholder:()=>assert.fail('Saved bootstrap map was blank')});
vm.runInContext(script.slice(script.indexOf('function drawMap()'),script.indexOf('function drawFocus()')),mapContext);
vm.runInContext('drawMap()',mapContext);
assert.equal(dots.length,2);
assert.ok(dots.every(p=>p.color==='#66b8db'));
const functions = script.slice(script.indexOf('function operatorHints()'), script.indexOf('function validSettings()'))
  + script.slice(script.indexOf('function gate()'), script.indexOf('function preflightText('));
const elements = {};
const element = id => elements[id] ||= {checked: false, textContent: '', disabled: false};
const s = {material: 'hBN', oxide_nm: 90, operator_ready: true, analysis_after_scan: true};
element('analysis_after_scan').checked = true;
element('release_hold').checked = true;
const context = vm.createContext({
  $: element, settings: () => ({...s, analysis_after_scan: element('analysis_after_scan').checked}),
  signature: () => '', selectedDraft: () => null, active: () => false,
  checkPass: () => false, validSettings: () => true,
  snapshot: {hold: {active: true}, resources: {storage_ok: true}},
  draftSignature: '', checkSignature: '', busy: false, connected: true,
  lastSuccess: Date.now(), token: 'test', stopPending: false,
});
vm.runInContext(functions, context);
vm.runInContext('operatorHints(); gate()', context);
assert.equal(element('analysis_after_scan').checked, false);
assert.equal(element('analysis_after_scan').disabled, true);
assert.equal(element('startBtn').disabled, false);
assert.equal(element('startBtn').textContent, 'Start scan');
assert.equal(element('startHint').textContent, 'Ready to start.');
// Returning to a supported sample makes analysis available without silently enabling it.
s.material = 'graphene';
vm.runInContext('operatorHints(); gate()', context);
assert.equal(element('analysis_after_scan').disabled, false);
assert.equal(element('analysis_after_scan').checked, false);
element('analysis_after_scan').checked = true;
vm.runInContext('operatorHints(); gate()', context);
assert.equal(element('startBtn').textContent, 'Start scan & analysis');
assert.equal(element('startBtn').disabled, false);
// Every previously silent blocker must explain why Start is disabled.
for (const [key, value, message] of [
  ['busy', true, 'Checking settings'],
  ['lastSuccess', 0, 'current server status'],
  ['token', '', 'Session unavailable'],
]) {
  const old = context[key]; context[key] = value;
  vm.runInContext('gate()', context);
  assert.equal(element('startBtn').disabled, true);
  assert.ok(element('startHint').textContent.includes(message));
  context[key] = old;
}
element('release_hold').checked = false;
vm.runInContext('gate()', context);
assert.equal(element('startBtn').disabled, true);
assert.match(element('startHint').textContent, /microscope is available/);
console.log('Scan console gate checks passed.');

// A completed/failed hBN run must not reopen as a graphene setup. Polling must
// not replace unsaved operator edits; readiness is reset by applySettings.
const refreshSource=script.slice(script.indexOf('async function refresh()'),script.indexOf("$('setupForm').addEventListener"));
const applied=[];
const refreshContext=vm.createContext({
  pollBusy:false,snapshot:null,connected:false,lastSuccess:0,
  fallback:{material:'graphene'},
  api:async()=>({run:{state:'search_sampling_complete',settings:{material:'hBN',oxide_nm:90}},defaults:{material:'hBN'},resources:{}}),
  applySettings:value=>applied.push(value), render:()=>{}, tick:()=>{},gate:()=>{},
});
vm.runInContext(refreshSource,refreshContext);
(async()=>{
  await vm.runInContext('refresh()',refreshContext);
  assert.equal(applied[0].material,'hBN');
  await vm.runInContext('refresh()',refreshContext);
  assert.equal(applied.length,1);
  const applySource=script.slice(script.indexOf('function applySettings('),script.indexOf('function dirty('));
  const controls={operator_ready:{type:'checkbox',checked:true},material:{type:'select',value:''},release_hold:{checked:true}};
  const applyContext=vm.createContext({$:id=>controls[id],names:['material','operator_ready'],fallback:{},dirty:()=>{}});
  vm.runInContext(applySource,applyContext);
  vm.runInContext("applySettings({material:'hBN',operator_ready:true})",applyContext);
  assert.equal(controls.material.value,'hBN');
  assert.equal(controls.operator_ready.checked,false);
  assert.equal(controls.release_hold.checked,false);
  console.log('Last-run material and draft-preservation checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
