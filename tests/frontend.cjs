// npm install --no-save --package-lock=false playwright@1.62.1
// npx playwright install chromium && node tests/frontend.cjs
// Set CHROMIUM_PATH to use an existing browser.
const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.CHROMIUM_PATH, args:['--no-sandbox']});
  try {
    const page = await browser.newPage();
    const errors = [], requests = [];
    page.on('pageerror', e=>errors.push(e.message));
    await page.addInitScript(()=>{
      window.streams = [];
      window.EventSource = class {
        constructor(url) { this.url=url; streams.push(this); }
        close() { this.closed=true; }
        emit(event) { this.onmessage?.({data:JSON.stringify(event)}); }
      };
    });
    await page.route('**/*', async route=>{
      const url=new URL(route.request().url()); requests.push(url.pathname);
      if (url.hostname!=='app.test') return route.abort();
      if (url.pathname==='/' || url.pathname.startsWith('/static/')) {
        const file=path.join(__dirname,'../src/pantree_insights/static',url.pathname==='/'?'index.html':path.basename(url.pathname));
        return route.fulfill({body:fs.readFileSync(file),contentType:file.endsWith('.js')?'application/javascript':file.endsWith('.css')?'text/css':'text/html'});
      }
      let json={};
      if (url.pathname==='/api/me') json={user:{name:'Tester',email:'test@example.com'},limit:{used:0,per_day:10},signin:{},data:{}};
      if (url.pathname==='/api/projects') json=[{id:'project',name:'Project'}];
      if (url.pathname==='/api/chats') json=['A','B'].map(id=>({id,title:id}));
      if (url.pathname==='/api/demos') json=[{slug:'sample',question:'Sample question'}];
      if (url.pathname.startsWith('/api/chats/')) {
        const id=url.pathname.split('/').pop();
        await new Promise(resolve=>setTimeout(resolve,id==='A'?250:30));
        json={title:id,messages:[{role:'user',content:`Message ${id}`}],pending_run:null};
      }
      if (url.pathname==='/api/pulse') { await new Promise(resolve=>setTimeout(resolve,200)); json={flags:[]}; }
      await route.fulfill({json});
    });
    await page.goto('https://app.test/');
    await page.evaluate(async()=>Object.assign(window, await import('/static/app.js')));
    await page.waitForSelector('[data-action="replay"]');
    await page.waitForSelector('[data-action="open-chat"]');
    const sidebarRequests=requests.filter(x=>x==='/api/projects'||x==='/api/chats').length;
    assert(!requests.some(x=>x.includes('plotly')||x.includes('pptx')));
    await page.evaluate(()=>{openChat('A');openChat('B');});
    await page.waitForTimeout(400);
    assert.deepEqual(await page.evaluate(()=>[S.chatId,$('#chat-title').textContent,$('#messages').innerText]),['B','B','Message B']);
    // A recent completed chat uses the cache without another server read.
    const chatReads=requests.filter(x=>x==='/api/chats/B').length;
    await page.evaluate(()=>{openChat('B');});
    assert.equal(await page.locator('#messages').innerText(),'Message B');
    assert.equal(requests.filter(x=>x==='/api/chats/B').length,chatReads);
    await page.evaluate(()=>{openChat('A');renderWelcome();});
    await page.waitForTimeout(350);
    assert.equal(await page.locator('#chat-title').innerText(),'New chat');
    assert.equal(await page.locator('[data-action="replay"]').count(),4);
    assert.equal(await page.locator('[data-action="example"]').count(),0);
    const beforeOffline=requests.length;
    await page.context().setOffline(true);
    for (let i=0;i<4;i++) {
      const expected=await page.evaluate(i=>PANTREE_DEMOS[i].question,i);
      await page.locator('[data-action="replay"]').nth(i).click();
      assert.equal(await page.locator('#panel-sub').innerText(),expected);
      assert.equal(await page.locator('#offline-chart').count(),1);
      assert.equal(await page.locator('#messages .answer').count(),1);
      assert.equal(await page.locator('#question').isDisabled(),true);
      assert.equal(await page.locator('[data-kind="ppt"]').count(),0);
      assert.match(await page.evaluate(()=>chartPng()),/^data:image\/png;base64,/);
      await page.locator('.new-chat').click();
    }
    await page.context().setOffline(false);
    assert.equal(requests.length,beforeOffline,'offline playback must make no network requests');
    assert.equal(await page.evaluate(()=>streams.length),0,'offline playback must not open SSE');
    const longQuestion='Which five brands and five products generated the most realized revenue during the selected sale period, and how concentrated were brand sales?';
    await page.evaluate(q=>streamRun('sample',0,null,q),longQuestion);
    assert.equal(await page.locator('#panel-sub').innerText(),longQuestion);
    await page.locator('.new-chat').click();
    await page.evaluate(()=>streams.at(-1).emit({type:'step',step:'plan',status:'started'}));
    assert.deepEqual(await page.evaluate(()=>[streams.at(-1).closed,S.busy,S.run]),[true,false,null]);
    const progressCursors=[];
    await page.route('**/api/runs/sample/progress?*', async route=>{
      const since=new URL(route.request().url()).searchParams.get('since');
      progressCursors.push(since);
      if (since==='1') {
        return route.fulfill({json:{active:true,events:[{type:'step',step:'prepare',status:'done'}]}});
      }
      await route.fulfill({json:{active:false,events:[{type:'final',status:'done',
        question:'Revenue by category',analysis:{headline:'Saved answer'},data_as_of:'2026-11-16'}]}});
    });
    await page.evaluate(()=>{
      streamRun('sample',0,null,'Revenue by category');
      streams.at(-1).emit({type:'step',step:'prepare',status:'started'});
      streams.at(-1).onerror();
    });
    await page.locator('#messages .answer h3').filter({hasText:'Saved answer'}).waitFor();
    assert.deepEqual(progressCursors,['1','2']);
    assert.equal(await page.evaluate(()=>S.busy),false);
    await page.unroute('**/api/runs/sample/progress?*');
    assert.equal(await page.locator('.send').isDisabled(),false);
    const count=requests.filter(x=>x==='/api/demos').length;
    await page.locator('.new-chat').click();
    assert.equal(requests.filter(x=>x==='/api/demos').length,count);
    await page.locator('[data-action="toggle-project"]').click();
    assert.equal(requests.filter(x=>x==='/api/projects'||x==='/api/chats').length,sidebarRequests);
    await page.locator('[data-action="show-pulse"]').click();
    assert.equal(await page.locator('#dialog [role="status"]').innerText(),'Loading…');
    await page.locator('#dialog [data-close]').click();
    await page.waitForTimeout(300);
    assert.equal(await page.locator('#dialog').isVisible(),false);
    assert.equal(await page.evaluate(()=>{
      let calls=0;const descriptor=Object.getOwnPropertyDescriptor(Element.prototype,"innerHTML");
      Object.defineProperty(Element.prototype,"innerHTML",{...descriptor,set(value){if(this.id==="steps")calls++;descriptor.set.call(this,value);}});
      showRunInPanel({status:'declined',events:[{type:'step',step:'plan',status:'started'},{type:'step',step:'plan',status:'done'}]});
      Object.defineProperty(Element.prototype,"innerHTML",descriptor);return calls;
    }),1);
    await page.route('https://cdn.jsdelivr.net/**',async route=>{
      await new Promise(resolve=>setTimeout(resolve,100));
      await route.fulfill({contentType:'application/javascript',body:'window.Plotly={newPlot:async el=>{el.dataset.drawn="yes";},purge:()=>{}};'});
    });
    await page.evaluate(()=>{
      $('#result').innerHTML='<div id="chart-area"></div>';
      window.oldChart=drawChart({figure:{data:[],layout:{}}});renderWelcome();
    });
    await page.evaluate(()=>window.oldChart);
    assert.equal(await page.locator('#chart').count(),0);
    await page.evaluate(async()=>{
      newTimeline('Chart',false);$('#result').innerHTML='<div id="chart-area"></div>';
      await drawChart({figure:{data:[],layout:{}}});
    });
    assert.equal(await page.locator('#chart').getAttribute('data-drawn'),'yes');
    assert(!await page.evaluate(()=>fmtNum(247,'lost_sales_units').includes('₹')));
    assert(await page.evaluate(()=>fmtNum(100,'net_sales').includes('₹')));
    // Verified rows arrive before analysis completes and never mark the run finished.
    await page.evaluate(()=>{
      newTimeline('Revenue',false); S.busy=true;
      onEvent({type:'result',run_id:'early',trust:'verified',data_as_of:'2026-11-22',data_size:'small',
        result:{columns:['revenue'],data:[[123.45]],truncated:false},sql:'SELECT 123.45 AS revenue'},null);
    });
    assert.match(await page.locator('#result table').innerText(),/₹123/);
    assert.equal(await page.evaluate(()=>S.run.final.result.data[0][0]),123.45);
    assert.match(await page.locator('#result h3').innerText(),/Preparing the explanation/);
    assert.equal(await page.evaluate(()=>S.busy),true);
    // A settled clarification must stop displaying an unresolved question marker.
    await page.evaluate(()=>{
      newTimeline('Clarification',false);
      onEvent({type:'step',step:'understand',status:'started'},null);
      onEvent({type:'step',step:'understand',status:'needs_input',detail:{readback:'Orders by city'}},null);
    });
    assert.equal(await page.locator('#steps .dot').innerText(),'…');
    await page.evaluate(()=>onEvent({type:'answered'},null));
    assert.equal(await page.locator('#steps .dot').innerText(),'✓');
    // Both JSON and HTML errors must leave the shell usable with a visible recovery button.
    for (const status of [429,504,200]) {
      await page.route('**/api/me',async route=>{
        await route.fulfill({status,contentType:'text/html',body:'upstream rate/timeout error'});
      });
      await page.evaluate(()=>init());
      assert.equal(await page.locator('#messages [role="alert"]').count(),1);
      assert.equal(await page.locator('.new-chat').isVisible(),true);
      assert.equal(await page.locator('[data-action="replay"]').count(),4,'demos survive startup API failure');
      await page.unroute('**/api/me');
      await page.locator('[data-action="retry-start"]').click();
      await page.locator('#messages [role="alert"]').waitFor({state:'detached'});
      await page.waitForSelector('[data-action="replay"]');
    }
    // Bounded GET retries recover transient limits; write requests are never replayed.
    let transient=0, writes=0;
    await page.route('**/api/transient',async route=>{
      transient++;
      await route.fulfill(transient===1?{status:429,headers:{'Retry-After':'0'},json:{detail:'busy'}}:{json:{ok:true}});
    });
    assert.equal(await page.evaluate(()=>api('/api/transient').then(x=>x.ok)),true);
    assert.equal(transient,2);
    await page.route('**/api/write',async route=>{writes++;await route.fulfill({status:429,json:{detail:'busy'}});});
    await page.evaluate(()=>api('/api/write',{method:'POST',body:{}}).catch(()=>{}));
    assert.equal(writes,1);
    // A failed refresh must retain the already loaded conversation and offer a cooldown.
    await page.evaluate(()=>openChat('B',true));
    let failedReads=0;
    await page.route('**/api/chats/B',async route=>{
      failedReads++;
      await route.fulfill({status:429,headers:{'Retry-After':'20'},body:'busy'});
    });
    await page.evaluate(()=>openChat('B',true));
    assert.equal(await page.locator('#chat-title').innerText(),'B');
    assert.equal(await page.locator('.read-warning').count(),1);
    assert.equal(await page.locator('[data-action="reopen-chat"]').isDisabled(),true);
    assert.equal(failedReads,1,'respect a long Retry-After without hammering the server');
    assert.equal(await page.evaluate(()=>S.busy),false);
    await page.unroute('**/api/chats/B');
    await page.route('**/api/slow',async route=>{
      await new Promise(resolve=>setTimeout(resolve,50));await route.fulfill({json:{}});
    });
    const timeoutMessage=await page.evaluate(()=>api('/api/slow',{timeoutMs:5}).catch(error=>error.message));
    assert.match(timeoutMessage,/timed out/);
    await page.waitForTimeout(100);
    // Every conversation reply uses the ordinary composer and current plan identity on desktop/mobile.
    const replies=[];
    await page.route('**/api/chats/B/ask', async route=>{
      replies.push(route.request().postDataJSON());
      return route.fulfill({json:{run_id:'conversation',since:10}});
    });
    for (const width of [1440,390]) {
      await page.setViewportSize({width,height:844});
      await page.evaluate(()=>{
        leaveView(); S.chatId='B'; newTimeline('Revenue',false);
        onEvent({type:'clarify',kind:'confirm',run_id:'conversation',plan_version:2,
                 message:'Revenue by brand. Shall I proceed?'},null);
      });
      assert.equal(await page.locator('#question').isDisabled(),false);
      assert.equal(await page.locator('.clarify, input[type=radio], #use-defaults').count(),0);
      await page.locator('#question').fill('No, by category instead');
      const beforeReply=await page.evaluate(()=>streams.length);
      await page.locator('.send').click();
      await page.waitForFunction(n=>streams.length>n,beforeReply);
      assert.equal(replies.at(-1).pending_run_id,'conversation');
      assert.equal(replies.at(-1).plan_version,2);
      await page.evaluate(()=>streams.at(-1).emit({type:'clarify',kind:'confirm',plan_version:3,
        message:'Revised: revenue by category. Shall I proceed?'}));
      assert.equal(await page.locator('#question').isDisabled(),false);
      assert.match(await page.locator('#messages').innerText(),/Revised: revenue by category/);
      const beforeCancel=await page.evaluate(()=>streams.length);
      await page.locator('#question').fill('Cancel'); await page.locator('.send').click();
      await page.waitForFunction(n=>streams.length>n,beforeCancel);
      await page.evaluate(()=>streams.at(-1).emit({type:'final',status:'cancelled',message:'Cancelled.'}));
      assert.equal(await page.evaluate(()=>S.pending),null);
      assert.match(await page.locator('#messages').innerText(),/Cancelled/);
    }
    await page.setViewportSize({width:1440,height:900});
    await page.evaluate(()=>{
      showResult({status:'done',data_as_of:'2026-11-16',analyses:[
        {purpose:'Volume',analysis:{headline:'Orders'},sql:'SELECT 1',result:{columns:['orders'],data:[[1]]}},
        {purpose:'Value',analysis:{headline:'Revenue'},sql:'SELECT 2',result:{columns:['revenue'],data:[[2]]}}
      ]}); openPanel();
    });
    await page.locator('[data-analysis="1"]').click();
    assert.equal(await page.locator('#result h3').innerText(),'Revenue');
    assert.equal(await page.locator('#result th').innerText(),'revenue');
    assert.equal(await page.locator('[data-kind="csv"]').count(),1);

    // Cancelling an adaptive next step keeps completed evidence and its historical provenance inspectable.
    await page.evaluate(()=>showResult({status:'cancelled',message:'Cancelled the pending probe.',
      context_sources:[{run_id:'abcdef123456',question:'Earlier footwear refunds',stale:true}],
      investigation_stop:'No additional query executed.',analyses:[
        {purpose:'Completed revenue query',analysis:{headline:'Observed revenue'},sql:'SELECT 2',
         result:{columns:['revenue'],data:[[2]]}}
      ]}));
    assert.equal(await page.locator('#result h3').innerText(),'Observed revenue');
    assert.match(await page.locator('#result').innerText(),/Cancelled the pending probe/);
    await page.locator('#result details summary').click();
    assert.match(await page.locator('#result details').innerText(),/Earlier footwear refunds/);
    assert.match(await page.locator('#result details').innerText(),/recomputation required/);
    assert.equal(await page.locator('#result th').innerText(),'revenue');

    // Edits work on desktop and mobile, preserve drafts on failure, and retry with one stable key.
    const editPosts=[]; let failEdit=true;
    await page.route('**/api/chats/edit-source/messages/user-edit/edit',async route=>{
      editPosts.push(route.request().postDataJSON());
      if(failEdit){failEdit=false;return route.fulfill({status:503,json:{detail:'Temporary save failure'}});}
      await route.fulfill({json:{chat_id:'edited-copy',run_id:'edited-run',mode:'fork'}});
    });
    await page.route('**/api/chats/edited-copy',route=>route.fulfill({json:{id:'edited-copy',title:'Edited question',
      messages:[{id:'saved-edit',role:'user',content:'Orders by brand',run:{original:'Revenue by brand'}}],pending_run:null}}));
    for(const width of [1440,390]) {
      await page.setViewportSize({width,height:900});
      await page.evaluate(()=>{$('#app').classList.add('panel-closed');S.chatId='edit-source';S.busy=false;S.pending=null;S.run=null;
        renderChat({title:'Editing',messages:[{id:'user-edit',role:'user',content:'Revenue by brand'}]},false);});
      await page.getByRole('button',{name:'Edit your message'}).click();
      await page.getByRole('textbox',{name:'Edit your message'}).fill('Cancelled draft');
      await page.locator('[data-edit-cancel]').click();
      assert.equal(await page.locator('.user-text').innerText(),'Revenue by brand');
      await page.getByRole('button',{name:'Edit your message'}).click();
      await page.getByRole('textbox',{name:'Edit your message'}).fill('Orders by brand');
      if(process.env.PANTREE_EDIT_SCREENSHOTS){fs.mkdirSync(process.env.PANTREE_EDIT_SCREENSHOTS,{recursive:true});await page.screenshot({path:path.join(process.env.PANTREE_EDIT_SCREENSHOTS,`edit-${width}.png`)});}
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      await page.locator('[data-edit-save]').click();
      if(width===1440){
        await page.locator('.edit-error').filter({hasText:'temporarily unavailable'}).waitFor();
        assert.equal(await page.getByRole('textbox',{name:'Edit your message'}).inputValue(),'Orders by brand');
        await page.locator('[data-edit-save]').click();
        await page.waitForFunction(()=>S.chatId==='edited-copy');
        assert.equal(editPosts[0].idempotency_key,editPosts[1].idempotency_key);
      }
      await page.waitForFunction(()=>S.chatId==='edited-copy' && document.querySelector('.user-text')?.textContent==='Orders by brand');
      assert.match(await page.locator('.message-actions').innerText(),/Edited/);
    }
    await page.setViewportSize({width:1440,height:900});

    // Offline demos appear before the account API responds, and a late failure cannot replace one.
    let releaseStartup;
    await page.route('**/api/me',async route=>{
      await new Promise(resolve=>{releaseStartup=resolve;});
      await route.fulfill({status:429,headers:{'Retry-After':'20'},body:'busy'});
    });
    await page.reload({waitUntil:'domcontentloaded'});
    await page.evaluate(async()=>Object.assign(window, await import('/static/app.js')));
    await page.waitForSelector('[data-action="replay"]');
    await page.waitForFunction(()=>S.me && !S.me.user);
    await page.locator('[data-action="replay"]').first().click();
    const offlineQuestion=await page.locator('#panel-sub').innerText();
    while (!releaseStartup) await new Promise(resolve=>setTimeout(resolve,10));
    releaseStartup();
    await page.waitForTimeout(100);
    assert.equal(await page.locator('#panel-sub').innerText(),offlineQuestion);
    assert.equal(await page.locator('#offline-chart').count(),1);
    if (process.env.SCREENSHOT_PATH) await page.screenshot({path:process.env.SCREENSHOT_PATH,fullPage:true});
    assert.deepEqual(errors,[]);
    console.log('PASS: four offline demos and PNG exports, full questions, startup failures, chat races/cache, GET retry/cooldown, single POST, stream cleanup and live charts.');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
