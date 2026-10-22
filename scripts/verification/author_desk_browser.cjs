// Local production UI acceptance. No mocked routes or model responses.
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('../../frontend/node_modules/playwright');
const root = path.resolve(__dirname, '../..');
const out = path.join(root, 'out/author-desk-preview');
const linkFile = path.join(out, 'session-private.url');
const bytes = fs.readFileSync(fs.existsSync(linkFile) ? linkFile : path.join(out, 'launch-private.log'));
const log = bytes.toString(bytes[0] === 255 && bytes[1] === 254 ? 'utf16le' : 'utf8');
const token = log.match(/#plotpilot-session=([^\s\x1b"<>]+)/)?.[1];
if (!token) throw new Error('Launch local preview first');
(async () => {
  const browser = await chromium.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  const page = await context.newPage();
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  const serverErrors = []; page.on('response', response => { if (response.status() >= 500) serverErrors.push({ url: new URL(response.url()).pathname, status: response.status() }); });
  const checks = [];
  async function check(name, action) {
    try { await action(); checks.push({ name, passed:true }); console.log('PASS '+name); }
    catch (error) { checks.push({ name, passed:false, error:error.message.replaceAll(token,'[REDACTED]').slice(0,700) }); await page.screenshot({path:path.join(out,`failure-${checks.length}.png`)}); console.log('FAIL '+name); }
    fs.writeFileSync(path.join(out, 'browser-checks.json'), JSON.stringify({checks,errors,serverErrors},null,2));
  }
  async function closeModal() { const close = page.locator('.n-modal:visible .n-base-close').last(); if(await close.count()) await close.click(); else await page.getByRole('button',{name:'关闭',exact:true}).last().click(); await page.waitForTimeout(300); }
  await page.goto(`http://127.0.0.1:8018/#plotpilot-session=${token}`);
  await page.getByRole('heading', { name: '我的书库' }).waitFor();
  await page.waitForTimeout(600);
  await page.screenshot({ path: path.join(out, 'library-desktop.png') });
  await check('existing manuscript opens from fresh browser library', async()=>{
    await page.getByRole('button',{name:'继续写作',exact:true}).first().click();
    await page.waitForURL('**/workbench',{timeout:10000});
  });
  await page.goto('http://127.0.0.1:8018/book/author-desk-fog-harbor/workbench?chapter=1');
  await page.waitForTimeout(4000);
  fs.writeFileSync(path.join(out, 'workbench-visible-text.txt'), await page.locator('body').innerText());
  await page.screenshot({ path: path.join(out, 'workbench-desktop.png') });
  const editor = page.locator('.editor-input-wrapper textarea');
  const summary = { heading: await page.locator('.desk-book strong').innerText(), editor: await editor.inputValue(), errors };
  fs.writeFileSync(path.join(out, 'browser-initial.json'), JSON.stringify(summary, null, 2));
  console.log(JSON.stringify({ heading:summary.heading, editorChars:summary.editor.length, errors }));
  if(process.argv.includes('--full')) {
    await check('chapter search and switch', async () => {
      await page.locator('.chapter-search input').fill('仓库');
      await page.locator('.sidebar .n-list-item').first().click();
      await page.waitForFunction(()=>document.querySelector('.editor-input-wrapper textarea')?.value.includes('周渡'));
      await page.locator('.chapter-search input').fill('潮声');
      await page.locator('.sidebar .n-list-item').first().click();
      await page.locator('.chapter-search input').fill('');
    });
    await check('title and manuscript real save', async () => {
      const title=page.locator('.editor-title input');
      await title.fill('潮声里的信（编辑验证）');
      const original=await editor.inputValue();
      const marker='林澄将铜钥匙交给周渡保管，并在便笺上记下新仓库号一百零七。';
      if(original.includes(marker)) { if((await title.inputValue())!=='潮声里的信（编辑验证）') throw new Error('Saved title lost'); return; }
      await editor.fill(original.replaceAll('九十二','一百零七')+'\n\n'+marker);
      const saved = page.waitForResponse(r=>r.request().method()==='PUT' && /\/chapters\/1$/.test(new URL(r.url()).pathname),{timeout:30000});
      await page.locator('.editor-header-actions').getByRole('button',{name:'保存',exact:true}).click();
      const response=await saved;
      if(!response.ok()) throw new Error('Save HTTP '+response.status());
      const body=await response.json();
      fs.writeFileSync(path.join(out,'edit-save-result.json'),JSON.stringify({revision:body.revision,title:body.title,derived_status:body.derived_status,derivation_job_id:body.derivation_job_id},null,2));
      await page.waitForTimeout(1000);
      await page.reload();
      await page.waitForFunction(()=>document.querySelector('.editor-title input')?.value==='潮声里的信（编辑验证）');
      if(!(await editor.inputValue()).includes('一百零七')) throw new Error('Saved manuscript missing after reload');
    });
    await check('history and automatic reanalysis status', async()=>{
      await page.getByRole('button',{name:'历史版本',exact:true}).click();
      await page.getByText('章节历史版本',{exact:true}).waitFor();
      await page.getByRole('button',{name:'重跑章后关联分析',exact:true}).waitFor();
      await page.screenshot({path:path.join(out,'chapter-history.png')});
      await page.getByRole('button',{name:'关闭',exact:true}).last().click();
    });
    await check('all nine reference panels', async()=>{
      for(const label of ['叙事简报','当前语境','伏笔账本','故事演进','作品设定','世界观','知识库','角色档案','手稿道具']) {
        await page.locator('.reference-heading .n-select').click();
        await page.locator('.n-base-select-option').filter({hasText:new RegExp('^'+label+'$')}).click();
        await page.waitForTimeout(450);
      }
      await page.screenshot({path:path.join(out,'props-panel.png')});
    });
    await check('focus mode and explicit reference reopening', async()=>{
      await page.getByRole('button',{name:'专注',exact:true}).click();
      if(await page.locator('.desk-reference-pane').boundingBox().then(b=>b?.width>1)) throw new Error('Reference pane still consumes focus width');
      await page.getByRole('button',{name:'资料',exact:true}).click();
      if(await page.locator('.workbench.is-focused').count()) throw new Error('Explicit reference did not exit focus');
      await page.locator('.reference-heading').waitFor({state:'visible'});
    });
    await check('DAG navigation and return without remounting writer', async()=>{
      await page.getByRole('button',{name:'工作流',exact:true}).first().click();
      await page.locator('.vue-flow').waitFor({timeout:20000});
      await page.screenshot({path:path.join(out,'workflow-desktop.png')});
      await page.locator('.desk-navigation').getByRole('button',{name:'写作',exact:true}).click();
      await editor.waitFor({state:'visible'});
    });
    await check('model reasoning visible and configuration save', async()=>{
      await page.getByRole('button',{name:'模型',exact:true}).click();
      await page.getByText('思考程度与推理参数',{exact:false}).first().click();
      await page.getByText('模型思考程度',{exact:true}).first().waitFor();
      const saved=page.waitForResponse(r=>r.request().method()==='PUT' && new URL(r.url()).pathname.endsWith('/llm-control'));
      await page.getByRole('button',{name:'保存配置',exact:true}).click();
      if(!(await saved).ok()) throw new Error('Model configuration rejected');
      await page.screenshot({path:path.join(out,'model-reasoning.png')});
      await closeModal();
    });
    await check('three visual themes', async()=>{
      await page.getByRole('button',{name:'设置',exact:true}).click();
      for(const mode of ['dark','anchor','light']) {
        await page.locator('.theme-tile[data-mode="'+mode+'"]').click();
        await closeModal();
        await page.waitForTimeout(400);
        await page.screenshot({path:path.join(out,'workbench-'+mode+'.png')});
        if(mode!=='light') await page.getByRole('button',{name:'设置',exact:true}).click();
      }
    });
    await check('narrow screen chapters reference focus', async()=>{
      await page.setViewportSize({width:800,height:900});
      await page.getByRole('button',{name:'专注',exact:true}).click();
      await page.getByRole('button',{name:'章节',exact:true}).click();
      await page.locator('.mobile-panel-close').waitFor({state:'visible'});
      if((await page.locator('.desk-chapter-pane').boundingBox()).width<150) throw new Error('Chapter drawer has no usable width');
      await page.getByRole('button',{name:'关闭章节目录 ×',exact:true}).click();
      await page.getByRole('button',{name:'资料',exact:true}).click();
      // Click again if the previously expanded panel was just closed.
      if(await page.locator('.wb-right-collapsed').count()) await page.getByRole('button',{name:'资料',exact:true}).click();
      await page.locator('.reference-heading').waitFor({state:'visible'});
      await page.screenshot({path:path.join(out,'workbench-tablet.png')});
      await page.getByRole('button',{name:'资料',exact:true}).click();
      await page.setViewportSize({width:390,height:844});
      await page.screenshot({path:path.join(out,'workbench-mobile.png')});
      const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1);
      if(overflow) throw new Error('Document horizontally overflows');
    });
  }
  fs.writeFileSync(path.join(out,'browser-checks.json'),JSON.stringify({checks,errors,serverErrors},null,2));
  await browser.close();
  if(checks.some(c=>!c.passed)||errors.length||serverErrors.length) process.exitCode=1;
})().catch(error => { console.error(error.message.replaceAll(token, '[REDACTED]')); process.exitCode = 1; });
