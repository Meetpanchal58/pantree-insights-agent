// Optional actual-app screenshots. Supply a local signed-in Playwright storage state; never commit it.
const {chromium}=require('playwright');
const fs=require('node:fs');
(async()=>{
 const out=process.env.PANTREE_SCREENSHOT_DIR || 'docs/screenshots/pr41'; fs.mkdirSync(out,{recursive:true});
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 try {
  const page=await browser.newPage({viewport:{width:1440,height:1000},storageState:process.env.PANTREE_BROWSER_STATE});
  await page.goto(process.env.PANTREE_SCREENSHOT_URL || 'http://127.0.0.1:8041');
  await page.waitForSelector('#model-control:not([hidden])');
  await page.evaluate(()=>{document.documentElement.dataset.theme='light';});
  await page.screenshot({path:`${out}/chat-desktop.png`});
  await page.locator('[data-action="dataset"]:visible').last().click();
  await page.locator('.overview-heading').waitFor();
  await page.screenshot({path:`${out}/overview-desktop.png`});
  await page.locator('[data-sheet="sales"]').click();await page.locator('.spreadsheet tbody tr').first().waitFor();
  await page.screenshot({path:`${out}/sales-desktop.png`});
  await page.locator('[data-sheet="Overview"]').click();
  await page.locator('#dictionary-table').selectOption('product_skus');
  await page.waitForFunction(()=>dictionaryData?.table==='product_skus');
  await page.evaluate(()=>document.querySelector('.dictionary-section').scrollIntoView());
  await page.screenshot({path:`${out}/dictionary-desktop.png`});
  await page.locator('[data-action="back-chat"]').click();await page.evaluate(()=>aiDialog());
  await page.locator('#ai-mode').waitFor();await page.locator('#ai-mode').selectOption('personal');
  await page.locator('#ai-provider').selectOption('gemini');
  await page.screenshot({path:`${out}/settings-desktop.png`});
  await page.locator('#admin-usage').click();await page.locator('[data-save-allowance]').first().waitFor();
  await page.locator('#admin-usage-body').scrollIntoViewIfNeeded();
  await page.screenshot({path:`${out}/usage-desktop.png`});
  await page.locator('[data-close]').click();
  await page.setViewportSize({width:390,height:844});
  await page.evaluate(()=>{document.documentElement.dataset.theme='dark';document.querySelector('#app').classList.add('panel-closed');});
  await page.locator('[data-action="dataset"]:visible').last().click();await page.locator('.overview-heading').waitFor();
  await page.screenshot({path:`${out}/overview-mobile-dark.png`});
  await page.locator('[data-sheet="sales"]').click();await page.locator('.spreadsheet tbody tr').first().waitFor();
  await page.screenshot({path:`${out}/sales-mobile-dark.png`});
  console.log(`Captured 8 actual-app screenshots in ${out}`);
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
