// Browser contracts for workbook navigation and provider settings. Uses the actual shipped UI with HTTP fixtures.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 try {
  const page=await browser.newPage(); const errors=[],writes=[],previews=[];
  page.on('pageerror',e=>errors.push(e.message));
  let account={mode:'shared',provider:'openai',model:'gpt-main',default_provider:'openai',default_model:'gpt-main',
    light_model:'openai:gpt-light',shared_configured:true,encryption_ready:true,personal:null,exempt:false,
    usage:{day:'2026-11-22',timezone:'UTC',groups:[],requests:1,shared_used:.12,daily_limit:1,remaining:.88}};
  const metadata={version:'snapshot-one',snapshot:{size:'small',first_day:'2025-01-01',last_day:'2026-11-22'},notice:'Governed read-only data.',
    tables:['products','sales'].map(name=>({name,description:name==='sales'?'Order lines and governed revenue':'Product styles',grain:'one row per item',
      primary_key:['item_id'],date_column:'placed_at',group:'Commerce',uses:['Revenue'],columns:[{name:'item_id',description:'Identifier',key:'primary'}]})),
    relationships:[{left:'sales.item_id',right:'products.item_id',condition:'sales.item_id = products.item_id',cardinality:'N:1',measured:true,precaution:'Aggregate facts before joining.'}]};
  await page.route('**/*',async route=>{
   const req=route.request(),u=new URL(req.url());if(u.hostname!=='app.test')return route.abort();
   if(u.pathname==='/'||u.pathname.startsWith('/static/')){const file=path.join(__dirname,'../src/pantree_insights/static',u.pathname==='/'?'index.html':path.basename(u.pathname));return route.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html'});}
   let data={};
   if(u.pathname==='/api/me')data={user:{id:'user',name:'Analyst',email:'analyst@example.test',is_admin:true},limit:{used:1,per_day:10,unlimited:true},signin:{},data:{}};
   if(['/api/projects','/api/chats','/api/demos'].includes(u.pathname))data=[];
   if(u.pathname==='/api/pulse')data={flags:[]};
   if(u.pathname==='/api/dataset')data=metadata;
   if(u.pathname.startsWith('/api/dataset/')){const table=u.pathname.split('/').pop();previews.push(table);data={version:'snapshot-one',table,row_count:4000,column_count:2,sampled_rows:512,
    rows:Array.from({length:30},(_,i)=>({item_id:`ID-${i}`,revenue:i*100})),dictionary:[{name:'item_id',type:'VARCHAR',description:'Identifier',key:'primary',values:['ID-0','ID-1'],value_kind:'sample values',more_values:true},
    {name:'revenue',type:'DOUBLE',description:'Revenue',key:'ordinary',values:[0,100],value_kind:'sample values',range:[0,51100],range_scope:'sample',more_values:true}]};}
   if(u.pathname==='/api/ai/settings'){
    if(req.method()==='PUT'){const body=req.postDataJSON();writes.push(body);account={...account,...body,personal:body.api_key?{provider:body.provider,suffix:'test'}:account.personal};delete account.api_key;}
    data=account;
   }
   if(u.pathname==='/api/ai/models')data={models:[{id:'gpt-main',name:'Main model',rates:[1,.1,5]},{id:'gpt-other',name:'Other compatible model',rates:[2,.2,10]}]};
   if(u.pathname==='/api/ai/test'){writes.push(req.postDataJSON());data={connected:true,message:'Credentials accepted. No inference performed.',models:[{id:'personal-model',name:'Personal model',rates:[1,.1,5]}]};}
   if(u.pathname==='/api/admin/usage')data={shared_budget:1,groups:[],users:[{id:'ordinary',name:'Ordinary user',is_admin:false,usage:{shared_used:.2,daily_limit:1}}]};
   if(u.pathname==='/api/admin/usage/ordinary'){writes.push(req.postDataJSON());data={};}
   await route.fulfill({json:data});
  });
  await page.goto('https://app.test/');await page.waitForSelector('#chat-model option[value="gpt-other"]', {state:'attached'});
  for(const width of [1440,390]) for(const theme of ['light','dark']) {
   await page.setViewportSize({width,height:900});await page.evaluate(theme=>{document.documentElement.dataset.theme=theme;S.chatId='chat';S.run={steps:[],question:'Orders'};S.pending={run_id:'pending',plan_version:7};$('#messages').innerHTML='<div id="preserved">Existing result and confirmation</div>';},theme);
   await page.locator('[data-action="dataset"]:visible').last().click();
   await page.locator('.overview-heading').waitFor();
   assert.equal(await page.locator('#app').isVisible(),false);
   await page.locator('#sheet-search').fill('sales');assert.equal(await page.locator('[data-sheet="products"]').count(),0);
   await page.locator('[data-sheet="sales"]').click();await page.locator('.spreadsheet tbody tr').first().waitFor();
   assert.equal(await page.locator('.spreadsheet tbody tr').count(),30);
   assert.equal(await page.locator('.spreadsheet thead th').nth(1).evaluate(e=>getComputedStyle(e).position),'sticky');
   await page.locator('[data-sheet="Overview"]').click();await page.locator('#dictionary-table').selectOption('sales');
   await page.locator('#dictionary-search').fill('identifier');await page.waitForFunction(()=>document.querySelector('#dictionary-rows').innerText.includes('ID-0'));
   assert.match(await page.locator('#dictionary-rows').innerText(),/sample values/);
   await page.locator('[data-action="back-chat"]').click();
   assert.equal(await page.locator('#preserved').innerText(),'Existing result and confirmation');
   assert.deepEqual(await page.evaluate(()=>S.pending),{run_id:'pending',plan_version:7});
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  }
  await page.evaluate(()=>$('#app').classList.add('panel-closed'));
  await page.locator('#chat-model').selectOption('gpt-other');await page.waitForFunction(()=>W.ai.model==='gpt-other');
  assert.equal(writes.at(-1).model,'gpt-other');assert.equal(await page.evaluate(()=>S.pending.plan_version),7);
  await page.evaluate(()=>aiDialog());await page.locator('#ai-mode').selectOption('personal');
  await page.locator('#ai-provider').selectOption('groq');await page.locator('#ai-key').fill('private-key-test');
  await page.locator('#test-ai').click();await page.locator('#ai-model option[value="personal-model"]').waitFor({state:'attached'});
  await page.locator('#save-ai').click();await page.waitForFunction(()=>document.querySelector('#ai-key').value==='');
  assert.equal(writes.find(w=>w.mode==='personal').provider,'groq');
  assert.equal(await page.evaluate(()=>JSON.stringify(localStorage)+JSON.stringify(sessionStorage)).then(s=>s.includes('private-key-test')),false);
  await page.locator('#admin-usage').click();await page.locator('[data-allowance="ordinary"]').fill('2.5');await page.locator('[data-save-allowance]').click();
  await page.waitForFunction(()=>document.querySelector('.toast')?.textContent.includes('saved'));
  assert(writes.some(w=>w.daily_limit===2.5));assert.deepEqual(errors,[]);
  console.log('PASS: desktop/mobile light/dark workbook, lazy previews, dictionary, preserved pending chat, model switching, personal keys and admin allowances.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
