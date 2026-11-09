/* Explicit single-call decisions; forms needing input retain the JSON fallback. */
function confirmationResponse(approval, allow) {
  const params = approval.params || {};
  if (['item/commandExecution/requestApproval', 'item/fileChange/requestApproval'].includes(approval.method)) {
    return {decision: allow ? 'accept' : 'decline'};
  }
  if (approval.method === 'claude/can_use_tool') {
    return allow ? {behavior:'allow', updatedInput:params.input || {}} : {behavior:'deny', message:'Denied from Arbiter'};
  }
  if (approval.method === 'mcpServer/elicitation/request' && ['form','openai/form'].includes(params.mode)) {
    const schema = params.requestedSchema || {};
    if (!allow) return {action:'decline', content:null};
    if (schema.type === 'object' && schema.properties && Object.keys(schema.properties).length === 0 && !(schema.required || []).length) {
      return {action:'accept', content:{}};
    }
  }
  return null;
}
globalThis.ArbiterApprovals = {response:confirmationResponse};

let state = null, events = [], source, refreshing = false;
const $ = id => document.getElementById(id);
const terminal = s => ['finished', 'failed'].includes(s.state);
const node = (tag, cls, value) => { const e = document.createElement(tag); if (cls) e.className = cls; if (value !== undefined) e.textContent = value; return e; };
async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const value = await r.json(); if (!r.ok) throw Error(value.error || r.statusText); return value;
}
async function action(operation, data) { try { const result = await api('/api/action', {action:operation,data}); await refresh(); return result; } catch(e) { error(e.message); } }
function error(message) { $('error').textContent = message; $('error').style.display = 'block'; setTimeout(() => $('error').style.display = 'none', 7000); }
function button(text, fn) { const b = node('button', '', text); b.type='button'; b.onclick=fn; return b; }
function name(id) { return state.sessions[id]?.name || id || 'Arbiter'; }
async function refresh() { if(refreshing)return; refreshing=true; try { state = await api('/api/state'); render(); } catch(e) { error(e.message); } finally { refreshing=false; } }
function render() {
  const all = Object.values(state.sessions), live = all.filter(s => !terminal(s));
  $('count').textContent = live.length; $('sessions').replaceChildren();
  for (const s of all.filter(s => $('show-ended').checked || !terminal(s)).reverse()) {
    const card=node('article','session'), top=node('div','session-top');
    top.append(node('h3','',s.name),node('span','badge '+s.state,s.state)); card.append(top,node('div','meta',`${s.kind} / ${s.id}`));
    card.append(node('p','status',s.status));
    for(const r of Object.values(state.reservations).filter(r=>r.session===s.id && ['granted','requested','suspended'].includes(r.state))) {
      card.append(node('div','resource',`${r.cpu} CPU · ${r.memory_mb} MiB${r.exclusive.length?' · '+r.exclusive.join(', '):''}`));
      card.append(node('div','small muted',r.state==='requested'?r.reason:r.state));
    }
    if(!terminal(s)) {
      const actions=node('div','actions');
      const verb=['paused','draining'].includes(s.state)?'resume':'pause';
      actions.append(button(verb==='pause'?'Request pause':'Resume',()=>action('control',{session:s.id,action:verb,reason:'Requested from message board'})),button('Ask for update',()=>action('control',{session:s.id,action:'status',reason:'Short update requested from message board'})));
      card.append(actions);
    }
    $('sessions').append(card);
  }
  if(!$('sessions').children.length)$('sessions').append(node('div','empty','No active sessions. Launch an agent, register a session, or run arbiter demo.'));
  const held=Object.values(state.reservations).filter(r=>r.state==='granted'); $('capacity').replaceChildren();
  for(const [label,key,unit] of [['CPU scheduling budget','cpu','CPU'],['Job memory budget','memory_mb','MiB']]) {
    const used=held.reduce((sum,r)=>sum+r[key],0), max=state.capacity[key], metric=node('div','metric'), line=node('div');
    line.append(node('span','',label),node('strong','',`${Math.round(used*10)/10} / ${Math.round(max*10)/10} ${unit}`));
    const progress=node('progress');progress.max=max;progress.value=used;metric.append(line,progress);$('capacity').append(metric);
  }
  if(state.telemetry?.available_mb!==undefined)$('capacity').append(node('div','small muted',`${state.telemetry.available_mb.toLocaleString()} MiB available on host · load ${state.telemetry.load?.[0]?.toFixed(2)}`));
  for(const id of ['sender','recipient']) {
    const selected=$(id).value; $(id).replaceChildren();
    if(id==='recipient')$(id).append(new Option('Everyone · shared board',''));
    for(const s of live)$(id).append(new Option(s.name,s.id));
    if([...$(id).options].some(o=>o.value===selected))$(id).value=selected;
  }
  $('approvals').replaceChildren();
  for(const a of Object.values(state.approvals).filter(a=>a.state==='pending' && !terminal(state.sessions[a.session]))) {
    const card=node('article');card.append(node('h2','',`Input needed · ${name(a.session)}`),node('p','',a.params.message || a.method),node('pre','',JSON.stringify(a.params,null,2)));
    const accept = ArbiterApprovals.response(a,true), decline = ArbiterApprovals.response(a,false);
    if(accept)card.append(button('Allow this call',()=>action('approval_answer',{session:a.session,approval:a.id,response:accept})));
    if(decline)card.append(button('Decline',()=>action('approval_answer',{session:a.session,approval:a.id,response:decline})));
    if(!accept)card.append(node('p','small muted',`Respond with: arbiter answer ${a.id} '<provider response JSON>'`));
    $('approvals').append(card);
  }
  renderEvents();
}
function renderEvents(){
  if(!state)return; const filter=$('filter').value; $('events').replaceChildren();
  for(const e of [...events].reverse().filter(e=>filter==='all'||e.kind.startsWith(filter+'.')).slice(0,250)) {
    const row=node('article','event'), body=node('div'), d=e.data;
    row.append(node('time','time',new Date(e.at*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',hour12:false})),body);
    body.append(node('div','event-title',`${name(e.session)} · ${e.kind.replaceAll('.',' / ')}`));
    const message=d.text || d.reason || d.status || (d.action?`${d.action} request · ${d.state||''}`:'') || (d.reservation?`${d.reservation}${d.cpu!==undefined?` · ${d.cpu} CPU · ${d.memory_mb} MiB`:''}`:'');
    if(message)body.append(node('div','event-text',message));
    if(e.kind==='message.sent')body.append(node('div','event-detail',`→ ${d.to?name(d.to):'everyone'} · #${d.topic} · ${d.id}${d.reply_to?' · reply to '+d.reply_to:''}`));
    if(d.stopping_point)body.append(node('div','small muted','Next stopping point: '+d.stopping_point));
    $('events').append(row);
  }
  if(!$('events').children.length)$('events').append(node('div','empty','Activity appears here as sessions work.'));
}
$('show-ended').onchange=()=>state&&render();$('filter').onchange=renderEvents;
$('compose').onsubmit=async e=>{e.preventDefault();const form=new FormData(e.target);if(!$('sender').value)return error('Register or launch a session first.');const result=await action('message',{session:$('sender').value,to:$('recipient').value||null,topic:form.get('topic'),text:form.get('text')});if(result)e.target.elements.text.value='';};
async function start(){
  await refresh();let cursor=0;
  while(true){const batch=await api('/api/events?after='+cursor);events.push(...batch);if(batch.length)cursor=batch.at(-1).id;if(batch.length<200)break;}
  events=events.slice(-1000);renderEvents();source=new EventSource('/events?after='+cursor);
  source.onopen=()=>{$('connection').textContent='● Connected';};source.onerror=()=>{$('connection').textContent='○ Reconnecting…';};
  source.onmessage=e=>{const event=JSON.parse(e.data);if(!events.some(x=>x.id===event.id))events.push(event);events=events.slice(-1000);refresh();};
}
start().catch(e=>error(e.message));setInterval(refresh,5000);
