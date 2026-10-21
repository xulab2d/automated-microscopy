// No server/hardware: hBN must not call the graphene thickness estimator.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(require('node:path').join(__dirname,'../flakefinder/graphene_review.html'),'utf8');
const start=html.indexOf('async function loadThickness(row){');
const end=html.indexOf('\n}',start)+2;
const box={hidden:false,textContent:'old estimate'};
const context={data:{material:'hBN'},$:()=>box,fetch:()=>assert.fail('hBN requested graphene thickness')};
vm.createContext(context);vm.runInContext(html.slice(start,end),context);
(async()=>{await context.loadThickness({id:'chip001/tile_000001/1'});
 assert.equal(box.hidden,true);assert.equal(box.textContent,'');
 for(const match of html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g))new Function(match[1]);
 assert.ok(html.includes("data.material_label||data.settings?.material"));
 const fields={colorAxes:{value:'01'},colorMin:{value:''},colorMax:{value:''}};
 const plot={colorPoints:new Float32Array([-2,3,1,30,0,-.8,-.6,-.4,40,1]),
  colorData:{proposals:[{contrast_rgb:[-.8,-.6,-.4]}]},
  finite:Number.isFinite,$:id=>fields[id]};
 vm.createContext(plot);
 vm.runInContext(html.slice(html.indexOf('function fitColorRange('),html.indexOf('function nearestColor(')),plot);
 plot.fitColorRange(true);
 assert.ok(Number(fields.colorMin.value)<-.8 && Number(fields.colorMax.value)>-.6);
 assert.ok(Number(fields.colorMax.value)<0); // no inherited graphene +0.4 bound
 plot.colorData.proposals=[];plot.fitColorRange(true);
 assert.ok(Number(fields.colorMin.value)<-2 && Number(fields.colorMax.value)>3);
 assert.ok(html.includes("if(colorData.material==='hBN')fitColorRange(true)"));
 const labelContext={data:{material:'hBN'},finite:Number.isFinite,fmt:v=>String(v)};
 vm.createContext(labelContext);
 vm.runInContext(html.slice(html.indexOf('function hbnEvidenceText('),html.indexOf('function failReason(')),labelContext);
 const text=labelContext.hbnEvidenceText({hbn_screen:{policy_version:'hbn-thin-film-evidence-v2',
  target_band:'below_5_nm',thickness_interval_nm:[1,4],operator_supported:true,contamination_ambiguous:true}});
 assert.ok(text.includes('Below 5 nm')&&text.includes('nm conditional'));
 assert.ok(text.includes('thickness not measured')&&text.includes('residue ambiguous'));
 assert.ok(html.includes('value="other">Thicker or unresolved'));
 assert.ok(html.includes("target_band:$('targetBand').value"));
 assert.ok(html.includes('value="all">All thicknesses'));
 const defaults={};vm.createContext(defaults);
 vm.runInContext(html.slice(html.indexOf('function progressiveDefaults('),html.indexOf('function hbnEvidenceText(')),defaults);
 const live={material:'hBN',status:{state:'running'}};
 assert.equal(defaults.progressiveDefaults(live,false,false).filter,'all');
 assert.equal(defaults.progressiveDefaults(live,false,false).targetBand,'all');
 assert.equal(defaults.progressiveDefaults(live,true,true).filter,null);
 assert.equal(defaults.progressiveDefaults(live,true,true).targetBand,null);
 assert.equal(defaults.progressiveDefaults(live,true,false).targetBand,'all');
 assert.equal(defaults.progressiveDefaults({material:'hBN',status:{state:'complete'},review_processing:{pending_observations:12}},false,false).filter,'all');
 assert.equal(defaults.progressiveDefaults({material:'hBN',status:{state:'complete'}},false,false),null);
 assert.equal(defaults.progressiveDefaults({material:'graphene',status:{state:'running'}},false,false),null);
 assert.match(labelContext.hbnEvidenceText({review_pending:true}),/boundary and halo checks pending/);
 assert.ok(html.includes('!progressiveDefaults(value,true,true)&&value.fields_version')); // live rows update on polling
 assert.ok(html.includes('data.review_processing=value.review_processing;data.status=value.status'));
 const elements={targetBand:{value:'both'},chip:{value:''},area:{value:'25'},groupDuplicates:{checked:true},showArtifacts:{checked:false},outlines:{checked:false},grid:{style:{},setAttribute(){}},connection:{},updateResults:{},warning:{},state:{},dialog:{hidden:true},indexProgress:{}};
 const requests=[];let renders=0;const timers=new Map();let nextTimer=0;const setTimer=(fn,delay)=>{const id=++nextTimer;timers.set(id,{fn,delay});return id};
 const refreshed={run:'scan-20261015-223549-98fae566',selectedChip:'',filter:'shortlist',field:'',offset:0,data:null,busy:false,generation:0,lastGood:0,
  progressiveDefaultsApplied:false,filterTouched:false,targetTouched:false,reviewRefreshTimer:null,query:new URLSearchParams(),URLSearchParams,AbortController,document:{hidden:false},setTimeout:setTimer,clearTimeout:id=>timers.delete(id),esc:String,fmt:String,
  $:id=>elements[id],render:()=>renders++,Date,
  fetch:async url=>{requests.push(new URL(url,'http://localhost').searchParams);return {ok:true,json:async()=>({material:'hBN',status:{state:'running'},review_processing:{pending_observations:10},fields_version:'one',fields:[],offset:0,rows:[]})}}};
 vm.createContext(refreshed);
 vm.runInContext(html.slice(html.indexOf('function progressiveDefaults('),html.indexOf('function hbnEvidenceText(')),refreshed);
 vm.runInContext(html.slice(html.indexOf('function scheduleReviewRefresh('),html.indexOf('function refilter(')),refreshed);
 await refreshed.refresh();
 assert.equal(requests.length,2);assert.equal(requests[1].get('filter'),'all');assert.equal(requests[1].get('target_band'),'all');assert.equal(renders,1);
 await refreshed.refresh(true);assert.equal(requests.length,3);assert.equal(renders,2); // no repeated defaults request
 assert.equal(requests[0].get('progressive'),'1');assert.equal(timers.size,0);
 // Progressive responses keep available rows and one short follow-up, then stop when ready.
 const readyRows=[{id:'saved/1'}];let waiting=7;
 refreshed.fetch=async()=>({ok:true,json:async()=>({material:'hBN',status:{state:'running'},review_index:{state:waiting?'loading':'ready',fields_indexed:12,fields_waiting:waiting},fields_version:'one',fields:[],offset:0,rows:readyRows})});
 await refreshed.refresh(true);assert.equal(refreshed.data.rows[0].id,'saved/1');assert.equal(timers.size,1);assert.equal([...timers.values()][0].delay,1000);
 refreshed.renderIndexProgress();assert.equal(elements.indexProgress.hidden,false);assert.match(elements.indexProgress.textContent,/12 fields ready, 7 remaining/);
 await refreshed.refresh(true);assert.equal(timers.size,1); // manual refresh replaces, never duplicates timer
 waiting=0;await refreshed.refresh(true);assert.equal(timers.size,0);refreshed.renderIndexProgress();assert.equal(elements.indexProgress.hidden,true);
 // Network failure keeps prior rows, clears busy state and arranges a bounded retry.
 refreshed.fetch=async()=>{throw new Error('Drive temporarily unavailable')};
 await refreshed.refresh();assert.equal(refreshed.busy,false);assert.equal(refreshed.data.rows[0].id,'saved/1');assert.match(elements.warning.textContent,/Retrying shortly/);assert.equal([...timers.values()][0].delay,5000);
 // A hung response aborts at 30 seconds; overlapping refresh calls never start another request.
 let hungCalls=0;
 refreshed.fetch=(_url,options)=>{hungCalls++;return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(Object.assign(new Error('aborted'),{name:'AbortError'}))))};
 const hung=refreshed.refresh();assert.equal(refreshed.busy,true);await refreshed.refresh();assert.equal(hungCalls,1);
 const timeout=[...timers.values()].find(t=>t.delay===30000);assert.ok(timeout);timeout.fn();await hung;
 assert.equal(refreshed.busy,false);assert.match(elements.warning.textContent,/timed out.*Retrying/);assert.equal(timers.size,1);assert.equal([...timers.values()][0].delay,5000);
 // The next successful poll recovers without a page reload or residual retry timer.
 refreshed.fetch=async()=>({ok:true,json:async()=>({material:'hBN',status:{state:'running'},review_index:{state:'ready'},fields_version:'one',fields:[],offset:0,rows:readyRows})});
 await refreshed.refresh();assert.equal(timers.size,0);assert.equal(refreshed.busy,false);
 // Human review uses source identity, session token and optimistic revision; no scientific writes.
 const feedbackNodes={humanFeedback:{hidden:false,innerHTML:'',querySelectorAll:()=>[]},feedbackActor:{value:'Reviewer A'},feedbackStatus:{textContent:'',className:''}};
 const store=new Map(),feedbackCalls=[];let galleryRefreshes=0;
 const feedback={run:'scan-20261015-223549-98fae566',detail:{id:'chip001/tile_000001/1'},detailGeneration:0,generation:0,data:{material:'hBN',rows:[{id:'chip001/tile_000001/1'}]},
  $:id=>feedbackNodes[id],esc:String,localStorage:{getItem:k=>store.get(k),setItem:(k,v)=>store.set(k,v)},AbortController,URLSearchParams,setTimeout:()=>1,clearTimeout:()=>{},crypto:{randomUUID:()=> 'unique-event'},refresh:()=>galleryRefreshes++};
 vm.createContext(feedback);vm.runInContext(html.slice(html.indexOf('let feedbackSaving='),html.indexOf('async function openObject(')),feedback);
 feedback.detail.human_feedback={label:'thin_hbn',actor:'Prior reviewer'};feedback.renderFeedback();assert.match(feedbackNodes.humanFeedback.innerHTML,/data-feedback="thin_hbn" aria-pressed="true" class="active"/);feedback.detail.human_feedback=null;
 feedback.renderFeedback();assert.ok(feedbackNodes.humanFeedback.innerHTML.includes('data-feedback="thin_hbn"'));assert.ok(feedbackNodes.humanFeedback.innerHTML.includes('data-feedback="encapsulation_hbn"'));
 feedback.data.material='graphene';feedback.renderFeedback();assert.ok(feedbackNodes.humanFeedback.innerHTML.includes('data-feedback="good_flake"'));assert.ok(!feedbackNodes.humanFeedback.innerHTML.includes('data-feedback="thin_hbn"'));feedback.data.material='hBN';
 // Keep the status DOM small; save logic remains the actual extracted browser function.
 feedback.renderFeedback=(message='',error=false)=>{feedbackNodes.feedbackStatus.textContent=message;feedbackNodes.feedbackStatus.className=error?'bad':''};
 const identity={source:'pinned-identity'};
 feedback.fetch=async(url,options)=>{feedbackCalls.push({url,options});if(url.startsWith('/api/graphene?'))return {ok:true,json:async()=>({rows:[{id:feedback.detail.id,feedback_identity:identity,feedback_revision:2}]})};if(url==='/api/session')return {ok:true,json:async()=>({token:'review-token'})};const payload=JSON.parse(options.body);return {ok:true,json:async()=>({revision:payload.expected_revision+1,annotation:payload.label?{label:payload.label,actor:payload.actor}:null})}};
 const originalDetail=feedback.detail;await feedback.saveFeedback('thin_hbn');
 assert.equal(feedbackCalls.length,3);assert.equal(feedbackCalls[1].url,'/api/session');
 const post=feedbackCalls[2];assert.equal(post.options.method,'POST');assert.equal(post.options.credentials,'same-origin');assert.equal(post.options.headers['X-Review-Token'],'review-token');
 const body=JSON.parse(post.options.body);assert.equal(body.expected_revision,2);assert.equal(body.identity.source,'pinned-identity');assert.equal(body.label,'thin_hbn');assert.equal(body.actor,'Reviewer A');assert.equal(body.client_event_id,'unique-event');
 assert.equal(feedback.detail,originalDetail);assert.equal(feedback.detail.feedback_revision,3);assert.equal(feedback.detail.human_feedback.label,'thin_hbn');assert.equal(galleryRefreshes,1);
 await feedback.saveFeedback(null);assert.equal(JSON.parse(feedbackCalls.at(-1).options.body).expected_revision,3);assert.equal(JSON.parse(feedbackCalls.at(-1).options.body).label,null);assert.equal(feedback.detail.human_feedback,null);assert.equal(feedback.detail.feedback_revision,4);
 feedback.fetch=async()=>({ok:false,status:409,json:async()=>({error:'Review changed; reopen this observation before retrying.'})});await feedback.saveFeedback('bulk');assert.match(feedbackNodes.feedbackStatus.textContent,/Review changed/);assert.equal(feedback.detail.feedback_revision,4);assert.equal(galleryRefreshes,2);
 // Invalid session token is discarded; missing token never permits a POST.
 feedback.fetch=async()=>({ok:false,status:403,json:async()=>({error:'Invalid review token'})});await feedback.saveFeedback('bulk');
 let deniedCalls=0;feedback.fetch=async url=>{deniedCalls++;assert.equal(url,'/api/session');return {ok:true,json:async()=>({})}};await feedback.saveFeedback('bulk');assert.equal(deniedCalls,1);assert.match(feedbackNodes.feedbackStatus.textContent,/session unavailable/);
 console.log('hBN UI progressive loading, feedback POST/undo/conflict/token and material guards passed');
})().catch(error=>{console.error(error);process.exitCode=1});
