/* Local desktop workspace; phone previews share the shipped phone renderer. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id), all = q => [...document.querySelectorAll(q)];
  const clone = x => JSON.parse(JSON.stringify(x));
  let state, draft, dirty = false, conflict = false, page = 'mixer', view = 'layout';
  let gesture = null, ghost = null, dropPad = null, librarySignature = '', appsSignature = '';
  let movingPage = null, portrait = false, routeDirty = false, previewReady = false, polling = false;
  const titles = {layout:['Page layout','Arrange the pages around your home screen.'],mixer:['Phone workspace','Manage your apps and try navigation in every direction.'],soundboard:['Soundboard','Find your sound. Give it a place on your phone.'],routing:['Audio routing','Your voice and sounds, connected to the right places.'],connect:['Connect & devices','A simple link between your phone and PC.'],settings:['Settings','Make Deckster fit your setup.']};
  const glyphs = {mixer:'◉',soundboard:'♫',devices:'⌁',media:'▶'};
  function node(tag, cls, text) { const n=document.createElement(tag); if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n; }
  function soundIcon(clip) {
    const icon=node('span','emoji',clip.emoji||'♫');
    if(/^\/static\/meme-art\/[a-z0-9-]+\.webp$/.test(clip.artwork||'')){
      const img=node('img','sound-art');img.src=clip.artwork;img.alt='';img.loading='lazy';img.decoding='async';img.draggable=false;
      img.onerror=()=>{icon.textContent=clip.emoji||'♫';};icon.replaceChildren(img);
    }
    return icon;
  }
  let noticeTimer, routeSignature='', formatSignature='', formatTimer, formatChecking=false, formatGeneration=0, wireGeometry='';
  let signalPolling=false, signalEpoch=0, signalRetryAt=0;
  let deliveryChecking=false, deliverySignature='';
  let refreshTimer=null, signalTimer=null, latestWorkspace=null, formatsPending=false;
  function notify(message, error=false, scope=view) { if(scope!==view)return;clearTimeout(noticeTimer);$('notice').hidden=false;$('notice').classList.toggle('error',error);$('notice').textContent=message;noticeTimer=setTimeout(()=>$('notice').hidden=true,error?8000:3500); }
  async function request(url, body) { const r=await fetch(url, body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {cache:'no-store'});const data=await r.json();if(!r.ok){const error=new Error(data.error||data.msg||`Request failed (${r.status})`);error.commandResult=data.commandResult;throw error;}return data; }
  async function action(action, args={}) { const scope=view;try{return await request('/admin/api/workspace',{action,...args});}catch(e){notify(e.message,true,scope);throw e;} }
  function run(fn) { return (...args)=>Promise.resolve().then(()=>fn(...args)).catch(()=>{}); }
  function currentPages() { return {mixer:'center',...draft.pages}; }
  function changed() {dirty=true;$('notice').hidden=true;render();}
  function render() {
    if(!draft||document.hidden)return;
    $('save').disabled=!dirty||conflict; $('draft-status').textContent=conflict?'Phone layout changed elsewhere. Discard your draft to reload.':dirty?'Unsaved changes · Save to update your phone':'All changes saved';
    document.querySelector('footer').classList.toggle('unsaved',dirty);$('discard').disabled=!dirty;
    all('[data-hide]').forEach(b=>{const active=b.dataset.hide===(draft.hideInteraction||'drag');b.classList.toggle('active',active);b.setAttribute('aria-pressed',active);});
    renderMap();renderLibrary();renderApps();sendPreview();
  }
  function sendPreview() {
    if(document.hidden||!previewReady||!state||!draft||gesture||!['layout','mixer','soundboard'].includes(view))return;
    const snapshot=clone(state.snapshot);snapshot.presentation=clone(draft);snapshot.soundboard=clone(state.soundboard);
    $('phone').contentWindow.postMessage({t:'preview-state',snapshot,page},location.origin);
  }
  function resizePreview() {
    if(document.hidden)return;
    const screen=state?.connectedScreens?.[0],size=screen?[screen.width,screen.height]:[960,432];const width=portrait?size[1]:size[0],height=portrait?size[0]:size[1];
    $('preview-device').textContent=screen?`${screen.name} · ${screen.width} × ${screen.height} · detected`:'Connect your phone · preview size follows automatically';
    const viewport=$('preview-viewport');viewport.style.aspectRatio=`${width}/${height}`;
    const maxPortraitWidth=Math.min(300,Math.max(320,innerHeight-320)*width/height);
    viewport.style.maxWidth=height>width?maxPortraitWidth+'px':'800px';viewport.style.margin='auto';
    const frame=$('phone');frame.style.width=width+'px';frame.style.height=height+'px';frame.style.transform=`scale(${viewport.clientWidth/width})`;
  }
  function choosePage(name) {page=name;all('[data-page]').forEach(b=>{b.classList.toggle('active',b.dataset.page===name);b.setAttribute('aria-pressed',b.dataset.page===name);});sendPreview();}
  function chooseView(name) {
    clearTimeout(noticeTimer);$('notice').hidden=true;view=name;signalEpoch++;document.querySelector('main').dataset.view=name;$('runtime').hidden=name!=='routing';all('[data-view]').forEach(b=>{b.classList.toggle('active',b.dataset.view===name);if(b.dataset.view===name)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});
    $('title').textContent=titles[name][0];$('subtitle').textContent=titles[name][1];
    const editor=['layout','mixer','soundboard'].includes(name);$('editor').hidden=!editor;document.querySelector('footer').hidden=!editor;$('save').hidden=!editor;$('discard').hidden=!editor;
    $('editor').dataset.view=name;
    for(const key of ['routing','connect','settings'])$(key+'-view').hidden=name!==key;
    syncPreviewVisibility();
    $('cable-warning').hidden=!['routing','soundboard'].includes(name)||state?.cableAvailable!==false;
    $('layout-tools').hidden=name!=='layout';$('app-tools').hidden=name!=='mixer';$('hide-tools').hidden=name!=='mixer';$('sound-tray').hidden=name!=='soundboard';
    if(name==='soundboard')choosePage('soundboard');if(name==='mixer')choosePage('mixer');
    resizePreview();render();
    if(name==='routing'){requestAnimationFrame(drawWires);pollSignals();}
  }
  function movePage(name, target) {
    const pages=currentPages(), prior=pages[name], occupant=Object.keys(pages).find(p=>pages[p]===target);
    if(prior===target)return;pages[name]=target;if(occupant)pages[occupant]=prior;draft.pages=pages;movingPage=null;changed();
  }
  function renderMap() {
    const host=$('page-map');host.replaceChildren();const positions={top:[1,2],left:[2,1],center:[2,2],right:[2,3],bottom:[3,2]},pages=currentPages();
    for(const [position,[row,col]] of Object.entries(positions)){
      const slot=node('div','map-slot');slot.dataset.position=position;slot.style.gridRow=row;slot.style.gridColumn=col;
      const name=Object.keys(pages).find(n=>pages[n]===position);
      if(name){const card=node('button','page-card',`${glyphs[name]}  ${name[0].toUpperCase()+name.slice(1)}`);card.dataset.name=name;card.draggable=true;card.classList.toggle('selected',movingPage===name);card.setAttribute('aria-label',`${name}, ${position}. Select, then select a destination.`);
        card.onclick=()=>{if(movingPage&&movingPage!==name)movePage(movingPage,position);else{movingPage=name;choosePage(name);renderMap();}};
        card.ondragstart=e=>{e.dataTransfer.setData('application/deckster-page',name);e.dataTransfer.effectAllowed='move';};slot.append(card);
      }else{const empty=node('button','text-button',position==='center'?'Empty center':'＋ Empty');empty.onclick=()=>{if(movingPage)movePage(movingPage,position);};slot.append(empty);}
      slot.ondragover=e=>{if(e.dataTransfer.types.includes('application/deckster-page')){e.preventDefault();slot.classList.add('over');}};
      slot.ondragleave=()=>slot.classList.remove('over');slot.ondrop=e=>{e.preventDefault();const name=e.dataTransfer.getData('application/deckster-page');if(glyphs[name])movePage(name,position);};host.append(slot);
    }
  }
  function putClip(slot,id) {
    if(!state.soundboard.clips.some(c=>c.id===id))return;
    if(slot<0||slot>=12)return;
    const source=draft.padSlots.indexOf(id);if(source===slot)return;if(source>=0)draft.padSlots[source]=draft.padSlots[slot];draft.padSlots[slot]=id;changed();
  }
  async function audition(id) {await action('audition',{id});notify('Playing on your PC headphones or speakers.',false,'soundboard');}
  function renderLibrary() {
    const query=$('sound-search').value.toLowerCase(),host=$('library');
    // Keep import/manage controls above the fixed footer as collections grow.
    const available=innerHeight-host.getBoundingClientRect().top-document.querySelector('footer').offsetHeight-$('import-sound').offsetHeight-$('manage-library').offsetHeight-52;
    host.style.maxHeight=view==='soundboard'&&innerWidth>720?Math.max(100,Math.floor(available))+'px':'';
    const collection=$('sound-collection'),selected=collection.value;
    const names=[...new Set(state.soundboard.clips.map(c=>c.collection||'My sounds'))].sort();
    if(collection.dataset.signature!==JSON.stringify(names)){
      collection.replaceChildren();const all=node('option','','All collections');all.value='';collection.append(all);
      for(const name of names){const opt=node('option','',name);opt.value=name;collection.append(opt);}
      collection.value=names.includes(selected)?selected:'';collection.dataset.signature=JSON.stringify(names);
    }
    const unused=state.soundboard.clips.filter(c=>!draft.padSlots.includes(c.id));$('library-count').textContent=unused.length;
    const clips=unused.filter(c=>(!collection.value||(c.collection||'My sounds')===collection.value)&&[c.label,c.collection,...(c.tags||[])].join(' ').toLowerCase().includes(query));
    const signature=JSON.stringify(clips);if(signature===librarySignature)return;librarySignature=signature;host.replaceChildren();
    for(const clip of clips){const item=node('div','sound-item');item.dataset.id=clip.id;item.tabIndex=0;item.setAttribute('aria-label',`Listen to ${clip.label}. Drag onto a pad to assign.`);
      item.append(soundIcon(clip));const label=node('div','sound-name',clip.label);label.append(node('small','',`${Number(clip.duration||0).toFixed(1)}s · ${clip.collection||'Unassigned'}`));item.append(label);
      item.onpointerdown=e=>startGesture(e,item,clip.id,false);item.onkeydown=e=>{if(e.key==='Enter')run(()=>audition(clip.id))();};host.append(item);
    }
    if(!clips.length)host.append(node('div','empty',query||collection.value?'No unassigned sounds match. Check Manage sound library for assigned sounds.':'All sounds are assigned. Hold a pad and drag it here to unassign.'));
  }
  function point(e) {
    if(e.target.ownerDocument===document)return {x:e.clientX,y:e.clientY};
    const r=$('phone').getBoundingClientRect(),scale=r.width/parseFloat($('phone').style.width);
    return {x:r.left+e.clientX*scale,y:r.top+e.clientY*scale};
  }
  function targetPad(x,y) {
    const frame=$('phone'),r=frame.getBoundingClientRect(),scale=r.width/parseFloat(frame.style.width);
    if(x<r.left||x>r.right||y<r.top||y>r.bottom)return null;
    return frame.contentDocument.elementFromPoint((x-r.left)/scale,(y-r.top)/scale)?.closest('.sound-pad');
  }
  function lift() {
    if(!gesture||gesture.cancelled)return;gesture.lifted=true;gesture.source.classList.add('desktop-lifted');
    document.body.classList.add('grabbing');$('phone').contentDocument.body.classList.add('grabbing');
    const clip=state.soundboard.clips.find(c=>c.id===gesture.id);ghost=node('div','drag-ghost',`${clip.emoji||'♫'}  ${clip.label}`);document.body.append(ghost);moveGhost(gesture.x,gesture.y);
  }
  function moveGhost(x,y) {if(ghost){ghost.style.left=(x+12)+'px';ghost.style.top=(y+12)+'px';}}
  function startGesture(e,source,id,hold) {
    if(e.button!==0||gesture||view!=='soundboard')return;
    e.preventDefault();e.stopImmediatePropagation();const p=point(e);
    gesture={source,id,x:p.x,y:p.y,lifted:false,hold,pointerId:e.pointerId};
    $('phone').contentDocument.body.dataset.desktopGesture='active';
    source.setPointerCapture(e.pointerId);if(hold)gesture.timer=setTimeout(lift,450);
  }
  function moveGesture(e) {
    if(!gesture||e.pointerId!==gesture.pointerId)return;
    e.preventDefault();e.stopImmediatePropagation();const p=point(e),distance=Math.hypot(p.x-gesture.x,p.y-gesture.y);
    if(!gesture.lifted&&distance>5){if(gesture.hold){clearTimeout(gesture.timer);gesture.cancelled=true;}else lift();}
    if(!gesture.lifted)return;moveGhost(p.x,p.y);
    dropPad?.classList.remove('desktop-drop-over');dropPad=targetPad(p.x,p.y);dropPad?.classList.add('desktop-drop-over');
  }
  function finishGesture(e,cancel=false) {
    if(!gesture||e.pointerId!==gesture.pointerId)return;
    e.preventDefault();e.stopImmediatePropagation();const g=gesture,p=point(e),pad=targetPad(p.x,p.y);
    clearTimeout(g.timer);g.source.classList.remove('desktop-lifted');dropPad?.classList.remove('desktop-drop-over');dropPad=null;ghost?.remove();ghost=null;
    document.body.classList.remove('grabbing');$('phone').contentDocument.body.classList.remove('grabbing');
    gesture=null;delete $('phone').contentDocument.body.dataset.desktopGesture;
    if(g.source.hasPointerCapture(e.pointerId))g.source.releasePointerCapture(e.pointerId);
    if(!cancel&&g.lifted&&pad)putClip([...$('phone').contentDocument.querySelectorAll('.sound-pad')].indexOf(pad),g.id);
    else if(!cancel&&g.lifted&&g.hold){const r=$('sound-tray').getBoundingClientRect();if(p.x>=r.left&&p.x<=r.right&&p.y>=r.top&&p.y<=r.bottom){draft.padSlots[draft.padSlots.indexOf(g.id)]=null;changed();}}
    else if(!cancel&&!g.lifted&&!g.cancelled)run(()=>audition(g.id))();sendPreview();
  }
  function bindGestureEvents(doc){doc.addEventListener('pointermove',moveGesture,true);doc.addEventListener('pointerup',e=>finishGesture(e),true);doc.addEventListener('pointercancel',e=>finishGesture(e,true),true);}
  function cancelGesture(){if(gesture)finishGesture({pointerId:gesture.pointerId,target:document.body,clientX:gesture.x,clientY:gesture.y,preventDefault(){},stopImmediatePropagation(){}},true);}
  window.addEventListener('blur',cancelGesture);window.addEventListener('resize',cancelGesture);
  document.addEventListener('keydown',e=>{if(e.key==='Escape')cancelGesture();});
  function renderApps(){const host=$('app-list');const live=state.presentation.sessions,ids=[...new Set([...draft.appOrder,...live.map(s=>s.id),...draft.hiddenApps])];$('app-count').textContent=live.length+' live';const signature=JSON.stringify([ids,draft.hiddenApps,live.map(s=>[s.id,s.appLabel,s.iconKey])]);if(signature===appsSignature){host.querySelectorAll('.app-row').forEach(row=>{const app=live.find(s=>s.id===row.dataset.id);row.querySelector('small').textContent=app?Math.round((app.level||0)*100)+'%':'Offline';});return;}appsSignature=signature;host.replaceChildren();
    for(const id of ids){const app=live.find(s=>s.id===id),hidden=draft.hiddenApps.includes(id);const row=node('div','app-row'+(hidden?' hidden-app':''));row.draggable=true;row.dataset.id=id;row.append(node('span','grip','⠿'));if(app&&app.iconKey){const img=node('img');img.src='/icon/'+encodeURIComponent(app.iconKey);img.alt='';row.append(img);}row.append(node('span','app-name',app?(app.appLabel||app.label):id));row.append(node('small','',app?Math.round((app.level||0)*100)+'%':'Offline'));const b=node('button','',hidden?'Show':'⊘ Hide');b.onclick=()=>{draft.hiddenApps=hidden?draft.hiddenApps.filter(x=>x!==id):[...draft.hiddenApps,id];changed();};row.append(b);row.ondragstart=e=>e.dataTransfer.setData('application/deckster-app',id);row.ondragover=e=>{if(e.dataTransfer.types.includes('application/deckster-app'))e.preventDefault();};row.ondrop=e=>{e.preventDefault();const source=e.dataTransfer.getData('application/deckster-app');if(!ids.includes(source)||source===id)return;const order=ids.filter(x=>x!==source);order.splice(order.indexOf(id),0,source);draft.appOrder=order;changed();};host.append(row);}
    if(!ids.length)host.append(node('div','empty','Open an app that plays audio to see it here.'));
  }
  function wirePreview() {
    const doc=$('phone').contentDocument;if(!doc||doc._desktopWired)return;doc._desktopWired=true;bindGestureEvents(doc);
    doc.addEventListener('pointerdown',e=>{const pad=e.target.closest('.sound-pad');if(!pad||view!=='soundboard')return;const slot=[...doc.querySelectorAll('.sound-pad')].indexOf(pad),id=draft.padSlots[slot];if(id)startGesture(e,pad,id,true);},true);
    doc.addEventListener('click',e=>{if(view==='soundboard'&&e.target.closest('.sound-pad:not(.sound-pad-plus)')){e.preventDefault();e.stopImmediatePropagation();}},true);
    doc.addEventListener('keydown',e=>{if(e.key==='Escape')cancelGesture();const pad=e.target.closest('.sound-pad');if(!pad||view!=='soundboard')return;const slot=[...doc.querySelectorAll('.sound-pad')].indexOf(pad),id=draft.padSlots[slot];if(!id)return;if(e.key==='Enter'){e.preventDefault();run(()=>audition(id))();}if(e.ctrlKey){const delta={ArrowLeft:-1,ArrowRight:1,ArrowUp:-4,ArrowDown:4}[e.key];if(delta&&slot+delta>=0&&slot+delta<12){e.preventDefault();putClip(slot+delta,id);}}},true);
    doc.addEventListener('dblclick',e=>{const pad=e.target.closest('.sound-pad');if(pad&&view==='soundboard'){const slot=[...doc.querySelectorAll('.sound-pad')].indexOf(pad);openClip(draft.padSlots[slot]);}},true);
    doc.addEventListener('dragover',e=>{if(e.target.closest('.sound-pad')&&e.dataTransfer.types.includes('application/deckster-clip'))e.preventDefault();});
    doc.addEventListener('drop',e=>{const pad=e.target.closest('.sound-pad');if(!pad)return;e.preventDefault();putClip([...doc.querySelectorAll('.sound-pad')].indexOf(pad),e.dataTransfer.getData('application/deckster-clip'));});
  }
  bindGestureEvents(document);
  window.addEventListener('message',e=>{
    if(e.origin!==location.origin||e.source!==$('phone').contentWindow||!e.data)return;
    if(e.data.t==='preview-ready'){previewReady=true;wirePreview();syncPreviewVisibility();sendPreview();return;}
    if(e.data.t==='preview-page'){page=e.data.page;all('[data-page]').forEach(b=>b.classList.toggle('active',b.dataset.page===page));return;}
    if(e.data.t!=='preview-command'||document.hidden||!['layout','mixer','soundboard'].includes(view))return;const cmd=e.data.command;
    if(cmd.t==='presentation_update'){Object.assign(draft,clone(cmd.changes));changed();}
    else if(cmd.t==='soundboard_play')run(()=>audition(cmd.id||cmd.clipId))();
    else if(cmd.t==='soundboard_stop_all')run(()=>action('stop'))();
    else if(cmd.t==='soundboard_update_clip')run(async()=>{try{await action('clip',{id:cmd.clipId,changes:cmd.changes});await refresh();}catch(e){$('phone').contentWindow.postMessage({t:'preview-error',message:e.message},location.origin);}})();
    else if(['set_volume','set_mute','set_default_output','set_default_input','media_control','app_input_mute','set_app_input_binding','clear_app_input_binding'].includes(cmd.t))run(async()=>{try{const result=await action('control',{command:cmd});if(result.commandResult)$('phone').contentWindow.postMessage({t:'preview-result',result:result.commandResult},location.origin);}catch(e){if(e.commandResult)$('phone').contentWindow.postMessage({t:'preview-result',result:e.commandResult},location.origin);throw e;}})();
    else notify('This action is unavailable in the preview.',true);
  });
  async function refresh(cached){if(polling||gesture||document.hidden)return;polling=true;try{
    const next=cached||await request('/admin/api/workspace');if(document.hidden){latestWorkspace=next;return;}if(gesture)return;const redraw=!draft||(!dirty&&JSON.stringify(draft)!==JSON.stringify(next.presentation.presentation));state=next;
    if(!dirty){draft=clone(next.presentation.presentation);conflict=false;}else if(next.presentation.presentation.revision!==draft.revision){conflict=true;}
    const activeLink=next.admin.connection?.active?.mode||next.admin.mode;
    $('connection-label').textContent='PC connected · '+(activeLink==='loopback'?'USB':'Wi-Fi');$('connection-dot').classList.remove('off');$('version').textContent='DECKSTER '+next.admin.version;
    const hasWarnings=Object.values(next.soundboard.streamWarnings||{}).some(w=>w.count>0);
    $('runtime').textContent=next.soundboard.runtime==='ready'?(hasWarnings?'● Audio connected · interruptions detected':'● Audio connected'):'Audio needs attention';$('runtime').classList.toggle('warn',next.soundboard.runtime!=='ready'||hasWarnings||!!next.soundboard.error);
    const nextRouteSignature=JSON.stringify([next.soundboard.inputs,next.soundboard.outputs,next.soundboard.config]);if(!routeDirty&&routeSignature!==nextRouteSignature){routeSignature=nextRouteSignature;for(const key of ['inputId','voiceOutputId','earsOutputId']){const select=$(key),devices=key==='inputId'?next.soundboard.inputs:next.soundboard.outputs;select.replaceChildren();const blank=node('option','',key==='earsOutputId'?'Off · no local monitoring':'Choose a device');blank.value='';select.append(blank);for(const d of devices||[]){const opt=node('option','',d.name);opt.value=d.id;select.append(opt);}select.value=next.soundboard.config[key]||'';}}
    $('receiving-mic').textContent=next.receivingMic;$('receiving-mic').title=next.receivingMic;
    const interruptions=Object.entries(next.soundboard.streamWarnings||{}).filter(([,w])=>w.count>0).map(([bus,w])=>`${bus==='voice'?'Microphone + sounds':'Local listening'}: ${w.count}`);
    $('audio-detail').textContent=[next.soundboard.error,interruptions.length?'Audio interruptions since this connection — '+interruptions.join('; ')+'.':'',`Audio status: ${next.soundboard.runtime}. These meters measure audio inside Deckster, not reception in another app. Use your game or call's microphone test to confirm delivery.`].filter(Boolean).join(' ');renderRouting(next);
    $('cable-warning').hidden=!['routing','soundboard'].includes(view)||next.cableAvailable!==false;
    resizePreview();scheduleFormats();
    $('firewall-controls').hidden=!next.admin.firewallNeeded;$('pair-code').textContent=next.admin.pairCode;$('pair-qr').src='/admin/qr?code='+encodeURIComponent(next.admin.pairCode);$('connect-url').textContent=next.admin.connectUrl;$('connect-note').textContent=next.admin.connectNote;
    renderConnectionStatus(next.admin);
    all('[data-mode]').forEach(b=>b.classList.toggle('active',b.dataset.mode===next.admin.mode));$('autostart').checked=next.admin.autostart;$('secure').checked=next.admin.secure;
    const devices=$('device-list');devices.replaceChildren();for(const d of next.admin.devices){const row=node('div','device',d.name||d.id);const b=node('button','','Revoke');b.onclick=run(async()=>{await request('/admin/api/device/revoke',{id:d.id});await refresh();});row.append(b);devices.append(row);}if(!next.admin.devices.length)devices.append(node('p','','No phones paired yet.'));
    if(redraw)render();else{renderLibrary();renderApps();sendPreview();$('save').disabled=!dirty||conflict;if(conflict)$('draft-status').textContent='Phone layout changed elsewhere. Discard your draft to reload.';}
  }catch(e){$('connection-dot').classList.add('off');$('connection-label').textContent='PC connection lost';notify(e.message,true);}finally{polling=false;}}
  $('save').onclick=run(async()=>{if(!Object.values(currentPages()).includes('center')){notify('The center is empty. Move a page into the center before saving.',true);return;}const changes={appOrder:draft.appOrder,hiddenApps:draft.hiddenApps,pages:draft.pages,padSlots:draft.padSlots,hideInteraction:draft.hideInteraction||'drag'};const saved=await action('presentation',{changes,baseRevision:draft.revision});draft=saved;dirty=false;conflict=false;render();notify('Saved to your phone.');await refresh();});
  $('discard').onclick=()=>{dirty=false;conflict=false;draft=clone(state.presentation.presentation);render();notify('Draft discarded. The saved phone layout is restored.');};
  all('[data-view]').forEach(b=>b.onclick=()=>chooseView(b.dataset.view));all('[data-page]').forEach(b=>b.onclick=()=>choosePage(b.dataset.page));all('[data-hide]').forEach(b=>b.onclick=()=>{draft.hideInteraction=b.dataset.hide;changed();});
  $('sound-search').oninput=renderLibrary;
  $('sound-collection').onchange=renderLibrary;
  $('orientation').onclick=()=>{portrait=!portrait;resizePreview();};
  $('import-sound').onclick=()=> $('sound-files').click();
  $('sound-files').onchange=run(async()=>{for(const file of $('sound-files').files){const body=new FormData();body.append('file',file);const response=await fetch('/admin/api/import',{method:'POST',body});const result=await response.json();if(!response.ok){notify(result.error,true);throw new Error(result.error);}}$('sound-files').value='';await refresh();render();notify('Sounds added to your library.');});
  $('restore-layout').onclick=run(async()=>{const saved=await action('defaults',{baseRevision:draft.revision});draft=saved;dirty=false;conflict=false;await refresh();render();notify('Phone defaults restored. Your library, routes and pairing are preserved.');});
  $('restore-sounds').onclick=run(async()=>{await action('starter_sounds');await refresh();render();notify('Starter sounds restored. Imported sounds are preserved.');});
  $('manage-library').onclick=()=>{const host=$('clip-list');host.replaceChildren();for(const clip of state.soundboard.clips){const button=node('button','secondary');button.append(soundIcon(clip),node('span','',clip.label));button.onclick=()=>openClip(clip.id);host.append(button);}$('library-dialog').showModal();};
  $('close-library').onclick=()=> $('library-dialog').close();
  function openClip(id){const clip=state.soundboard.clips.find(c=>c.id===id);if(!clip)return;$('clip-dialog').dataset.id=id;for(const key of ['label','emoji','gain'])$('clip-'+key).value=clip[key];for(const key of ['voice','ears'])$('clip-'+key).checked=clip[key];if(!$('clip-dialog').open)$('clip-dialog').showModal();}
  $('close-clip').onclick=()=> $('clip-dialog').close();
  $('save-clip').onclick=run(async()=>{const changes={label:$('clip-label').value,emoji:$('clip-emoji').value,gain:Number($('clip-gain').value),voice:$('clip-voice').checked,ears:$('clip-ears').checked};await action('clip',{id:$('clip-dialog').dataset.id,changes});$('clip-dialog').close();await refresh();render();notify('Sound settings saved.');});
  $('delete-clip').onclick=run(async()=>{if(dirty){$('clip-dialog').close();$('library-dialog').close();notify('Save or discard your layout draft before deleting a library sound.',true);return;}await action('remove_clip',{id:$('clip-dialog').dataset.id});$('clip-dialog').close();$('library-dialog').close();await refresh();render();notify('Sound removed from the library.');});
  $('connect-route').onclick=run(async()=>{const config={...state.soundboard.config};for(const key of ['inputId','voiceOutputId','earsOutputId'])config[key]=$(key).value;await action('routing',{config});routeDirty=false;await refresh();notify('Audio connected. Choose the receiving microphone shown here in your game or call.',false,'routing');});
  for(const key of ['inputId','voiceOutputId','earsOutputId'])$(key).onchange=()=>{routeDirty=true;renderRouting(state);scheduleFormats();};
  all('[data-test]').forEach(b=>b.onclick=run(async()=>{await action('test',{bus:b.dataset.test});notify('Test tone sent to '+(b.dataset.test==='voice'?'the mixer output. Check your game or call’s mic test.':'your headphones or speakers.'),false,'routing');}));$('stop-audio').onclick=run(()=>action('stop'));
  $('revoke-all').onclick=run(async()=>{await action('revoke_all');await refresh();notify('All phones revoked. Scan the QR again to reconnect.');});
  $('allow-firewall').onclick=run(async()=>{const result=await action('firewall');await refresh();notify(result.ok?'Wi-Fi firewall rule added.':'The firewall rule was not added.',!result.ok);});
  $('refresh-code').onclick=run(async()=>{await request('/admin/api/pair/refresh',{});await refresh();});
  all('[data-mode]').forEach(b=>b.onclick=run(async()=>{await request('/admin/api/mode',{mode:b.dataset.mode});await refresh();}));
  $('autostart').onchange=run(async()=>{await request('/admin/api/autostart',{enabled:$('autostart').checked});await refresh();});
  $('secure').onchange=run(async()=>{await request('/admin/api/secure',{enabled:$('secure').checked});await refresh();notify('Connection setting saved. The phone connection is updating.');});
  function renderConnectionStatus(admin){
    const connection=admin.connection||{},requested=connection.requested||{mode:admin.mode,secure:admin.secure},active=connection.active||requested;
    const label=link=>!link.mode?'unavailable':(link.mode==='loopback'?'USB':'Wi-Fi')+(link.secure?' · secure':' · HTTP');
    const mismatch=active.mode!==requested.mode||active.secure!==requested.secure;
    const blocked=!!connection.error||!!connection.transitioning||mismatch||connection.ready===false;
    let message='';
    if(connection.error)message='Phone connection change failed: '+connection.error+'. '+(active.mode?'Still using '+label(active)+'.':'Phone connection unavailable.')+' Your requested choice is saved.';
    else if(connection.transitioning||mismatch)message='Updating phone connection… Current: '+label(active)+'. Requested: '+label(requested)+'.';
    else if(connection.ready===false)message='The phone connection is starting. Pairing will be available when it is ready.';
    for(const [id,host]of [['connection-status',$('connect-note').parentElement],['secure-status',$('secure').closest('.setting').querySelector('div')]]){let status=$(id);if(!status){status=node('p','connection-status');status.id=id;status.setAttribute('role','status');host.append(status);}status.hidden=!blocked;status.textContent=message;status.classList.toggle('error',!!connection.error);}
    $('pair-qr').hidden=blocked;$('pair-code').hidden=blocked;$('connect-url').hidden=blocked;$('refresh-code').disabled=blocked;
    if(blocked)$('connect-note').textContent='Pairing details will return when the requested phone connection is active.';
    if(connection.error)$('connection-label').textContent='PC connected · phone connection change failed';
  }
  function renderRouting(next){
    const sound=next.soundboard,status=next.routingStatus||{mode:'unknown',ready:sound.runtime==='ready'},ready=status.ready&&!routeDirty;
    $('route-mode').textContent=routeDirty?'Unsaved route preview':status.mode==='mixed'?'Windows input: virtual mixer':status.mode==='bypass'?'Windows input: direct mic bypass':'Windows input: not confirmed';
    $('route-mode').classList.toggle('warn',status.mode!=='mixed'||!ready);
    $('default-input').textContent='Windows default: '+(status.defaultInput||'Not reported');
    $('use-mixer-input').disabled=!ready||!status.receiverId||status.mode==='mixed';
    $('connect-route').textContent=routeDirty?'Apply device changes':ready?'Audio running':'Start audio';
    $('connect-route').disabled=ready&&!routeDirty;
    $('recommended-route').disabled=next.cableAvailable===false;
    $('discord-input').textContent=next.receivingMic;
    $('verify-receiver').disabled=!ready||deliveryChecking;
    const deliveryRoute=JSON.stringify([sound.config,sound.runtime,routeDirty]);
    if(deliverySignature&&deliverySignature!==deliveryRoute){deliverySignature='';$('delivery-results').textContent='The audio route changed. Check sound delivery again.';}
    all('[data-test]').forEach(b=>b.disabled=!ready||(b.dataset.test==='ears'&&!sound.config.earsOutputId));
    $('bypass-path').classList.toggle('active',status.mode==='bypass');
    $('bypass-detail').textContent=status.mode==='bypass'?'Voice goes directly to apps. Deckster sounds are left out.':status.mode==='mixed'?'Windows uses the mixer. This direct path is off.':'Check the microphone selected in your app.';
    if(routeDirty)$('audio-detail').textContent='Unsaved route preview. Apply this route before testing or changing the Windows microphone.';
    drawWires();renderSignals(next,ready);
  }
  $('use-mixer-input').onclick=run(async()=>{await action('use_mixer_input');await refresh();notify('Windows now uses the mixer microphone. Check apps that select their own input.',false,'routing');});
  $('verify-receiver').onclick=run(async()=>{
    deliveryChecking=true;$('verify-receiver').disabled=true;
    const signature=JSON.stringify([state.soundboard.config,state.soundboard.runtime,routeDirty]);
    $('delivery-results').textContent='Checking the cable’s receiving microphone…';
    try{const result=await action('verify_receiver');
      if(signature!==JSON.stringify([state.soundboard.config,state.soundboard.runtime,routeDirty])){$('delivery-results').textContent='The audio route changed. Check sound delivery again.';return;}
      deliverySignature=signature;
      $('delivery-results').textContent=result.passed?'Test sound confirmed at '+result.receiver+'. If Discord is still silent, use Studio or disable its voice filters, then test an actual clip in Discord.':result.error||'The test sound was not reliably received. Check the cable’s recording volume and mute in Windows Sound settings, restart audio, and try again.';
    }catch(e){$('delivery-results').textContent='Sound delivery check failed: '+e.message;}
    finally{deliveryChecking=false;renderRouting(state);}
  });
  function selectedRoute(){return Object.fromEntries(['inputId','voiceOutputId','earsOutputId'].map(key=>[key,$(key).value]));}
  function scheduleFormats(){
    if(document.hidden){formatsPending=true;return;}
    const signature=JSON.stringify([selectedRoute(),state.soundboard.inputs,state.soundboard.outputs]);
    if(signature===formatSignature)return;formatSignature=signature;formatGeneration++;clearTimeout(formatTimer);
    $('format-results').textContent='Checking selected devices automatically…';$('format-summary').textContent='Checking automatically…';formatTimer=setTimeout(checkFormats,400);
  }
  async function checkFormats(){
    if(document.hidden){formatsPending=true;return;}
    if(formatChecking){clearTimeout(formatTimer);formatTimer=setTimeout(checkFormats,400);return;}
    const config=selectedRoute();if(!config.inputId&&!config.voiceOutputId&&!config.earsOutputId){$('format-results').textContent='Choose Use recommended settings after connecting your microphone and installing VB-CABLE.';$('format-summary').textContent='Choose audio devices to check';return;}
    const generation=formatGeneration;formatChecking=true;$('check-formats').disabled=true;
    try{const result=await request('/admin/api/workspace',{action:'check_formats',config});if(generation!==formatGeneration)return;
      const host=$('format-results');host.replaceChildren();for(const d of result.endpoints){host.append(node('p',d.supported?'format-ok':'format-warn',d.label+' · '+d.name+' — '+(d.supported?'48 kHz supported'+(d.defaultRate?' (device default '+(d.defaultRate/1000)+' kHz)':''):'Needs attention: '+d.error)));}
      const compatible=result.endpoints.every(d=>d.supported);host.prepend(node('p',compatible?'format-ok':'format-warn',compatible?'Selected devices are compatible. No sample-rate changes are needed.':'Some devices need attention.'));
      $('format-summary').textContent=compatible?'48 kHz · devices compatible':'Check device settings';$('format-summary').style.color=compatible?'var(--green)':'#ffc18d';$('format-details').open=!compatible;
      if(!compatible)host.append(node('p','',result.recommendation));
    }catch(e){if(generation===formatGeneration){$('format-results').textContent='Could not check formats: '+e.message;$('format-summary').textContent='Check unavailable';$('format-details').open=true;}}finally{formatChecking=false;$('check-formats').disabled=false;}
  }
  $('check-formats').onclick=()=>{formatGeneration++;clearTimeout(formatTimer);checkFormats();};
  $('recommended-route').onclick=run(async()=>{await action('recommended_routing');routeDirty=false;routeSignature='';formatSignature='';await refresh();notify('Recommended devices connected. Compatibility is checked automatically. Use the microphone-routing button to send the mix to apps.',false,'routing');});
  function svgNode(tag,attrs){const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [key,value]of Object.entries(attrs))n.setAttribute(key,value);return n;}
  function drawWires(){
    if(document.hidden||view!=='routing'||!state)return;const graph=$('signal-graph'),svg=$('routing-wires'),g=graph.getBoundingClientRect();
    const rect=id=>{const r=$(id==='bypass'?'bypass-path':'node-'+id).getBoundingClientRect();return {x:r.left-g.left,y:r.top-g.top,w:r.width,h:r.height,right:r.right-g.left,bottom:r.bottom-g.top};};
    const mic=rect('mic'),sounds=rect('sounds'),mixer=rect('mixer'),apps=rect('apps'),ears=rect('ears'),bypass=rect('bypass'),narrow=innerWidth<=720,compact=innerWidth<1150;
    const signature=JSON.stringify([g.width,g.height,mic,sounds,mixer,apps,ears,bypass]);if(signature===wireGeometry)return;wireGeometry=signature;
    svg.querySelectorAll('g').forEach(n=>n.remove());svg.setAttribute('viewBox','0 0 '+g.width+' '+g.height);
    function path(points){
      points=points.filter((p,i)=>!i||p[0]!==points[i-1][0]||p[1]!==points[i-1][1]);let d='M'+points[0].join(',');
      for(let i=1;i<points.length-1;i++){const a=points[i-1],p=points[i],b=points[i+1],da=Math.hypot(p[0]-a[0],p[1]-a[1]),db=Math.hypot(b[0]-p[0],b[1]-p[1]),r=Math.min(6,da/2,db/2);d+=' L'+[p[0]+(a[0]-p[0])*r/da,p[1]+(a[1]-p[1])*r/da].join(',')+' Q'+p.join(',')+' '+[p[0]+(b[0]-p[0])*r/db,p[1]+(b[1]-p[1])*r/db].join(',');}
      return d+' L'+points.at(-1).join(',');
    }
    function wire(id,points,label,position){const group=svgNode('g',{'data-wire':id});group.append(svgNode('path',{d:path(points),class:'audio-wire disconnected'}),svgNode('path',{d:path(points),class:'wire-pulse',hidden:''}));if(label){const text=svgNode('text',{x:position[0],y:position[1],class:'wire-label'});text.textContent=label;group.append(text);}svg.append(group);}
    const sourceY=mixer.y-15;
    const micPoints=narrow?[[mic.x,mic.y+mic.h*.5],[12,mic.y+mic.h*.5],[12,mixer.y+mixer.h*.28],[mixer.x,mixer.y+mixer.h*.28]]:compact?[[mic.x+mic.w*.5,mic.bottom],[mic.x+mic.w*.5,sourceY],[mixer.x+mixer.w*.25,sourceY],[mixer.x+mixer.w*.25,mixer.y]]:[[mic.x+mic.w*.7,mic.bottom],[mic.x+mic.w*.7,sourceY],[mixer.x-16,sourceY],[mixer.x-16,mixer.y+mixer.h*.25],[mixer.x,mixer.y+mixer.h*.25]];
    const soundPoints=narrow?[[sounds.x+sounds.w*.5,sounds.bottom],[sounds.x+sounds.w*.5,mixer.y]]:compact?[[sounds.x+sounds.w*.5,sounds.bottom],[sounds.x+sounds.w*.5,sourceY],[mixer.x+mixer.w*.75,sourceY],[mixer.x+mixer.w*.75,mixer.y]]:[[sounds.x+sounds.w*.3,sounds.bottom],[sounds.x+sounds.w*.3,sourceY],[mixer.right+16,sourceY],[mixer.right+16,mixer.y+mixer.h*.25],[mixer.right,mixer.y+mixer.h*.25]];
    wire('mic',micPoints,'Voice',narrow?[mic.x+45,mic.bottom+20]:[mic.x+mic.w*(compact?.5:.7),sourceY-5]);
    wire('sounds',soundPoints,'Others',[sounds.x+sounds.w*(narrow||compact?.5:.3),narrow?mixer.y-12:sourceY-5]);
    const appX=mixer.x+mixer.w*(compact&&!narrow?.25:.5),earX=mixer.x+mixer.w*.75,appY=apps.y-15;
    wire('voice',[[appX,mixer.bottom],[appX,appY],[apps.x+apps.w*.5,appY],[apps.x+apps.w*.5,apps.y]],'Mixed input',[apps.x+apps.w*.5+38,appY+3]);
    const earPoints=narrow?[[mixer.right,mixer.y+mixer.h*.65],[g.width-12,mixer.y+mixer.h*.65],[g.width-12,ears.y+ears.h*.5],[ears.right,ears.y+ears.h*.5]]:compact?[[earX,mixer.bottom],[earX,ears.y-15],[ears.x+ears.w*.5,ears.y-15],[ears.x+ears.w*.5,ears.y]]:[[mixer.right,mixer.y+mixer.h*.75],[ears.x,ears.y+ears.h*.75]];
    wire('ears',earPoints,'Me',narrow?[ears.x+ears.w*.5,ears.y-12]:compact?[ears.x+ears.w*.5+22,ears.y-12]:[(mixer.right+ears.x)/2,mixer.y+mixer.h*.75-7]);
    const directPoints=compact?[[mic.x,mic.y+mic.h*.75],[4,mic.y+mic.h*.75],[4,bypass.y+bypass.h*.25],[bypass.x,bypass.y+bypass.h*.25]]:[[mic.x+mic.w*.3,mic.bottom],[mic.x+mic.w*.3,bypass.y]];
    wire('direct-source',directPoints,compact?'':'Direct',[mic.x+mic.w*.3,sourceY-5]);
    const bypassPoints=compact?[[bypass.x,bypass.y+bypass.h*.75],[narrow?20:12,bypass.y+bypass.h*.75],[narrow?20:12,apps.y+apps.h*.7],[apps.x,apps.y+apps.h*.7]]:[[bypass.x+bypass.w*.5,bypass.bottom],[bypass.x+bypass.w*.5,appY],[apps.x-16,appY],[apps.x-16,apps.y+apps.h*.5],[apps.x,apps.y+apps.h*.5]];
    wire('bypass',bypassPoints,'',[0,0]);
  }
  function renderSignals(next,ready){
    const bypassLevel=next.routingStatus?.mode==='bypass'?Number(next.snapshot.devices?.meters?.input||0):0;
    $('meter-bypass').value=bypassLevel;$('level-bypass').textContent=next.routingStatus?.mode==='bypass'?(bypassLevel>.008?'Live · '+Math.round(bypassLevel*100)+'%':'No signal'):'Bypass off';$('bypass-path').classList.toggle('flowing',bypassLevel>.008);
    for(const bus of ['mic','sounds','voice','ears']){const level=ready?Number(next.soundboard.levels?.[bus]||0):0;$('meter-'+bus).value=level;$('level-'+bus).textContent=!ready?'Audio off':level>.008?Math.round(level*100)+'%':'No signal';document.querySelector('[data-signal="'+bus+'"]').classList.toggle('flowing',level>.008);}
    updateWireSignals(next,ready);
  }
  function updateWireSignals(next,ready){for(const id of ['mic','sounds','voice','ears','bypass','direct-source']){const group=document.querySelector('[data-wire="'+id+'"]');if(!group)continue;const bus=id==='direct-source'?'bypass':id,connected=bus==='bypass'?next.routingStatus?.mode==='bypass':ready&&(bus!=='ears'||!!next.soundboard.config.earsOutputId),level=bus==='bypass'?Number(next.snapshot.devices?.meters?.input||0):Number(next.soundboard.levels?.[bus]||0),paths=group.querySelectorAll('path');paths[0].setAttribute('class','audio-wire'+(connected?'':' disconnected'));group.style.setProperty('--signal-opacity',.55+Math.sqrt(Math.min(1,level))*.45);paths[1].toggleAttribute('hidden',!connected||level<=.008);}}
  async function pollSignals(){
    if(signalPolling||view!=='routing'||!state||document.hidden||performance.now()<signalRetryAt)return;
    signalPolling=true;const epoch=signalEpoch;
    try{const result=await request('/admin/api/signals');if(epoch!==signalEpoch||view!=='routing'||document.hidden)return;Object.assign(state.soundboard,result.soundboard);state.snapshot.devices.meters.input=result.inputLevel;renderSignals(state,state.routingStatus?.ready&&result.soundboard.runtime==='ready'&&!routeDirty);}
    catch(e){signalRetryAt=performance.now()+1500;if(epoch===signalEpoch&&view==='routing')renderSignals(state,false);}
    finally{signalPolling=false;}
  }
  new ResizeObserver(()=>{drawWires();if(state&&!document.hidden)updateWireSignals(state,state.routingStatus?.ready&&!routeDirty);}).observe($('signal-graph'));
  new ResizeObserver(resizePreview).observe($('preview-viewport'));
  window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
  function syncPreviewVisibility(){if(previewReady)$('phone').contentWindow.postMessage({t:'preview-visibility',visible:!document.hidden&&['layout','mixer','soundboard'].includes(view)},location.origin);}
  function updateVisibility(){
    clearInterval(refreshTimer);refreshTimer=null;clearInterval(signalTimer);signalTimer=null;signalEpoch++;
    syncPreviewVisibility();
    if(document.hidden){cancelGesture();clearTimeout(formatTimer);formatGeneration++;formatsPending=true;return;}
    const cached=latestWorkspace;latestWorkspace=null;
    refresh(cached).then(()=>{if(formatsPending){formatsPending=false;checkFormats();}sendPreview();});
    pollSignals();refreshTimer=setInterval(()=>refresh(),1200);signalTimer=setInterval(pollSignals,70);
  }
  document.addEventListener('visibilitychange',updateVisibility);
  refresh().then(()=>chooseView('layout'));refreshTimer=setInterval(()=>refresh(),1200);signalTimer=setInterval(pollSignals,70);
})();
