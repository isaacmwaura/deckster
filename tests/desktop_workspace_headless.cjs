/* Real shared-renderer desktop interactions, no live audio or visible windows. */
const assert=require('assert'),fs=require('fs'),http=require('http'),path=require('path');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'../web'),out=path.resolve(__dirname,'../release/verify-feedback-2026-09-30');
const actions=[],errors=[];
async function box(locator){for(let i=0;i<10;i++){await locator.waitFor({state:'visible'});const bounds=await locator.boundingBox();if(bounds)return bounds;}throw Error('Preview element did not settle');}
const clips=['Air Horn','Rimshot','Sad Trombone','Applause','Crickets','Record Scratch','Drum Roll','Censor Bleep','Success','Wrong','Bell','Pop','Boing','Whoosh','Click','Sparkle'].map((label,i)=>({id:'clip-'+i,label,emoji:['📣','🥁','🎺','👏','🦗','💿','🥁','🔇','✨','❌','🔔','💥','🌀','💨','🖱','✨'][i],duration:1.4,gain:1,voice:true,ears:true}));
let presentation={revision:0,appOrder:[],hiddenApps:[],pages:{soundboard:'top',devices:'right',media:'bottom'},padSlots:clips.slice(0,12).map(c=>c.id),hideInteraction:'drag'};
const sessions=[['discord','Discord',.72],['spotify','Spotify',.48],['chrome','Google Chrome',.35],['game','Your game',.86]].map(([id,appLabel,level])=>({id,appLabel,level,active:true}));
const devices={speakerMaster:{level:.68,muted:false},micMaster:{level:.92,muted:false},meters:{input:.4,output:.2},inputs:[{id:'mic',name:'Microphone (Realtek Audio)',isDefault:true},{id:'receiver',name:'CABLE Output (VB-Audio Virtual Cable)',isDefault:false}],outputs:[{id:'cable',name:'CABLE Input (VB-Audio Virtual Cable)'},{id:'speaker',name:'Headphones',isDefault:true}]};
const soundboard={clips,config:{inputId:'mic',voiceOutputId:'cable',earsOutputId:'speaker'},inputs:devices.inputs,outputs:devices.outputs,runtime:'ready',error:'',levels:{mic:.3,sounds:.2,voice:.5,ears:.2}};
let connectedScreens=[{width:960,height:432,name:'Demo phone'}],cableAvailable=true,formatsSupported=false,signalReads=0,workspaceReads=0;
let connectionHealth;
const snap=()=>({t:'snapshot',presentation,sessions,devices,soundboard,media:[]});
const server=http.createServer((req,res)=>{
  const pathname=req.url.split('?')[0];
  if(pathname==='/admin/api/signals'){signalReads++;res.setHeader('Content-Type','application/json');res.end(JSON.stringify({soundboard:{runtime:soundboard.runtime,levels:soundboard.levels},inputLevel:devices.meters.input}));return;}
  if(pathname==='/admin/api/workspace'){
    res.setHeader('Content-Type','application/json');
    if(req.method==='GET'){workspaceReads++;res.end(JSON.stringify({admin:{version:'0.6.6',mode:'loopback',pairCode:'123456',connectUrl:'https://localhost:8765/',connectNote:'USB connected',devices:[],secure:true,connection:connectionHealth},snapshot:snap(),presentation:{presentation,sessions,devices,clips},soundboard,cableAvailable,connectedScreens,receivingMic:'CABLE Output (VB-Audio Virtual Cable)',routingStatus:{ready:soundboard.runtime==='ready',mode:devices.inputs[1].isDefault?'mixed':'bypass',receiverId:'receiver',defaultInput:devices.inputs.find(d=>d.isDefault).name}}));return;}
    let raw='';req.on('data',d=>raw+=d);req.on('end',()=>{const body=JSON.parse(raw);actions.push(body);if(body.action==='presentation'){if(body.baseRevision!==presentation.revision){res.statusCode=409;res.end(JSON.stringify({error:'Layout changed elsewhere'}));return;}presentation={...presentation,...body.changes,revision:presentation.revision+1};res.end(JSON.stringify(presentation));}else if(body.action==='use_mixer_input'){devices.inputs[0].isDefault=false;devices.inputs[1].isDefault=true;res.end('{"ok":true}');}else if(body.action==='check_formats'){res.end(JSON.stringify({sampleRate:48000,endpoints:[{label:'Microphone',name:'Mic',supported:true,defaultRate:44100},{label:'Cable',name:'Cable',supported:formatsSupported,error:formatsSupported?'':'Unsupported sample rate'}],recommendation:'Choose 48,000 Hz in Windows audio settings.'}));}else res.end('{"ok":true}');});return;
  }
  if(pathname==='/admin/qr'){const qr=path.join(out,'demo-qr.png');if(fs.existsSync(qr)){res.setHeader('Content-Type','image/png');res.end(fs.readFileSync(qr));}else{res.writeHead(204);res.end();}return;}
  const name=pathname==='/admin'?'desktop.html':pathname==='/'?'index.html':pathname.replace(/^\/static\//,'');
  const file=path.join(root,name);if(!file.startsWith(root+path.sep)||!fs.existsSync(file)){res.writeHead(404);res.end();return;}
  res.setHeader('Content-Type',name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':name.endsWith('.png')?'image/png':'text/html');res.end(fs.readFileSync(file));
});
(async()=>{let browser;try{
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));browser=await chromium.launch({executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',headless:true});const page=await browser.newPage({viewport:{width:1440,height:1040}});page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{window.WebSocket=class{constructor(){throw Error('Preview must never open a phone socket');}};});
  await page.goto(`http://127.0.0.1:${server.address().port}/admin`);const frame=page.frameLocator('#phone');await frame.locator('#apps-grid .tile').first().waitFor();
  assert.strictEqual(await frame.locator('#apps-grid .tile').count(),4);assert.strictEqual(await frame.locator('#pair-overlay:not(.hidden)').count(),0);
  fs.mkdirSync(out,{recursive:true});await page.screenshot({path:path.join(out,'desktop-layout.png'),fullPage:true});
  await page.locator('[data-name=mixer]').dragTo(page.locator('[data-position=top]'));await page.click('#save');await page.waitForFunction(()=>document.querySelector('#draft-status').textContent==='All changes saved');assert.strictEqual(presentation.pages.mixer,'top');assert.strictEqual(presentation.pages.soundboard,'center');
  await page.click('[data-view=mixer]');await page.click('[data-hide=tap]');await page.click('#save');await page.waitForFunction(()=>document.querySelector('#save').disabled);assert.strictEqual(presentation.hideInteraction,'tap');
  await page.click('[data-view=soundboard]');await frame.locator('.sound-pad').first().waitFor();assert.strictEqual(await page.locator('.sound-item').count(),4);
  await page.locator('.sound-item').first().click();await page.waitForFunction(()=>document.querySelector('#notice').textContent.includes('Playing'));assert(actions.some(a=>a.action==='audition'&&a.id==='clip-12'));
  const target=frame.locator('.sound-pad').nth(3);await page.locator('.sound-item').first().dragTo(target);await page.click('#save');await page.waitForFunction(()=>document.querySelector('#save').disabled);assert.strictEqual(presentation.padSlots[3],'clip-12');assert(await page.locator('.sound-item[data-id="clip-3"]').count());
  await frame.locator('.sound-pad').nth(3).click();await page.waitForTimeout(50);assert(actions.some(a=>a.action==='audition'&&a.id==='clip-12'));await page.locator('.sound-item').first().dragTo(frame.locator('.sound-pad').nth(5));await page.click('#discard');
  const start=await box(frame.locator('.sound-pad').nth(3)),end=await box(frame.locator('.sound-pad').nth(8));
  const auditionsBefore=actions.filter(a=>a.action==='audition').length;
  await page.mouse.move(start.x+start.width/2,start.y+start.height/2);await page.mouse.down();await page.waitForTimeout(2600);
  assert(await frame.locator('.desktop-lifted').count());assert.strictEqual(await page.locator('.drag-ghost').count(),1);
  await page.mouse.move(end.x+end.width/2,end.y+end.height/2,{steps:10});await page.mouse.up();
  await page.click('#save');await page.waitForFunction(()=>document.querySelector('#save').disabled);
  assert.strictEqual(presentation.padSlots[8],'clip-12');assert.strictEqual(presentation.padSlots[3],'clip-8');assert.strictEqual(actions.filter(a=>a.action==='audition').length,auditionsBefore);
  // Fast and slow cross-frame drops at two sizes, with a poll occurring mid-drag.
  for(const width of [900,1920]){await page.setViewportSize({width,height:1080});
    for(const wait of [0,1400]){const source=await box(page.locator('.sound-item').first()),target=await box(frame.locator('.sound-pad').nth(wait?11:0)),id=await page.locator('.sound-item').first().getAttribute('data-id');
      await page.mouse.move(source.x+20,source.y+20);await page.mouse.down();await page.mouse.move(source.x+30,source.y+20);if(wait)await page.waitForTimeout(wait);
      await page.mouse.move(target.x+target.width/2,target.y+target.height/2,{steps:wait?20:1});if(wait)await page.waitForTimeout(wait);await page.mouse.up();await page.click('#save');await page.waitForFunction(()=>document.querySelector('#save').disabled);assert.strictEqual(presentation.padSlots[wait?11:0],id);
    }
  }
  await page.setViewportSize({width:1440,height:1040});
  await page.screenshot({path:path.join(out,'desktop-soundboard.png'),fullPage:true});
  await frame.locator('#app').screenshot({path:path.join(out,'phone-soundboard.png')});
  // Returning a lifted pad to the library clears only its draft assignment.
  const padBox=await box(frame.locator('.sound-pad').nth(8)),trayBox=await box(page.locator('#sound-tray'));
  const assignedBefore=presentation.padSlots[8];await page.mouse.move(padBox.x+20,padBox.y+20);await page.mouse.down();await page.waitForTimeout(500);await page.mouse.move(trayBox.x+20,trayBox.y+20,{steps:8});await page.mouse.up();
  assert(await page.locator('.sound-item[data-id="'+assignedBefore+'"]').count());await page.click('#discard');assert.strictEqual(presentation.padSlots[8],assignedBefore);
  // Escape cancels a lifted sound without an assignment or audition.
  const cancelBox=await box(frame.locator('.sound-pad').nth(8));await page.mouse.move(cancelBox.x+20,cancelBox.y+20);await page.mouse.down();await page.waitForTimeout(500);await page.keyboard.press('Escape');await page.mouse.up();assert.strictEqual(await page.locator('.drag-ghost').count(),0);assert(await page.locator('#save').isDisabled());
  // Discard restores all unsaved edits across all three workspaces.
  const baseline=JSON.stringify(presentation);await page.locator('.sound-item').first().dragTo(frame.locator('.sound-pad').nth(4));
  await page.click('[data-view=mixer]');await page.locator('.app-row button').first().click();await page.click('[data-hide=drag]');
  await page.click('[data-view=layout]');await page.locator('[data-name=devices]').dragTo(page.locator('[data-position=left]'));await page.click('#discard');assert.strictEqual(JSON.stringify(presentation),baseline);assert(await page.locator('#save').isDisabled());
  // Invalid empty center is rejected visibly before any Save request.
  await page.click('[data-view=layout]');await page.locator('[data-name=soundboard]').dragTo(page.locator('[data-position=left]'));const before=actions.length;await page.click('#save');assert((await page.textContent('#notice')).includes('center is empty'));assert.strictEqual(actions.length,before);await page.click('#discard');
  await page.click('[data-view=mixer]');await page.locator('.app-row button').first().click();await page.click('#save');await page.waitForFunction(()=>document.querySelector('#save').disabled);assert.strictEqual(presentation.hiddenApps.length,1);await page.setViewportSize({width:1251,height:834});assert(await page.locator('#hide-tools').evaluate(n=>n.getBoundingClientRect().bottom<innerHeight));assert(await page.locator('#app-tools').evaluate(n=>n.getBoundingClientRect().bottom<innerHeight-50));await page.screenshot({path:path.join(out,'desktop-workspace.png'),fullPage:true});await page.setViewportSize({width:1440,height:1040});
  await page.click('[data-view=routing]');assert((await page.textContent('#receiving-mic')).includes('CABLE Output'));assert((await page.textContent('#route-mode')).includes('direct mic bypass'));await page.locator('#inputId').evaluate(n=>n.options[1]._stable=true);await page.waitForTimeout(500);assert(await page.locator('#inputId').evaluate(n=>n.options[1]._stable));assert(await page.locator('#bypass-path').evaluate(n=>n.classList.contains('active')));
  assert(actions.some(a=>a.action==='check_formats'));assert.strictEqual(await page.locator('[data-wire]').count(),6);assert(await page.locator('[data-wire=mic] .wire-pulse').isVisible());
  assert.strictEqual(await page.locator('#meter-bypass').evaluate(n=>n.value),.4);assert.strictEqual(await page.locator('#meter-voice').evaluate(n=>n.value),.5);assert(await page.locator('[data-signal=voice]').evaluate(n=>n.classList.contains('flowing')));
  await page.click('[data-test=voice]');await page.locator('#notice').waitFor({state:'visible'});await page.waitForFunction(()=>document.querySelector('#notice').hidden,{},{timeout:5000});
  await page.click('[data-test=ears]');await page.click('[data-view=settings]');assert(await page.locator('#notice').isHidden());assert(await page.locator('#runtime').isHidden());
  await page.click('[data-view=routing]');await page.click('#use-mixer-input');await page.waitForFunction(()=>document.querySelector('#route-mode').textContent.includes('virtual mixer'));assert(actions.some(a=>a.action==='use_mixer_input'));assert(await page.locator('#use-mixer-input').isDisabled());
  await page.click('#check-formats');await page.waitForFunction(()=>document.querySelector('#format-results').textContent.includes('Needs attention'));assert((await page.textContent('#format-results')).includes('44.1 kHz'));assert((await page.textContent('#format-results')).includes('48,000 Hz'));
  await page.selectOption('#inputId','receiver');assert(await page.locator('[data-test=voice]').isDisabled());assert(await page.locator('#use-mixer-input').isDisabled());assert((await page.textContent('#audio-detail')).includes('Unsaved'));await page.selectOption('#inputId','mic');await page.click('#connect-route');
  await page.waitForFunction(()=>!document.querySelector('#format-results').textContent.includes('Checking'));
  assert(await page.locator('#format-details').evaluate(n=>n.open));formatsSupported=true;await page.click('#check-formats');await page.waitForFunction(()=>document.querySelector('#format-summary').textContent.includes('compatible'));assert(await page.locator('#format-details').evaluate(n=>!n.open));
  let deliveryPassed=true;
  await page.route('**/admin/api/workspace',async route=>{const request=route.request();if(request.method()==='POST'&&request.postDataJSON().action==='verify_receiver'){await new Promise(resolve=>setTimeout(resolve,150));await route.fulfill({json:{passed:deliveryPassed,receiver:'CABLE Output',scope:'virtual_microphone'}});}else await route.continue();});
  await page.click('.route-help summary');assert((await page.textContent('.route-help')).includes('Input Profile: Studio'));assert((await page.textContent('#discord-input')).includes('CABLE Output'));
  await page.click('#verify-receiver');assert(await page.locator('#verify-receiver').isDisabled());await page.waitForFunction(()=>document.querySelector('#delivery-results').textContent.includes('confirmed at'));assert((await page.textContent('#delivery-results')).includes('Discord'));
  deliveryPassed=false;await page.click('#verify-receiver');await page.waitForFunction(()=>document.querySelector('#delivery-results').textContent.includes('not reliably received'));
  await page.selectOption('#inputId','receiver');assert(await page.locator('#verify-receiver').isDisabled());assert((await page.textContent('#delivery-results')).includes('route changed'));await page.selectOption('#inputId','mic');await page.click('#connect-route');await page.click('.route-help summary');
  await page.waitForFunction(()=>document.querySelector('#notice').hidden,{},{timeout:5000});
  const readsBefore=workspaceReads,signalsBefore=signalReads;soundboard.levels={mic:0,sounds:0,voice:0,ears:0};await page.waitForFunction(()=>document.querySelector('[data-wire=mic] .wire-pulse').hasAttribute('hidden'));
  const started=Date.now();soundboard.levels={mic:.9,sounds:.2,voice:1,ears:.2};await page.waitForFunction(()=>!document.querySelector('[data-wire=mic] .wire-pulse').hasAttribute('hidden'));assert(Date.now()-started<250);await page.waitForTimeout(500);assert(signalReads-signalsBefore>=5);assert(workspaceReads-readsBefore<=1);
  soundboard.streamWarnings={voice:{count:3,last:'Input overflow'}};soundboard.error='Local listening stopped. Microphone and Others continue.';
  await page.waitForFunction(()=>document.querySelector('#audio-detail').textContent.includes('Microphone + sounds: 3'));
  assert((await page.textContent('#audio-detail')).includes('Microphone and Others continue'));assert((await page.textContent('#runtime')).includes('interruptions detected'));
  soundboard.streamWarnings={};soundboard.error='';await page.waitForFunction(()=>!document.querySelector('#audio-detail').textContent.includes('interruptions since'));
  for(const [width,height] of [[1251,834],[1280,800],[1440,900],[1920,1080]]){await page.setViewportSize({width,height});await page.waitForTimeout(80);const geometry=await page.evaluate(()=>{const r=id=>document.querySelector(id).getBoundingClientRect(),mic=r('#node-mic'),sounds=r('#node-sounds'),mixer=r('#node-mixer'),apps=r('#node-apps'),bottom=r('.route-help').bottom;return {sourceTops:Math.abs(mic.top-sounds.top),sourceSymmetry:Math.abs((mic.left+mic.width/2+sounds.left+sounds.width/2)/2-(mixer.left+mixer.width/2)),spine:Math.abs(apps.left+apps.width/2-mixer.left-mixer.width/2),bottom};});assert(geometry.sourceTops<1&&geometry.sourceSymmetry<1&&geometry.spine<1);assert(geometry.bottom<height,JSON.stringify({width,height,geometry}));await page.screenshot({path:path.join(out,'routing-fit-'+width+'.png'),fullPage:true});}
  await page.setViewportSize({width:1440,height:900});await page.screenshot({path:path.join(out,'desktop-routing.png'),fullPage:true});
  for(const width of [390,768,1024,1920]){await page.setViewportSize({width,height:900});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    await page.waitForTimeout(80);
    const intersections=await page.evaluate(()=>{const graph=document.querySelector('#signal-graph').getBoundingClientRect(),ends={mic:['mic','mixer'],sounds:['sounds','mixer'],voice:['mixer','apps'],ears:['mixer','ears'],bypass:['bypass','apps'],'direct-source':['mic','bypass']};const failures=[];for(const group of document.querySelectorAll('[data-wire]')){const path=group.querySelector('.audio-wire');for(const node of document.querySelectorAll('#signal-graph>.panel')){if(ends[group.dataset.wire].includes(node.id==='bypass-path'?'bypass':node.id.replace('node-','')))continue;const r=node.getBoundingClientRect();for(let d=0;d<path.getTotalLength();d+=4){const p=path.getPointAtLength(d),x=p.x+graph.left,y=p.y+graph.top;if(x>r.left+1&&x<r.right-1&&y>r.top+1&&y<r.bottom-1){failures.push(group.dataset.wire+' crosses '+node.id);break;}}}}return failures;});assert.deepStrictEqual(intersections,[]);
    await page.screenshot({path:path.join(out,'routing-'+width+'.png'),fullPage:true});}
  await page.setViewportSize({width:1440,height:1040});const probesBefore=actions.filter(a=>a.action==='check_formats').length;await page.click('#recommended-route');
  for(let i=0;i<30&&actions.filter(a=>a.action==='check_formats').length===probesBefore;i++)await page.waitForTimeout(100);
  assert(actions.filter(a=>a.action==='check_formats').length>probesBefore);await page.waitForFunction(()=>!document.querySelector('#format-results').textContent.includes('Checking'));assert(actions.some(a=>a.action==='recommended_routing'));const probes=actions.filter(a=>a.action==='check_formats').length;await page.waitForTimeout(600);assert.strictEqual(actions.filter(a=>a.action==='check_formats').length,probes);
  await page.setViewportSize({width:1440,height:1040});await page.click('[data-view=mixer]');
  for(const [width,height] of [[873,393],[1024,768],[1280,800],[960,432]]){connectedScreens=[{width,height,name:'Demo device'}];await page.waitForFunction(([w,h])=>document.querySelector('#preview-device').textContent.includes(w+' × '+h),[width,height]);assert.deepStrictEqual(await frame.locator('body').evaluate(()=>[innerWidth,innerHeight]),[width,height]);}
  presentation={...presentation,appOrder:[...presentation.appOrder,...Array.from({length:20},(_,i)=>'Offline app '+i)]};await page.waitForFunction(()=>document.querySelector('#app-list').textContent.includes('Offline app 19'));await page.setViewportSize({width:1251,height:834});assert(await page.locator('#hide-tools').evaluate(n=>n.parentElement.classList.contains('preview-column')&&n.getBoundingClientRect().bottom<innerHeight));assert(await page.locator('#app-list').evaluate(n=>n.scrollHeight>n.clientHeight));assert(await page.locator('#app-tools').evaluate(n=>n.getBoundingClientRect().bottom<innerHeight-45));await page.screenshot({path:path.join(out,'desktop-workspace-long-list.png'),fullPage:true});
  // Long offline names cannot overlap their status or Hide control.
  presentation={...presentation,appOrder:[...presentation.appOrder,'Deckster-v0.5.3-with-a-long-offline-process-name']};
  await page.waitForFunction(()=>document.querySelector('#app-list').textContent.includes('long-offline'));
  for(const width of [390,900,1440,1920]){await page.setViewportSize({width,height:1040});const collisions=await page.locator('.app-row').evaluateAll(rows=>rows.filter(row=>{const name=row.querySelector('.app-name').getBoundingClientRect(),status=row.querySelector('small').getBoundingClientRect(),button=row.querySelector('button').getBoundingClientRect();return name.right>button.left||name.bottom>status.top+.5;}).length);assert.strictEqual(collisions,0);}
  await page.setViewportSize({width:1440,height:1040});await page.click('[data-view=soundboard]');cableAvailable=false;await page.locator('#cable-warning').waitFor({state:'visible'});assert((await page.textContent('#cable-warning')).includes('VB-CABLE'));await page.click('[data-view=routing]');assert(await page.locator('#recommended-route').isDisabled());
  soundboard.runtime='setup_required';await page.waitForFunction(()=>document.querySelector('[data-wire=voice] .audio-wire').classList.contains('disconnected'));assert(await page.locator('[data-wire=voice] .audio-wire').evaluate(n=>n.classList.contains('disconnected')));assert(await page.locator('[data-wire=voice] .wire-pulse').isHidden());
  cableAvailable=true;soundboard.runtime='ready';await page.click('[data-view=connect]');await page.screenshot({path:path.join(out,'desktop-connect.png'),fullPage:true});

  await page.setViewportSize({width:900,height:740});await page.click('[data-view=soundboard]');assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.click('#orientation');await page.waitForTimeout(120);assert(await frame.locator('#app').evaluate(n=>n.clientHeight>n.clientWidth));
  // Remote edits cannot silently overwrite a desktop draft.
  await frame.locator('.sound-pad').nth(5).waitFor();await page.locator('.sound-item').first().dragTo(frame.locator('.sound-pad').nth(5));await page.waitForFunction(()=>document.querySelector('#draft-status').textContent.includes('Unsaved'));presentation={...presentation,revision:presentation.revision+1};await page.waitForTimeout(1500);assert(await page.locator('#save').isDisabled());assert((await page.textContent('#draft-status')).includes('elsewhere'));
  // A hidden workspace stops both network polling lanes and suspends the preview.
  await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'));});
  await page.waitForTimeout(100);const hiddenWorkspaceReads=workspaceReads,hiddenSignalReads=signalReads;
  await page.waitForTimeout(1500);assert.strictEqual(workspaceReads,hiddenWorkspaceReads);assert.strictEqual(signalReads,hiddenSignalReads);
  assert(await frame.locator('body').evaluate(n=>n.classList.contains('suspended')));
  await page.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,value:false});document.dispatchEvent(new Event('visibilitychange'));});
  await page.waitForFunction(()=>!document.querySelector('#phone').contentDocument.body.classList.contains('suspended'));
  for(let i=0;i<20&&workspaceReads===hiddenWorkspaceReads;i++)await page.waitForTimeout(50);assert(workspaceReads>hiddenWorkspaceReads);
  // Saved choices must not advertise an unavailable requested listener as active.
  connectionHealth={requested:{mode:'lan',secure:true},active:{mode:'loopback',secure:false},transitioning:true,ready:true,error:''};
  await page.click('[data-view=connect]');await page.waitForFunction(()=>document.querySelector('#connection-status').textContent.includes('Updating'));
  assert(await page.locator('#pair-qr').isHidden());assert(await page.locator('#pair-code').isHidden());assert(await page.locator('#connect-url').isHidden());
  connectionHealth={...connectionHealth,transitioning:false,ready:false,error:'Address already in use'};
  await page.waitForFunction(()=>document.querySelector('#connection-status').textContent.includes('Address already in use'));
  assert((await page.textContent('#connection-status')).includes('Still using USB'));await page.click('[data-view=settings]');
  assert((await page.textContent('#secure-status')).includes('requested choice is saved'));assert(await page.locator('#secure').isChecked());
  connectionHealth={...connectionHealth,active:{...connectionHealth.requested},ready:true,error:''};
  await page.click('[data-view=connect]');await page.locator('#pair-qr').waitFor({state:'visible'});assert(await page.locator('#connection-status').isHidden());
  assert.deepStrictEqual(errors,[]);console.log('desktop workspace checks passed; screenshots in '+out);
}catch(e){if(browser){const page=browser.contexts()[0]?.pages()[0];if(page){await page.screenshot({path:path.join(out,'desktop-check-failure.png'),fullPage:true});console.error(await page.locator('#draft-status').textContent());}}throw e;}finally{if(browser)await browser.close();server.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
