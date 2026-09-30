/* Real shared-renderer desktop interactions, no live audio or visible windows. */
const assert=require('assert'),fs=require('fs'),http=require('http'),path=require('path');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'../web'),out=path.resolve(__dirname,'../release/verify-feedback-2026-09-30');
const actions=[],errors=[];
const clips=['Air Horn','Rimshot','Sad Trombone','Applause','Crickets','Record Scratch','Drum Roll','Censor Bleep','Success','Wrong','Bell','Pop','Boing','Whoosh','Click','Sparkle'].map((label,i)=>({id:'clip-'+i,label,emoji:['📣','🥁','🎺','👏','🦗','💿','🥁','🔇','✨','❌','🔔','💥','🌀','💨','🖱','✨'][i],duration:1.4,gain:1,voice:true,ears:true}));
let presentation={revision:0,appOrder:[],hiddenApps:[],pages:{soundboard:'top',devices:'right',media:'bottom'},padSlots:clips.slice(0,12).map(c=>c.id),hideInteraction:'drag'};
const sessions=[['discord','Discord',.72],['spotify','Spotify',.48],['chrome','Google Chrome',.35],['game','Your game',.86]].map(([id,appLabel,level])=>({id,appLabel,level,active:true}));
const devices={speakerMaster:{level:.68,muted:false},micMaster:{level:.92,muted:false},inputs:[{id:'mic',name:'Microphone (Realtek Audio)'}],outputs:[{id:'cable',name:'CABLE Input (VB-Audio Virtual Cable)'},{id:'speaker',name:'Headphones',isDefault:true}]};
const soundboard={clips,config:{inputId:'mic',voiceOutputId:'cable',earsOutputId:'speaker'},inputs:devices.inputs,outputs:devices.outputs,runtime:'ready',error:''};
const snap=()=>({t:'snapshot',presentation,sessions,devices,soundboard,media:[]});
const server=http.createServer((req,res)=>{
  const pathname=req.url.split('?')[0];
  if(pathname==='/admin/api/workspace'){
    res.setHeader('Content-Type','application/json');
    if(req.method==='GET'){res.end(JSON.stringify({admin:{version:'0.6.3',mode:'loopback',pairCode:'123456',connectUrl:'https://localhost:8765/',connectNote:'USB connected',devices:[],secure:true},snapshot:snap(),presentation:{presentation,sessions,devices,clips},soundboard,receivingMic:'CABLE Output (VB-Audio Virtual Cable)'}));return;}
    let raw='';req.on('data',d=>raw+=d);req.on('end',()=>{const body=JSON.parse(raw);actions.push(body);if(body.action==='presentation'){if(body.baseRevision!==presentation.revision){res.statusCode=409;res.end(JSON.stringify({error:'Layout changed elsewhere'}));return;}presentation={...presentation,...body.changes,revision:presentation.revision+1};res.end(JSON.stringify(presentation));}else res.end('{"ok":true}');});return;
  }
  if(pathname==='/admin/qr'){res.writeHead(204);res.end();return;}
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
  const start=await frame.locator('.sound-pad').nth(3).boundingBox(),end=await frame.locator('.sound-pad').nth(8).boundingBox();
  const auditionsBefore=actions.filter(a=>a.action==='audition').length;
  await page.mouse.move(start.x+start.width/2,start.y+start.height/2);await page.mouse.down();await page.waitForTimeout(2600);
  assert(await frame.locator('.desktop-lifted').count());assert.strictEqual(await page.locator('.drag-ghost').count(),1);
  await page.mouse.move(end.x+end.width/2,end.y+end.height/2,{steps:10});await page.mouse.up();
  await page.click('#save');await page.waitForFunction(()=>document.querySelector('#save').disabled);
  assert.strictEqual(presentation.padSlots[8],'clip-12');assert.strictEqual(presentation.padSlots[3],'clip-8');assert.strictEqual(actions.filter(a=>a.action==='audition').length,auditionsBefore);
  // Fast and slow cross-frame drops at two sizes, with a poll occurring mid-drag.
  for(const width of [900,1920]){await page.setViewportSize({width,height:1080});
    for(const wait of [0,1400]){const source=await page.locator('.sound-item').first().boundingBox(),target=await frame.locator('.sound-pad').nth(wait?11:0).boundingBox(),id=await page.locator('.sound-item').first().getAttribute('data-id');
      await page.mouse.move(source.x+20,source.y+20);await page.mouse.down();await page.mouse.move(source.x+30,source.y+20);if(wait)await page.waitForTimeout(wait);
      await page.mouse.move(target.x+target.width/2,target.y+target.height/2,{steps:wait?20:1});if(wait)await page.waitForTimeout(wait);await page.mouse.up();await page.click('#save');await page.waitForFunction(()=>document.querySelector('#save').disabled);assert.strictEqual(presentation.padSlots[wait?11:0],id);
    }
  }
  await page.setViewportSize({width:1440,height:1040});
  await page.screenshot({path:path.join(out,'desktop-soundboard.png'),fullPage:true});
  // Returning a lifted pad to the library clears only its draft assignment.
  const padBox=await frame.locator('.sound-pad').nth(8).boundingBox(),trayBox=await page.locator('#sound-tray').boundingBox();
  const assignedBefore=presentation.padSlots[8];await page.mouse.move(padBox.x+20,padBox.y+20);await page.mouse.down();await page.waitForTimeout(500);await page.mouse.move(trayBox.x+20,trayBox.y+20,{steps:8});await page.mouse.up();
  assert(await page.locator('.sound-item[data-id="'+assignedBefore+'"]').count());await page.click('#discard');assert.strictEqual(presentation.padSlots[8],assignedBefore);
  // Escape cancels a lifted sound without an assignment or audition.
  const cancelBox=await frame.locator('.sound-pad').nth(8).boundingBox();await page.mouse.move(cancelBox.x+20,cancelBox.y+20);await page.mouse.down();await page.waitForTimeout(500);await page.keyboard.press('Escape');await page.mouse.up();assert.strictEqual(await page.locator('.drag-ghost').count(),0);assert(await page.locator('#save').isDisabled());
  // Discard restores all unsaved edits across all three workspaces.
  const baseline=JSON.stringify(presentation);await page.locator('.sound-item').first().dragTo(frame.locator('.sound-pad').nth(4));
  await page.click('[data-view=mixer]');await page.locator('.app-row button').first().click();await page.click('[data-hide=drag]');
  await page.click('[data-view=layout]');await page.locator('[data-name=devices]').dragTo(page.locator('[data-position=left]'));await page.click('#discard');assert.strictEqual(JSON.stringify(presentation),baseline);assert(await page.locator('#save').isDisabled());
  // Invalid empty center is rejected visibly before any Save request.
  await page.click('[data-view=layout]');await page.locator('[data-name=soundboard]').dragTo(page.locator('[data-position=left]'));const before=actions.length;await page.click('#save');assert((await page.textContent('#notice')).includes('center is empty'));assert.strictEqual(actions.length,before);await page.click('#discard');
  await page.click('[data-view=mixer]');await page.locator('.app-row button').first().click();await page.click('#save');await page.waitForFunction(()=>document.querySelector('#save').disabled);assert.strictEqual(presentation.hiddenApps.length,1);await page.setViewportSize({width:1920,height:1080});assert(await page.locator('#app-tools').evaluate(n=>n.getBoundingClientRect().bottom<innerHeight-50));await page.screenshot({path:path.join(out,'desktop-workspace.png'),fullPage:true});await page.setViewportSize({width:1440,height:1040});
  await page.click('[data-view=routing]');assert((await page.textContent('#receiving-mic')).includes('CABLE Output'));await page.screenshot({path:path.join(out,'desktop-routing.png'),fullPage:true});
  await page.setViewportSize({width:900,height:740});await page.click('[data-view=soundboard]');assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.click('#orientation');await page.waitForTimeout(120);assert(await frame.locator('#app').evaluate(n=>n.clientHeight>n.clientWidth));
  // Remote edits cannot silently overwrite a desktop draft.
  await page.locator('.sound-item').first().dragTo(frame.locator('.sound-pad').nth(5));presentation={...presentation,revision:presentation.revision+1};await page.waitForTimeout(1500);assert(await page.locator('#save').isDisabled());assert((await page.textContent('#draft-status')).includes('elsewhere'));
  assert.deepStrictEqual(errors,[]);console.log('desktop workspace checks passed; screenshots in '+out);
}finally{if(browser)await browser.close();server.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
