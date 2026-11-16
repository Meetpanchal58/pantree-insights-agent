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
    await page.waitForSelector('[data-action="replay"]');
    await page.waitForSelector('[data-action="open-chat"]');
    const sidebarRequests=requests.filter(x=>x==='/api/projects'||x==='/api/chats').length;
    assert(!requests.some(x=>x.includes('plotly')||x.includes('pptx')));
    await page.evaluate(()=>{openChat('A');openChat('B');});
    await page.waitForTimeout(400);
    assert.deepEqual(await page.evaluate(()=>[S.chatId,$('#chat-title').textContent,$('#messages').innerText]),['B','B','Message B']);
    // A revisit shows cached content immediately, but still fetches fresh history.
    await page.evaluate(()=>{openChat('B');});
    assert.equal(await page.locator('#messages').innerText(),'Message B');
    await page.evaluate(()=>{openChat('A');renderWelcome();});
    await page.waitForTimeout(350);
    assert.equal(await page.locator('#chat-title').innerText(),'New chat');
    await page.locator('[data-action="replay"]').click();
    assert(await page.evaluate(()=>streams.at(-1).url.endsWith('?instant=true')));
    await page.locator('.new-chat').click();
    await page.evaluate(()=>streams.at(-1).emit({type:'step',step:'plan',status:'started'}));
    assert.deepEqual(await page.evaluate(()=>[streams.at(-1).closed,S.busy,S.run]),[true,false,null]);
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
      let calls=0;const original=renderSteps;renderSteps=()=>{calls++;original();};
      showRunInPanel({status:'declined',events:[{type:'step',step:'plan',status:'started'},{type:'step',step:'plan',status:'done'}]});
      renderSteps=original;return calls;
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
      let first=true;
      await page.route('**/api/me',async route=>{
        if (!first) return route.fallback();
        first=false;
        await route.fulfill({status,contentType:'text/html',body:'upstream rate/timeout error'});
      });
      await page.evaluate(()=>init());
      assert.equal(await page.locator('#messages [role="alert"]').count(),1);
      assert.equal(await page.locator('.new-chat').isVisible(),true);
      await page.locator('[data-action="retry-start"]').click();
      await page.waitForSelector('[data-action="replay"]');
      await page.unroute('**/api/me');
    }
    await page.route('**/api/slow',async route=>{
      await new Promise(resolve=>setTimeout(resolve,50));await route.fulfill({json:{}});
    });
    const timeoutMessage=await page.evaluate(()=>api('/api/slow',{timeoutMs:5}).catch(error=>error.message));
    assert.match(timeoutMessage,/timed out/);
    await page.waitForTimeout(100);
    assert.deepEqual(errors,[]);
    console.log('PASS: chat races, cached revisits, stream cleanup, busy reset, metadata caching, sidebar requests, dialog dismissal, batched timeline and lazy chart navigation.');
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
