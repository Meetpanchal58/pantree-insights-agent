/* workspace: explicit ES module dependencies; no build step. */
import { $, S, esc } from './state.js';
import { showSignin } from './session.js';
import { api } from './api.js';
import { toast } from './render.js';
import { closeDialog, dialog } from './dialogs.js';

export const W = { metadata: null, previews: new Map(), sheet: 'Overview', epoch: 0, focus: null, ai: null, models: [], loading: null, active: null };

export const money = n => n == null ? 'Unknown' : `$${Number(n).toFixed(4)}`;

export async function openDataset() {
  if (!S.me?.user) return showSignin();
  W.focus = document.activeElement; const epoch = ++W.epoch;
  $('#app').hidden = true; $('#dataset-explorer').hidden = false;
  $('#dataset-content').innerHTML = '<p class="muted">Opening the governed dataset…</p>';
  $('#dataset-explorer [data-action="back-chat"]').focus();
  try {
    const metadata = await api('/api/dataset'); if (epoch !== W.epoch) return;
    if (W.metadata?.version !== metadata.version) W.previews.clear();
    W.metadata = metadata; W.sheet = 'Overview';
    $('#dataset-snapshot').textContent = `${metadata.snapshot.size} snapshot · ${metadata.snapshot.first_day} – ${metadata.snapshot.last_day}`;
    $('#sheet-search').value = ''; renderSheets(); renderOverview();
  } catch (e) { if (epoch === W.epoch) $('#dataset-content').textContent = e.message; }
}

export function backToChat() { ++W.epoch; $('#dataset-explorer').hidden = true; $('#app').hidden = false; W.focus?.focus(); }

export function renderSheets() {
  const query = $('#sheet-search').value.toLowerCase();
  $('#dataset-tabs').innerHTML = `<button class="sheet-tab ${W.sheet === 'Overview' ? 'selected' : ''}" data-sheet="Overview">◫ Overview</button>` +
    W.metadata.tables.filter(t => `${t.name} ${t.description}`.toLowerCase().includes(query)).map(t =>
      `<button class="sheet-tab ${W.sheet === t.name ? 'selected' : ''}" data-sheet="${esc(t.name)}">▦ ${esc(t.name)}</button>`).join('');
  $('#dataset-tabs').querySelectorAll('[data-sheet]').forEach(b => b.onclick = () => selectSheet(b.dataset.sheet));
}

export async function tablePreview(name) {
  const identity = `${W.metadata.version}:${name}`;
  if (!W.previews.has(identity)) {
    const value = await api(`/api/dataset/${encodeURIComponent(name)}?version=${encodeURIComponent(W.metadata.version)}`);
    W.previews.set(identity, value);
  }
  return W.previews.get(identity);
}

export async function selectSheet(name) {
  W.sheet = name; renderSheets(); const epoch = ++W.epoch;
  if (name === 'Overview') return renderOverview();
  const table = W.metadata.tables.find(t => t.name === name);
  $('#dataset-content').innerHTML = `<div class="sheet-heading"><h1>${esc(name)}</h1><p>${esc(table.grain)}</p></div><p>Loading sample rows…</p>`;
  try {
    const data = await tablePreview(name); if (epoch !== W.epoch) return;
    const columns = data.dictionary.map(c => c.name);
    $('#dataset-content').innerHTML = `<div class="sheet-heading"><h1>${esc(name)}</h1><p>${esc(table.description)}</p>
      <span class="tag">${data.row_count.toLocaleString()} rows</span> <span class="tag">${data.column_count} columns</span>
      <span class="tag">${data.rows.length} actual sample rows</span></div>
      <div class="spreadsheet-scroll"><table class="spreadsheet"><thead><tr><th class="row-index">#</th>${columns.map(c => `<th title="${esc(data.dictionary.find(x => x.name === c).description)}">${esc(c)}</th>`).join('')}</tr></thead>
      <tbody>${data.rows.map((row, i) => `<tr><th class="row-index">${i + 1}</th>${data.dictionary.map(c => `<td class="${typeof row[c.name] === 'number' ? 'num' : ''}">${c.masked ? '<span class="muted">••• masked</span>' : row[c.name] == null ? '<span class="muted">NULL</span>' : esc(String(row[c.name]))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
    $('#dataset-status').textContent = `Read-only · ${table.grain} · Up to 30 rows · No analytical allowance used`;
  } catch (e) { if (epoch === W.epoch) $('#dataset-content').textContent = e.message; }
}

export function renderOverview() {
  const m = W.metadata;
  $('#dataset-content').innerHTML = `<div class="overview-heading"><span class="eyebrow">KNOW YOUR DATA</span><h1>One workbook. The whole business.</h1>
    <p class="dataset-intro">Explore the data behind your answers. Start with a table below, open its sample rows, then check what each column means and how tables connect.</p>
    <div class="workbook-guide"><div><strong>1 · Choose a table</strong><p>Each sheet represents a business area. “One row represents” explains what is being counted.</p></div><div><strong>2 · Read real examples</strong><p>Open a sheet to see up to 30 sample rows. Scroll sideways for more columns; this does not change your data.</p></div><div><strong>3 · Understand the connections</strong><p>The dictionary explains columns. Relationships show how tables connect and when a join could double-count records.</p></div></div>
    <details class="dataset-notice"><summary>About this dataset and sample values</summary><p>${esc(m.notice)}</p></details><div class="row"><span class="tag">${m.tables.length} governed tables</span><span class="tag">${m.relationships.length} approved relationships</span><button class="btn small" id="jump-dictionary">Data dictionary & joins ↓</button></div></div>
    <div class="overview-grid">${m.tables.map(t => `<article class="dataset-card"><div class="row"><span class="tag">${esc(t.group)}</span><button class="btn small" data-open-table="${esc(t.name)}">Open sheet ↗</button></div>
      <h2>${esc(t.name)}</h2><p>${esc(t.description)}</p><dl><dt>One row represents</dt><dd>${esc(t.grain)}</dd><dt>Unique identifier</dt><dd>${esc(t.primary_key.join(', ') || 'Not declared')}</dd>
      <dt>Main date column</dt><dd>${esc(t.date_column || 'Not declared')}</dd><dt>Questions it helps answer</dt><dd>${esc(t.uses.join(' · ') || t.description)}</dd></dl></article>`).join('')}</div>
    <section class="dictionary-section"><h2>Data dictionary & relationships</h2><p class="muted">Select a table to inspect actual types, values and approved joins. Samples are read only when needed.</p>
      <div class="dictionary-controls"><label>Table <select id="dictionary-table">${m.tables.map(t => `<option value="${esc(t.name)}">${esc(t.name)}</option>`).join('')}</select></label>
      <label>Find a column <input id="dictionary-search" type="search" placeholder="Name, meaning or data type"></label></div>
      <div id="relationship-diagram"></div><div id="dictionary-rows">Loading dictionary…</div></section>`;
  $('#dataset-content').querySelectorAll('[data-open-table]').forEach(b => b.onclick = () => selectSheet(b.dataset.openTable));
  $('#jump-dictionary').onclick = () => $('.dictionary-section').scrollIntoView({behavior:'smooth'});
  $('#dictionary-table').onchange = loadDictionary; $('#dictionary-search').oninput = renderDictionary;
  $('#dataset-status').textContent = 'Read-only · Overview from governed schema and verified join metadata';
  loadDictionary();
}

export function relationships(name) {
  const links = W.metadata.relationships.filter(r => [r.left, r.right].some(v => v.split('.')[0] === name));
  const neighbors = [...new Set(links.flatMap(r => [r.left.split('.')[0], r.right.split('.')[0]]))].filter(n => n !== name);
  const shown = neighbors.slice(0, 6), width = Math.max(600, shown.length * 190), center = width / 2;
  const box = (x,y,label) => `<rect x="${x-86}" y="${y}" width="172" height="42" rx="9"/><text x="${x}" y="${y+26}" text-anchor="middle">${esc(label)}</text>`;
  $('#relationship-diagram').innerHTML = `<div class="relationship-scroll"><svg width="${width}" height="155" role="img" aria-label="Approved relationships for ${esc(name)}">${shown.map((n,i) => `<path d="M ${center} 50 V 77 H ${95+i*190} V 101"/>`).join('')}${box(center,8,name)}${shown.map((n,i) => box(95+i*190,101,n)).join('')}</svg></div>
    ${neighbors.length > 6 ? '<p class="muted">First six neighbors shown; all joins are listed below.</p>' : ''}
    <div class="join-list">${links.map(r => `<div><code>${esc(r.condition)}</code> <span class="tag">${esc(r.cardinality)}${r.measured ? ' · measured' : ''}</span><p>${esc(r.precaution)}</p></div>`).join('') || '<p>No approved relationships declared.</p>'}</div>`;
}

export let dictionaryData = null;

export async function loadDictionary() {
  const name = $('#dictionary-table')?.value, epoch = W.epoch; if (!name) return;
  dictionaryData = null; relationships(name);
  $('#dictionary-rows').textContent = 'Reading a bounded dictionary sample…';
  try {
    const data = await tablePreview(name);
    if (epoch !== W.epoch || $('#dictionary-table')?.value !== name) return;
    dictionaryData = data; renderDictionary();
  } catch (e) { if (epoch === W.epoch && $('#dictionary-rows')) $('#dictionary-rows').textContent = e.message; }
}

export function renderDictionary() {
  if (!dictionaryData || !$('#dictionary-search')) return;
  const query = $('#dictionary-search').value.toLowerCase();
  $('#dictionary-rows').innerHTML = `<p class="muted">${dictionaryData.row_count.toLocaleString()} rows · ${dictionaryData.sampled_rows} inspected for values. Ranges say whether they cover the table or this sample.</p>
    <div class="dictionary-scroll"><table><thead><tr><th>Column / type</th><th>Business meaning</th><th>Key</th><th>Values / range</th></tr></thead><tbody>${dictionaryData.dictionary.filter(c => `${c.name} ${c.description} ${c.type}`.toLowerCase().includes(query)).map(c =>
      `<tr><td><strong>${esc(c.name)}</strong><br><code>${esc(c.type)}</code></td><td>${esc(c.description)}</td><td>${esc(c.key)}</td>
      <td><span class="tag">${esc(c.value_kind)}</span><p>${c.masked ? '•••' : c.values.map(v => esc(String(v))).join(' · ') || 'No observed non-null values'}${c.more_values ? ' · … more may exist' : ''}</p>
      ${c.range ? `<p class="muted">${esc(c.range_scope)} range: ${esc(c.range[0])} → ${esc(c.range[1])}</p>` : ''}</td></tr>`).join('')}</tbody></table></div>`;
}

$('#sheet-search').oninput = () => W.metadata && renderSheets();

export function clearAI() { W.generation=(W.generation||0)+1; W.loading=null; W.ai=null; W.models=[]; W.active=null; W.owner=null; $("#model-control").hidden=true; }

export async function refreshAI(force = false) {
  if (!S.me?.user) { $('#model-control').hidden = true; return; }
  const owner = S.me.user.id || S.me.user.email;
  if (W.owner && W.owner !== owner) clearAI();
  if (W.loading) return W.loading;
  if (W.ai && !force && Date.now() - (W.loaded || 0) < 30000) return;
  W.owner = owner; const generation=W.generation||0;
  W.loading = (async () => {
    try {
      const ai = await api('/api/ai/settings');
      if ((S.me?.user?.id || S.me?.user?.email) !== owner || generation!==(W.generation||0)) return;
      W.ai = ai; W.loaded = Date.now(); W.models = [];
      if (ai.shared_configured || ai.mode === 'personal') {
        try { const models=(await api(`/api/ai/models?mode=${ai.mode}&provider=${ai.provider}`)).models;
          if(generation!==(W.generation||0))return; W.models=models; }
        catch (e) { W.modelError = e.message; }
      }
      renderModelSelector();
    } catch { /* Account features must not prevent local demo playback or chat recovery. */ }
    finally { if(generation===(W.generation||0))W.loading = null; }
  })();
  return W.loading;
}

export function renderModelSelector() {
  if (!W.ai || !S.me?.user) return;
  $('#model-control').hidden = false;
  const models = W.models.length ? W.models : [{id:W.ai.model,name:W.ai.model}];
  $('#chat-model').innerHTML = models.map(m => `<option value="${esc(m.id)}" ${m.id === W.ai.model ? 'selected' : ''}>${esc(m.name || m.id)}</option>`).join('');
  $('#chat-model').disabled = !W.models.length;
  displayActiveModel(W.active);
  const u = W.ai.usage;
  if (u) $('#limit-info').textContent = W.ai.exempt ? `${W.ai.mode === 'personal' ? 'Personal provider billing' : 'Admin · no user quota'} · ${u.requests} requests today` : `${money(u.remaining)} shared allowance remaining · ${u.requests} requests today`;
}

export function displayActiveModel(active) {
  W.active = active;
  if (!W.ai) return;
  $('#model-provider').textContent = active ? `Active: ${active.provider} · ${active.model}` : `${W.ai.provider} · ${W.ai.mode === 'personal' ? 'personal key' : 'Pantree API'}`;
  $('#chat-model').title = active ? 'Selection applies to subsequent requests. This active run keeps its original model.' : 'Model for subsequent requests';
}

$('#chat-model').onchange = async () => {
  const model = $('#chat-model').value;
  try {
    W.ai = await api('/api/ai/settings', {method:'PUT',body:{mode:W.ai.mode,provider:W.ai.provider,model}});
    renderModelSelector(); toast(W.active ? 'Model saved for the next request. Active run unchanged.' : 'Model selected');
  } catch (e) { toast(e.message,5000); renderModelSelector(); }
};

export function usageMarkup(ai) {
  const u = ai.usage, groups = u.groups || [];
  const sum = field => groups.reduce((v,g)=>v+(g[field]||0),0);
  return `<div class="usage-grid"><div><span>Daily allowance</span><strong>${ai.exempt ? 'Exempt' : money(u.daily_limit)}</strong></div>
    <div><span>Shared used / held</span><strong>${money(u.shared_used)}</strong></div><div><span>Remaining</span><strong>${ai.exempt ? 'Provider limits apply' : money(u.remaining)}</strong></div>
    <div><span>Requests / API calls</span><strong>${u.requests} / ${sum('calls')}</strong></div></div>
    <p class="muted">${sum('input_tokens').toLocaleString()} input · ${sum('cached_tokens').toLocaleString()} cached input · ${sum('output_tokens').toLocaleString()} output · ${(sum('input_tokens')+sum('output_tokens')).toLocaleString()} total tokens</p>
    <p>${money(sum('usd'))} known estimated API cost · ${sum('uncertain_calls')} calls with pending or unknown cost. Day resets at midnight ${esc(u.timezone)}.</p>
    <p class="muted">Personal keys bill your provider account. Shared service budget and application resource controls still apply where relevant. Unknown outcomes retain conservative reservations.</p>`;
}

export async function aiDialog() {
  if (!S.me?.user) return showSignin();
  await refreshAI(true); if (!W.ai) return toast('AI settings are unavailable. Try again.',5000);
  let ai = W.ai;
  const d = dialog(`<h2>Profile settings</h2><p class="muted">${esc(S.me.user.name || S.me.user.email)} · ${S.me.user.is_admin ? 'Administrator' : 'Personal account'}</p>
    <section class="ai-section"><h3>AI Provider &amp; Model</h3><p>Choose any compatible model from your provider account. No approval required.</p>
      <label for="ai-mode">API account</label><select id="ai-mode"><option value="shared" ${ai.mode==='shared'?'selected':''}>Pantree default API</option><option value="personal" ${ai.mode==='personal'?'selected':''}>Personal API key</option></select>
      <p class="muted">Configured: ${esc(ai.default_provider)} · Main: ${esc(ai.default_model)} · Lightweight: ${esc(ai.light_model)}. Analytical runs pin the selected main model.</p>
      <div id="personal-fields"><label for="ai-provider">Provider</label><select id="ai-provider">${['openai','gemini','anthropic','groq'].map(p=>`<option value="${p}" ${p===(ai.personal?.provider || ai.provider)?'selected':''}>${{openai:'OpenAI',gemini:'Google Gemini',anthropic:'Anthropic Claude',groq:'Groq'}[p]}</option>`).join('')}</select>
      <label for="ai-key">API key ${ai.personal ? `(stored key ends ${esc(ai.personal.suffix)})` : ''}</label><input id="ai-key" type="password" autocomplete="off" spellcheck="false" maxlength="512" placeholder="Enter a key to connect or replace it">
      <div class="row"><button class="btn small" id="test-ai">Test Connection</button><button class="btn small danger" id="remove-ai" ${ai.personal?'':'hidden'}>Remove stored key</button></div>
      <p class="muted">${ai.encryption_ready ? 'Keys are encrypted on the server and never returned to the browser.' : 'Personal keys require server encryption configuration.'}</p></div>
      <p id="ai-connection" role="status"></p><label for="ai-model">Compatible model</label><select id="ai-model"></select>
      <p class="muted">New selections affect subsequent requests. Replacing or removing a key revokes that version; pending runs cannot silently use another key.</p>
      <div class="dialog-actions"><button class="btn primary" id="save-ai">Save Settings</button></div></section>
    <section class="ai-section"><h3>Daily usage</h3>${usageMarkup(ai)}</section>
    ${S.me.user.is_admin ? '<section class="ai-section"><h3>Usage management</h3><button class="btn small" id="admin-usage">View users & allowances</button><div id="admin-usage-body"></div></section>' : ''}
    <div class="dialog-actions"><button class="btn" data-close>Close</button></div>`);
  const q = s=>d.querySelector(s); let choices=[], testedKey='', testedProvider='', request=0;
  const status = text=>{q('#ai-connection').textContent=text;};
  const populate = models=>{choices=models; q('#ai-model').innerHTML=models.map(m=>`<option value="${esc(m.id)}" ${m.id===ai.model?'selected':''}>${esc(m.name || m.id)}${!m.rates?' · pricing unavailable':''}</option>`).join(''); q('#save-ai').disabled=!models.length;};
  const load = async()=>{
    const epoch=++request; q('#personal-fields').hidden=q('#ai-mode').value!=='personal'; populate([]);
    const mode=q('#ai-mode').value, provider=mode==='shared'?'openai':q('#ai-provider').value;
    if(mode==='personal' && (!ai.personal || ai.personal.provider!==provider)) {status('Enter and test a personal key.');return;}
    status('Loading models from the provider…');
    try {const r=await api(`/api/ai/models?mode=${mode}&provider=${provider}`); if(epoch!==request || !d.isConnected)return; populate(r.models); status('Compatible account models loaded.');} catch(e){if(epoch===request)status(e.message);}
  };
  q('[data-close]').onclick=()=>{q('#ai-key').value=''; testedKey=''; closeDialog();};
  q('#ai-mode').onchange=load; q('#ai-provider').onchange=()=>{testedKey='';load();};
  q('#ai-key').oninput=()=>{++request;testedKey='';populate([]);status('Test this key before saving.');};
  q('#test-ai').onclick=async()=>{
    const epoch=++request, key=q('#ai-key').value, provider=q('#ai-provider').value;
    status('Testing credentials without a billable model call…'); q('#test-ai').disabled=true;
    try {const r=await api('/api/ai/test',{method:'POST',body:{provider,api_key:key}}); if(epoch!==request || !d.isConnected)return; testedKey=key;testedProvider=provider;populate(r.models);status(r.message);}
    catch(e){if(epoch===request){populate([]);status(e.message);}} finally {q('#test-ai').disabled=false;}
  };
  q('#save-ai').onclick=async()=>{
    const mode=q('#ai-mode').value, provider=mode==='shared'?'openai':q('#ai-provider').value, key=q('#ai-key').value;
    if (mode==='personal' && key && (key!==testedKey || provider!==testedProvider)) return status('Test this key before saving.');
    const model=q('#ai-model').value; if(!choices.some(m=>m.id===model)) return;
    q('#save-ai').disabled=true;
    try {W.ai=await api('/api/ai/settings',{method:'PUT',body:{mode,provider,model,...(mode==='personal'&&key?{api_key:key}:{})}});
      q('#ai-key').value='';testedKey=''; W.loaded=0; await refreshAI(true); ai=W.ai; q('#remove-ai').hidden=!ai.personal; status('Settings saved. Active runs keep their pinned configuration.');}
    catch(e){status(e.message);} finally {q('#save-ai').disabled=false;}
  };
  q('#remove-ai').onclick=async()=>{try {await api('/api/ai/credentials',{method:'DELETE',body:{}});q('#ai-key').value='';testedKey='';W.loaded=0;await aiDialog();} catch(e){status(e.message);}};
  if(q('#admin-usage')) q('#admin-usage').onclick=()=>adminUsage(q('#admin-usage-body'));
  load();
}

export async function adminUsage(target) {
  target.textContent='Loading usage…';
  try {
    const data=await api('/api/admin/usage');
    const total=data.groups.reduce((sum,g)=>sum+g.usd,0);
    target.innerHTML=`<p>Known API cost: ${money(total)} · Ordinary shared service budget: ${money(data.shared_budget)}</p>
      <div class="dictionary-scroll"><table><thead><tr><th>User</th><th>Requests</th><th>Shared used / held</th><th>Daily allowance</th><th></th></tr></thead><tbody>${data.users.map(u=>`<tr><td>${esc(u.name)}${u.is_admin?' · Admin (exempt)':''}</td><td>${u.usage.requests || 0}</td><td>${money(u.usage.shared_used)}</td><td><input aria-label="Daily allowance for ${esc(u.name)}" type="number" min="0" max="10000" step="0.01" value="${u.usage.daily_limit}" data-allowance="${esc(u.id)}" ${u.is_admin?'disabled':''}></td><td>${u.is_admin?'':`<button class="btn small" data-save-allowance="${esc(u.id)}">Save</button>`}</td></tr>`).join('')}</tbody></table></div>
      <h4>Usage by account, provider and model</h4><div class="dictionary-scroll"><table><thead><tr><th>Account</th><th>Billing</th><th>Model</th><th>Calls</th><th>Input / cached / output</th><th>Known cost</th></tr></thead><tbody>${data.groups.map(g=>`<tr><td>${esc(data.users.find(u=>u.id===g.user_id)?.name || g.user_id)}</td><td>${esc(g.billing)}</td><td>${esc(g.provider+':'+g.model)}</td><td>${g.calls}</td><td>${g.input_tokens} / ${g.cached_tokens} / ${g.output_tokens}</td><td>${money(g.usd)}${g.uncertain_calls?' + unknown':''}</td></tr>`).join('')}</tbody></table></div>`;
    target.querySelectorAll('[data-save-allowance]').forEach(b=>b.onclick=async()=>{try {await api(`/api/admin/usage/${b.dataset.saveAllowance}`,{method:'PUT',body:{daily_limit:Number(target.querySelector(`[data-allowance="${b.dataset.saveAllowance}"]`).value)}});toast('Daily allowance saved');}catch(e){toast(e.message);}});
  } catch(e){target.textContent=e.message;}
}

export let preparingAnalyst = null, preparedAt = 0;

export async function warmAnalyst() {
  if (!S.me?.user || preparingAnalyst || Date.now()-preparedAt < 300000) return;
  const owner=S.me.user.id || S.me.user.email, status=$('#analyst-readiness');
  status.textContent='Preparing the analyst… You can browse data while it loads.';
  preparingAnalyst=api('/api/ready',{timeoutMs:90000,retries:0}).then(data=>{
    if((S.me?.user?.id || S.me?.user?.email)!==owner)return;
    if(data.ready) {preparedAt=Date.now();status.textContent='Analyst ready';}
    else status.textContent='Analyst will prepare when you ask a question.';
  }).catch(()=>{if(S.me?.user)status.textContent='Preparation is taking longer. Your question will show its progress.';})
    .finally(()=>{preparingAnalyst=null;});
}

window.addEventListener('focus',()=>warmAnalyst());
