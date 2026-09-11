import assert from 'node:assert/strict';
import {spawn, spawnSync} from 'node:child_process';
import {mkdtempSync, mkdirSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {setTimeout as delay} from 'node:timers/promises';
import {chromium} from 'playwright';

// The fixture account exists only in a new temporary database. No collectors,
// research workers, API keys or live orders are used by this browser test.
const root = fileURLToPath(new URL('../', import.meta.url));
const temp = mkdtempSync(join(tmpdir(), 'striangle-ui-'));
const results = join(root, 'test-results');
mkdirSync(results, {recursive: true});
const python = process.env.PYTHON || 'python';
const baseURL = 'http://127.0.0.1:8765';
const env = {...process.env, DJANGO_DEBUG: 'true', DJANGO_SSL_REDIRECT: 'false',
  DJANGO_ALLOWED_HOSTS: '127.0.0.1,localhost', DATABASE_URL: `sqlite:///${join(temp, 'db.sqlite3')}`,
  OPENAI_API_KEY: '', OPENAI_MODEL: '', COINGLASS_API_KEY: '', BINANCE_API_KEY: '',
  BINANCE_API_SECRET: '', LIVE_TRADING_ENABLED: 'false', LIVE_MAX_CAPITAL: '0',
  LIVE_OWNER_ID: '', LIVE_ACCOUNT_DEDICATED: 'false', BINANCE_TESTNET: 'true'};
function manage(...args) {
  const result = spawnSync(python, [join(root, 'backend/manage.py'), ...args], {cwd: root, env, encoding: 'utf8', timeout: 30000});
  assert.equal(result.status, 0, result.stderr || result.error?.message || result.stdout);
}

let server, browser, page, logs = '';
const errors = [];
async function screenshot(name) { await page.screenshot({path: join(results, `${name}.png`), fullPage: true}); }
async function assertFits() {
  const layout = await page.evaluate(() => ({width: innerWidth, content: document.documentElement.scrollWidth,
    overflow: [...document.querySelectorAll('body *')].filter(node => {
      const rect = node.getBoundingClientRect(); return rect.width > 0 && rect.right > innerWidth + 1;
    }).slice(0, 10).map(node => ({tag: node.tagName, id: node.id, class: node.className}))}));
  assert(layout.content <= layout.width + 1, `Dashboard overflows the viewport: ${JSON.stringify(layout)}`);
}
try {
  manage('migrate', '--noinput');
  manage('shell', '-c', "from django.contrib.auth import get_user_model; get_user_model().objects.create_user('ui-fixture', password='local-ui-fixture-password', is_staff=True)");
  server = spawn(python, [join(root, 'backend/manage.py'), 'runserver', '127.0.0.1:8765', '--noreload'], {cwd: root, env, stdio: ['ignore', 'pipe', 'pipe']});
  server.stdout.on('data', data => { logs += data; });
  server.stderr.on('data', data => { logs += data; });
  server.on('error', error => { logs += error.message; });
  let healthy = false;
  for (let i = 0; i < 100; i++) {
    try { healthy = (await fetch(`${baseURL}/healthz/`, {signal: AbortSignal.timeout(500)})).ok; } catch {}
    if (healthy) break;
    await delay(100);
  }
  assert(healthy, `Django did not become healthy: ${logs}`);
  browser = await chromium.launch({headless: true});
  page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  page.setDefaultTimeout(15000);
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(`${baseURL}/bots.html`);
  await page.locator('#authPanel').waitFor({state: 'visible'});
  assert.equal(await page.locator('#workspace').isVisible(), false);
  await page.locator('#username').fill('ui-fixture');
  await page.locator('#password').fill('local-ui-fixture-password');
  await page.locator('#loginForm button').click();
  await page.locator('#sourceStatuses .source-status').first().waitFor();
  assert.equal(await page.locator('#sourceStatuses .source-status').count(), 7);
  assert.equal(await page.locator('#marketPrice').textContent(), '—');
  assert.match(await page.locator('#aiTitle').textContent(), /not configured/);
  assert.equal(await page.locator('#assetOverview button').count(),4);
  for(const symbol of ['BTCUSDT','XRPUSDT','SOLUSDT','ETHUSDT']) {
    await page.locator('#botSymbol').selectOption(symbol);
    assert.equal(await page.locator('#bookSymbol').textContent(),symbol.replace('USDT',' / USDT'));
    assert.equal(await page.locator('#exportCandles').getAttribute('href'),`/api/candles.csv?symbol=${symbol}`);
  }
  await page.locator('#botSymbol').selectOption('BTCUSDT');
  const recordQuote = (id, bid, ask) => manage('shell', '-c', `from django.utils import timezone; from trading.models import Event; now=timezone.now(); Event.objects.create(source='ui-fixture', source_id='${id}', symbol='BTCUSDT', kind='book', event_at=now, received_at=now, available_at=now, payload={'bids': [['${bid}', '10']], 'asks': [['${ask}', '10']]})`);
  recordQuote(1, '100', '100.1');
  await page.getByText('100.05 USDT', {exact:true}).first().waitFor();
  assert.match(await page.locator('#liveConnection').textContent(), /Live updates/);
  await page.locator('[data-market="BTCUSDT"]').focus();
  recordQuote(2, '100.1', '100.2');
  await page.getByText('100.15 USDT', {exact:true}).first().waitFor();
  assert.equal(await page.locator('[data-market="BTCUSDT"]').evaluate(node => node === document.activeElement), true, 'Live updates must preserve keyboard focus');
  await page.context().setOffline(true);
  await page.getByText('Browser offline', {exact:true}).waitFor();
  await page.locator('#bookFreshness').filter({hasText:'Stale book'}).waitFor();
  await page.context().setOffline(false);
  recordQuote(3, '100.2', '100.3');
  await page.getByText('100.25 USDT', {exact:true}).first().waitFor();
  await page.locator('#bookFreshness').filter({hasText:'Live book'}).waitFor();
  await assertFits();
  await screenshot('dashboard-desktop');

  await page.locator('[data-section="researchPanel"]').click();
  assert.equal(await page.locator('#cfg_capital').inputValue(), '10000');
  assert.equal(await page.locator('#researchPanel').isVisible(), true);
  assert.equal(await page.locator('#historyCount').inputValue(),'100000');
  await page.getByRole('button',{name:'Fetch all four',exact:true}).click();
  await page.locator('#jobList progress').nth(3).waitFor();
  const datasetJobs=(await (await page.request.get(`${baseURL}/api/dashboard/`)).json()).jobs;
  assert.equal(datasetJobs.length,4);
  assert.deepEqual(new Set(datasetJobs.map(job=>job.params.symbol)),new Set(['BTCUSDT','XRPUSDT','SOLUSDT','ETHUSDT']));
  assert(datasetJobs.every(job=>job.params.count===100000&&job.status==='queued'));
  // Real fitted coefficients on explicitly synthetic fixtures validate the UI,
  // not market performance. Production training uses recorded exchange candles.
  manage('shell', '-c', "from trading.tests.test_ml import bars; from trading.ml_training import fit_model; from trading.configuration import validate_config; from trading.models import MarketModel; from trading.recording import stamp; data=bars(); a,r=fit_model(data,validate_config()); a['version']='e2e-fixture'; MarketModel.objects.create(symbol='BTCUSDT',version=a['version'],artifact=a,report=r,data_end=stamp(data[-1]['closed_at']))");
  await page.getByRole('button',{name:'Train all four models',exact:true}).click();
  await page.locator('#marketModelMetrics tbody tr').nth(2).waitFor();
  assert.match(await page.locator('#marketModelTitle').textContent(), /Bitcoin.*trained price model/);
  assert.match(await page.locator('#marketModelMetrics').textContent(), /Trained market model/);
  const trainingJobs=(await (await page.request.get(`${baseURL}/api/dashboard/`)).json()).jobs.filter(j=>j.kind==='train');
  assert.equal(trainingJobs.length,4);
  await screenshot('trained-model-desktop');
  await page.locator('[data-section="paperPanel"]').click();
  const created = page.waitForResponse(response => response.url() === `${baseURL}/api/runs/` && response.request().method() === 'POST');
  await page.locator('#startPaper').click();
  const response = await created;
  assert.equal(response.status(), 201, await response.text());
  const run = await response.json();
  await page.locator('#runInspector').waitFor({state: 'visible'});
  assert.equal(await page.locator('#comparisonMetrics .comparison-card').count(), 4);
  assert.match(await page.locator('#decisionJournal').textContent(), /Waiting for live market data/);
  assert.match(await page.locator('#runActivity').textContent(), /signal checks every 1 s/);
  await page.getByRole('button', {name: 'Pause entries', exact: true}).click();
  await page.getByRole('button', {name: 'Resume entries', exact: true}).waitFor();
  await screenshot('paper-desktop');
  await page.getByRole('button', {name: 'Flatten & stop', exact: true}).click();
  await page.locator('#runControls').getByText('Closing positions', {exact: false}).waitFor();
  manage('bot_worker', '--once');
  const detail = await (await page.request.get(`${baseURL}/api/runs/${run.id}/`)).json();
  assert.equal(detail.status, 'stopped');
  assert.equal(detail.fills.length, 0);
  assert.equal(detail.orders.length, 0);
  await page.locator('#closeInspector').click();

  await page.locator('[data-section="livePanel"]').click();
  await page.locator('#checkReadiness').click();
  await page.locator('#readinessChecks .readiness-check').first().waitFor();
  const report = await (await page.request.get(`${baseURL}/api/runs/${run.id}/readiness/`)).json();
  assert.equal(report.eligible, false);
  assert(report.checks.some(check => check.label === 'Operator enablement' && !check.passed));
  await page.locator('[data-section="autonomyPanel"]').click();
  assert.equal(await page.locator('#autoLeverage').inputValue(), '10');
  await page.locator('#autoLeverage').fill('11');
  assert.equal(await page.locator('#autoLeverage').evaluate(n => n.checkValidity()), false);
  await page.locator('#autoLeverage').fill('10');
  await page.locator('#autoEnable').click();
  await page.locator('#autoStatus').filter({hasText: 'Enabled'}).waitFor();
  const automatic = (await (await page.request.get(`${baseURL}/api/dashboard/`)).json()).autonomy;
  assert.equal(automatic.config.max_leverage, 10);
  assert.equal(automatic.enabled, true);
  assert.equal(automatic.latest.status, 'queued');
  await screenshot('autonomy-desktop');
  await page.locator('#autoStop').click();
  await page.locator('#autoStatus').filter({hasText: 'Stopped'}).waitFor();
  manage('shell', '-c', "exec(open('tests/autonomy-ui-fixture.py').read())");
  await page.reload();
  await page.locator('[data-section="autonomyPanel"]').click();
  await page.locator('#autoAssetChoices article').first().waitFor();
  assert.equal(await page.locator('#autoAssetChoices article').count(), 4);
  assert.equal(await page.locator('#autoSearchProgress tbody tr').count(), 16);
  assert.equal(await page.locator('#autoPortfolios article').count(), 4);
  assert.match(await page.locator('#autoPortfolios').textContent(), /Baseline shadow/);
  assert.match(await page.locator('#autoAssetChoices').textContent(), /Selected from scikit-learn search/);
  await assertFits();
  await screenshot('autonomy-search-desktop');
  await page.setViewportSize({width: 390, height: 844});
  await assertFits();
  await screenshot('readiness-mobile');
  for (const section of ['dataPanel', 'researchPanel', 'paperPanel', 'autonomyPanel']) {
    await page.locator(`[data-section="${section}"]`).click();
    await assertFits();
    await screenshot(`${section}-mobile`);
  }
  await page.locator('#autoRunNow').click();
  await page.locator('#autoStatus').filter({hasText: 'queued'}).waitFor();
  const manualSearch = (await (await page.request.get(`${baseURL}/api/dashboard/`)).json()).autonomy;
  assert.equal(manualSearch.latest.report.trigger, 'manual');
  assert.equal(manualSearch.latest.status, 'queued');
  assert.equal(await page.locator('#autoRunNow').isDisabled(), true);
  await page.locator('#logout').click();
  await page.locator('#authPanel').waitFor({state: 'visible'});
  assert.equal((await page.request.get(`${baseURL}/api/dashboard/`)).status(), 401);
  await page.setViewportSize({width:1440,height:1000});
  const fixtureBars=Array.from({length:10001},(_,i)=>[1700000040000+i*3600000,'10','12','9','11','5',1700000040000+(i+1)*3600000-1]);
  await page.route('https://data-api.binance.vision/api/v3/**',async route=>{
    const url=new URL(route.request().url());
    const data=url.pathname.endsWith('/time')?{serverTime:fixtureBars.at(-1)[6]+1}:fixtureBars.filter(row=>row[0]<=Number(url.searchParams.get('endTime'))).slice(-Number(url.searchParams.get('limit')));
    await route.fulfill({json:data});
  });
  await page.goto(`${baseURL}/`);
  await page.locator('#quickAssets').waitFor();
  await page.locator('#marketCount').selectOption('10000');
  await page.locator('[data-asset="XRP/USDT"]').click();
  await page.getByText('Binance · Loaded 10,000 completed candles for XRP/USDT.',{exact:true}).waitFor();
  assert.equal(await page.locator('#assetIcon').textContent(),'XRP');
  assert.equal(await page.locator('#exportMarket').isEnabled(),true);
  await screenshot('four-assets-chart-desktop');
  await page.locator('#marketCount').selectOption('500');
  for(const asset of ['BTC','SOL','ETH']){
    await page.locator(`[data-asset="${asset}/USDT"]`).click();
    await page.getByText(`Binance · Loaded 500 completed candles for ${asset}/USDT.`,{exact:true}).waitFor();
    assert.equal(await page.locator('#symbol').textContent(),`${asset}/USDT`);
  }
  await page.setViewportSize({width:390,height:844});
  await assertFits();
  await screenshot('four-assets-chart-mobile');
  assert.deepEqual(errors, [], 'Browser runtime errors');
  console.log('Browser checks passed: authentication, dashboard, experiment settings, paper controls, readiness, mobile layout and logout.');
} catch (error) {
  if (page) await screenshot('failure').catch(() => {});
  throw error;
} finally {
  writeFileSync(join(results, 'django.log'), logs);
  if (browser) await browser.close();
  if (server && server.exitCode === null) {
    server.kill('SIGTERM');
    await Promise.race([new Promise(done => server.once('exit', done)), delay(3000)]);
    if (server.exitCode === null) server.kill('SIGKILL');
  }
  rmSync(temp, {recursive: true, force: true});
}
