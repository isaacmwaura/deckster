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
  function notify(message, error=false) { $('notice').hidden=false;$('notice').classList.toggle('error',error);$('notice').textContent=message; }
  async function request(url, body) { const r=await fetch(url, body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {cache:'no-store'});const data=await r.json();if(!r.ok)throw new Error(data.error||data.msg||`Request failed (${r.status})`);return data; }
  async function action(action, args={}) { try{return await request('/admin/api/workspace',{action,...args});}catch(e){notify(e.message,true);throw e;} }
  function run(fn) { return (...args)=>Promise.resolve().then(()=>fn(...args)).catch(()=>{}); }
  function currentPages() { return {mixer:'center',...draft.pages}; }
  function changed() {dirty=true;$('notice').hidden=true;render();}
  function render() {
    if(!draft)return;
    $('save').disabled=!dirty||conflict; $('draft-status').textContent=conflict?'Phone layout changed elsewhere. Discard your draft to reload.':dirty?'Unsaved changes · Save to update your phone':'All changes saved';
    document.querySelector('footer').classList.toggle('unsaved',dirty);$('discard').disabled=!dirty;
    all('[data-hide]').forEach(b=>{const active=b.dataset.hide===(draft.hideInteraction||'drag');b.classList.toggle('active',active);b.setAttribute('aria-pressed',active);});
    renderMap();renderLibrary();renderApps();sendPreview();
  }
  function sendPreview() {
    if(!previewReady||!state||!draft||gesture)return;
    const snapshot=clone(state.snapshot);snapshot.presentation=clone(draft);snapshot.soundboard=clone(state.soundboard);
    $('phone').contentWindow.postMessage({t:'preview-state',snapshot,page},location.origin);
  }
  function resizePreview() {
    const width=portrait?432:960,height=portrait?960:432;
    const viewport=$('preview-viewport');viewport.style.aspectRatio=`${width}/${height}`;
    viewport.style.maxWidth=portrait?'240px':'800px';viewport.style.margin='auto';
    const frame=$('phone');frame.style.width=width+'px';frame.style.height=height+'px';frame.style.transform=`scale(${viewport.clientWidth/width})`;
  }
  function choosePage(name) {page=name;all('[data-page]').forEach(b=>{b.classList.toggle('active',b.dataset.page===name);b.setAttribute('aria-pressed',b.dataset.page===name);});sendPreview();}
  function chooseView(name) {
    view=name;all('[data-view]').forEach(b=>{b.classList.toggle('active',b.dataset.view===name);if(b.dataset.view===name)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});
    $('title').textContent=titles[name][0];$('subtitle').textContent=titles[name][1];
    const editor=['layout','mixer','soundboard'].includes(name);$('editor').hidden=!editor;$('save').hidden=!editor;$('discard').hidden=!editor;
    $('editor').dataset.view=name;
    for(const key of ['routing','connect','settings'])$(key+'-view').hidden=name!==key;
    $('layout-tools').hidden=name!=='layout';$('app-tools').hidden=name!=='mixer';$('hide-tools').hidden=name!=='mixer';$('sound-tray').hidden=name!=='soundboard';
    if(name==='soundboard')choosePage('soundboard');if(name==='mixer')choosePage('mixer');
    resizePreview();render();
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
  async function audition(id) {await action('audition',{id});notify('Playing on your PC headphones or speakers.');}
  function renderLibrary() {
    const query=$('sound-search').value.toLowerCase(),host=$('library');
    const unused=state.soundboard.clips.filter(c=>!draft.padSlots.includes(c.id));$('library-count').textContent=unused.length;
    const clips=unused.filter(c=>(c.label||'').toLowerCase().includes(query));
    const signature=JSON.stringify(clips);if(signature===librarySignature)return;librarySignature=signature;host.replaceChildren();
    for(const clip of clips){const item=node('div','sound-item');item.dataset.id=clip.id;item.tabIndex=0;item.setAttribute('aria-label',`Listen to ${clip.label}. Drag onto a pad to assign.`);
      item.append(node('span','emoji',clip.emoji||'♫'));const label=node('div','sound-name',clip.label);label.append(node('small','',`${Number(clip.duration||0).toFixed(1)}s · Unassigned`));item.append(label);
      item.onpointerdown=e=>startGesture(e,item,clip.id,false);item.onkeydown=e=>{if(e.key==='Enter')run(()=>audition(clip.id))();};host.append(item);
    }
    if(!clips.length)host.append(node('div','empty',query?'No matching sounds.':'All sounds are assigned. Hold a pad and drag it here to unassign.'));
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
    for(const id of ids){const app=live.find(s=>s.id===id),hidden=draft.hiddenApps.includes(id);const row=node('div','app-row'+(hidden?' hidden-app':''));row.draggable=true;row.dataset.id=id;row.append(node('span','grip','⠿'));if(app&&app.iconKey){const img=node('img');img.src='/icon/'+encodeURIComponent(app.iconKey);img.alt='';row.append(img);}row.append(node('span','app-name',app?(app.appLabel||app.label):id+' (offline)'));row.append(node('small','',app?Math.round((app.level||0)*100)+'%':'Offline'));const b=node('button','',hidden?'Show':'⊘ Hide');b.onclick=()=>{draft.hiddenApps=hidden?draft.hiddenApps.filter(x=>x!==id):[...draft.hiddenApps,id];changed();};row.append(b);row.ondragstart=e=>e.dataTransfer.setData('application/deckster-app',id);row.ondragover=e=>{if(e.dataTransfer.types.includes('application/deckster-app'))e.preventDefault();};row.ondrop=e=>{e.preventDefault();const source=e.dataTransfer.getData('application/deckster-app');if(!ids.includes(source)||source===id)return;const order=ids.filter(x=>x!==source);order.splice(order.indexOf(id),0,source);draft.appOrder=order;changed();};host.append(row);}
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
    if(e.data.t==='preview-ready'){previewReady=true;wirePreview();sendPreview();return;}
    if(e.data.t==='preview-page'){page=e.data.page;all('[data-page]').forEach(b=>b.classList.toggle('active',b.dataset.page===page));return;}
    if(e.data.t!=='preview-command')return;const cmd=e.data.command;
    if(cmd.t==='presentation_update'){Object.assign(draft,clone(cmd.changes));changed();}
    else if(cmd.t==='soundboard_play')run(()=>audition(cmd.id||cmd.clipId))();
    else if(cmd.t==='soundboard_stop_all')run(()=>action('stop'))();
    else if(cmd.t==='soundboard_update_clip')run(async()=>{try{await action('clip',{id:cmd.clipId,changes:cmd.changes});await refresh();}catch(e){$('phone').contentWindow.postMessage({t:'preview-error',message:e.message},location.origin);}})();
    else if(['set_volume','set_mute','set_default_output','set_default_input','media_control','app_input_mute','set_app_input_binding','clear_app_input_binding'].includes(cmd.t))run(()=>action('control',{command:cmd}))();
    else notify('This action is unavailable in the preview.',true);
  });
  async function refresh(){if(polling||gesture)return;polling=true;try{
    const next=await request('/admin/api/workspace');if(gesture)return;const redraw=!draft||(!dirty&&JSON.stringify(draft)!==JSON.stringify(next.presentation.presentation));state=next;
    if(!dirty){draft=clone(next.presentation.presentation);conflict=false;}else if(next.presentation.presentation.revision!==draft.revision){conflict=true;}
    $('connection-label').textContent='PC connected · '+(next.admin.mode==='loopback'?'USB':'Wi-Fi');$('connection-dot').classList.remove('off');$('version').textContent='DECKSTER '+next.admin.version;
    $('runtime').textContent=next.soundboard.runtime==='ready'?'● Audio connected':'Audio needs attention';$('runtime').classList.toggle('warn',next.soundboard.runtime!=='ready');
    if(!routeDirty){for(const key of ['inputId','voiceOutputId','earsOutputId']){const select=$(key),devices=key==='inputId'?next.soundboard.inputs:next.soundboard.outputs;select.replaceChildren();const blank=node('option','',key==='earsOutputId'?'Off · no local monitoring':'Choose a device');blank.value='';select.append(blank);for(const d of devices||[]){const opt=node('option','',d.name);opt.value=d.id;select.append(opt);}select.value=next.soundboard.config[key]||'';}}
    $('receiving-mic').textContent=next.receivingMic;$('audio-detail').textContent=next.soundboard.error||`Audio status: ${next.soundboard.runtime}. Test tones check the selected output. A real game or call test is still needed to check what other people receive.`;
    $('firewall-controls').hidden=!next.admin.firewallNeeded;$('pair-code').textContent=next.admin.pairCode;$('pair-qr').src='/admin/qr?code='+encodeURIComponent(next.admin.pairCode);$('connect-url').textContent=next.admin.connectUrl;$('connect-note').textContent=next.admin.connectNote;
    all('[data-mode]').forEach(b=>b.classList.toggle('active',b.dataset.mode===next.admin.mode));$('autostart').checked=next.admin.autostart;$('secure').checked=next.admin.secure;
    const devices=$('device-list');devices.replaceChildren();for(const d of next.admin.devices){const row=node('div','device',d.name||d.id);const b=node('button','','Revoke');b.onclick=run(async()=>{await request('/admin/api/device/revoke',{id:d.id});await refresh();});row.append(b);devices.append(row);}if(!next.admin.devices.length)devices.append(node('p','','No phones paired yet.'));
    if(redraw)render();else{renderLibrary();renderApps();sendPreview();$('save').disabled=!dirty||conflict;if(conflict)$('draft-status').textContent='Phone layout changed elsewhere. Discard your draft to reload.';}
  }catch(e){$('connection-dot').classList.add('off');$('connection-label').textContent='PC connection lost';notify(e.message,true);}finally{polling=false;}}
  $('save').onclick=run(async()=>{if(!Object.values(currentPages()).includes('center')){notify('The center is empty. Move a page into the center before saving.',true);return;}const changes={appOrder:draft.appOrder,hiddenApps:draft.hiddenApps,pages:draft.pages,padSlots:draft.padSlots,hideInteraction:draft.hideInteraction||'drag'};const saved=await action('presentation',{changes,baseRevision:draft.revision});draft=saved;dirty=false;conflict=false;render();notify('Saved to your phone.');await refresh();});
  $('discard').onclick=()=>{dirty=false;conflict=false;draft=clone(state.presentation.presentation);render();notify('Draft discarded. The saved phone layout is restored.');};
  all('[data-view]').forEach(b=>b.onclick=()=>chooseView(b.dataset.view));all('[data-page]').forEach(b=>b.onclick=()=>choosePage(b.dataset.page));all('[data-hide]').forEach(b=>b.onclick=()=>{draft.hideInteraction=b.dataset.hide;changed();});
  $('sound-search').oninput=renderLibrary;
  $('orientation').onclick=()=>{portrait=!portrait;$('orientation').textContent=portrait?'Portrait ⇄':'Landscape ⇄';resizePreview();};
  $('import-sound').onclick=()=> $('sound-files').click();
  $('sound-files').onchange=run(async()=>{for(const file of $('sound-files').files){const body=new FormData();body.append('file',file);const response=await fetch('/admin/api/import',{method:'POST',body});const result=await response.json();if(!response.ok){notify(result.error,true);throw new Error(result.error);}}$('sound-files').value='';await refresh();render();notify('Sounds added to your library.');});
  $('restore-layout').onclick=run(async()=>{const saved=await action('defaults',{baseRevision:draft.revision});draft=saved;dirty=false;conflict=false;await refresh();render();notify('Phone defaults restored. Your library, routes and pairing are preserved.');});
  $('restore-sounds').onclick=run(async()=>{await action('starter_sounds');await refresh();render();notify('Starter sounds restored. Imported sounds are preserved.');});
  $('manage-library').onclick=()=>{const host=$('clip-list');host.replaceChildren();for(const clip of state.soundboard.clips){const button=node('button','secondary',`${clip.emoji||'♫'} ${clip.label}`);button.onclick=()=>openClip(clip.id);host.append(button);}$('library-dialog').showModal();};
  $('close-library').onclick=()=> $('library-dialog').close();
  function openClip(id){const clip=state.soundboard.clips.find(c=>c.id===id);if(!clip)return;$('clip-dialog').dataset.id=id;for(const key of ['label','emoji','gain'])$('clip-'+key).value=clip[key];for(const key of ['voice','ears'])$('clip-'+key).checked=clip[key];if(!$('clip-dialog').open)$('clip-dialog').showModal();}
  $('close-clip').onclick=()=> $('clip-dialog').close();
  $('save-clip').onclick=run(async()=>{const changes={label:$('clip-label').value,emoji:$('clip-emoji').value,gain:Number($('clip-gain').value),voice:$('clip-voice').checked,ears:$('clip-ears').checked};await action('clip',{id:$('clip-dialog').dataset.id,changes});$('clip-dialog').close();await refresh();render();notify('Sound settings saved.');});
  $('delete-clip').onclick=run(async()=>{if(dirty){$('clip-dialog').close();$('library-dialog').close();notify('Save or discard your layout draft before deleting a library sound.',true);return;}await action('remove_clip',{id:$('clip-dialog').dataset.id});$('clip-dialog').close();$('library-dialog').close();await refresh();render();notify('Sound removed from the library.');});
  $('connect-route').onclick=run(async()=>{const config={...state.soundboard.config};for(const key of ['inputId','voiceOutputId','earsOutputId'])config[key]=$(key).value;await action('routing',{config});routeDirty=false;await refresh();notify('Audio connected. Choose the receiving microphone shown here in your game or call.');});
  for(const key of ['inputId','voiceOutputId','earsOutputId'])$(key).onchange=()=>{routeDirty=true;};
  all('[data-test]').forEach(b=>b.onclick=run(async()=>{await action('test',{bus:b.dataset.test});notify('Test tone sent to '+(b.dataset.test==='voice'?'others.':'your headphones or speakers.'));}));$('stop-audio').onclick=run(()=>action('stop'));
  $('revoke-all').onclick=run(async()=>{await action('revoke_all');await refresh();notify('All phones revoked. Scan the QR again to reconnect.');});
  $('allow-firewall').onclick=run(async()=>{const result=await action('firewall');await refresh();notify(result.ok?'Wi-Fi firewall rule added.':'The firewall rule was not added.',!result.ok);});
  $('refresh-code').onclick=run(async()=>{await request('/admin/api/pair/refresh',{});await refresh();});
  all('[data-mode]').forEach(b=>b.onclick=run(async()=>{await request('/admin/api/mode',{mode:b.dataset.mode});await refresh();}));
  $('autostart').onchange=run(async()=>{await request('/admin/api/autostart',{enabled:$('autostart').checked});await refresh();});
  $('secure').onchange=run(async()=>{await request('/admin/api/secure',{enabled:$('secure').checked});notify('Connection setting saved. Reopen Deckster from the tray to use the updated connection.');});
  new ResizeObserver(resizePreview).observe($('preview-viewport'));
  window.addEventListener('beforeunload',e=>{if(dirty){e.preventDefault();e.returnValue='';}});
  refresh().then(()=>chooseView('layout'));setInterval(refresh,1200);
})();
