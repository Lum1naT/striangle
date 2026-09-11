const $ = id => document.getElementById(id);
const labels = {trend: 'Trend baseline', rsi: 'RSI baseline', ai_trend: 'AI-assisted trend'};
const assets = {BTCUSDT: 'Bitcoin', XRPUSDT: 'XRP', SOLUSDT: 'Solana', ETHUSDT: 'Ethereum'};
const fmt = (value, digits = 2) => value == null || !Number.isFinite(Number(value)) ? '—' : new Intl.NumberFormat('en-US', {minimumFractionDigits: digits, maximumFractionDigits: digits}).format(Number(value));
const when = value => value ? new Date(typeof value === 'number' ? value * 1000 : value).toISOString().replace('T', ' ').slice(0, 19) : '—';
const localInput = value => new Date(value).toISOString().slice(0, 16);
let snapshot = null, selectedId = null, selectedValidation = null, curve = [], initialized = false, polling = false, authenticated = false;
let detailSequence = 0;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function message(text, error = false) { $('globalStatus').textContent = text; $('globalStatus').classList.toggle('error', error); }
function csrf() { return document.cookie.split('; ').find(x => x.startsWith('csrftoken='))?.split('=').slice(1).join('=') || ''; }
async function api(path, payload) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 20000);
  try {
    const response = await fetch('/api/' + path, {method: payload === undefined ? 'GET' : 'POST', credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
      headers: payload === undefined ? {} : {'Content-Type': 'application/json', 'X-CSRFToken': decodeURIComponent(csrf())}, body: payload === undefined ? undefined : JSON.stringify(payload)});
    if (!response.headers.get('content-type')?.includes('application/json')) throw new Error('The Django backend is unavailable at this address. Open the app through its backend server.');
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 401 && path !== 'login/') showAuth();
      throw new Error(data.error || `Request failed (${response.status})`);
    }
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('The server did not respond in time. Check its connection before retrying.');
    throw error;
  } finally { clearTimeout(timer); }
}
function showAuth() {
  authenticated = false; selectedId = null; snapshot = null;
  $('workspace').hidden = true; $('authPanel').hidden = false; $('logout').hidden = true; $('accountName').textContent = '';
}
function showWorkspace(username) {
  authenticated = true; $('authPanel').hidden = true; $('workspace').hidden = false; $('logout').hidden = false; $('accountName').textContent = username;
}
function switchSection(id) {
  for (const panel of document.querySelectorAll('.operation-section')) panel.hidden = panel.id !== id;
  for (const button of document.querySelectorAll('[data-section]')) {
    button.classList.toggle('selected', button.dataset.section === id);
    button.setAttribute('aria-pressed', String(button.dataset.section === id));
  }
}
for (const button of document.querySelectorAll('[data-section]')) button.onclick = () => switchSection(button.dataset.section);

const configFields = [
  ['capital', 'Capital per portfolio, USDT', 10, 10000000, 1], ['allocation_pct', 'Maximum cash allocation, %', .1, 100, .1],
  ['risk_per_trade_pct', 'Planned equity risk per trade, %', .01, 5, .01], ['max_drawdown_pct', 'Maximum drawdown, %', .1, 50, .1],
  ['fee_bps', 'Fee per side, basis points', 0, 100, 1], ['slippage_bps', 'Adverse slippage, basis points', 0, 100, 1],
  ['stop_pct', 'Stop loss, %', .1, 30, .1], ['take_pct', 'Take profit, %', .1, 100, .1],
  ['daily_loss_pct', 'Daily loss limit, %', .1, 20, .1], ['max_spread_bps', 'Maximum entry spread, bp', .1, 100, .1],
  ['fast', 'Fast SMA, minutes', 2, 99, 1], ['slow', 'Slow SMA, minutes', 3, 200, 1],
  ['spread_bps', 'Assumed spread for candles, bp', 0, 100, 1], ['max_participation_pct', 'Visible depth participation, %', .1, 20, .1],
];
for (const [key, title, min, max, step] of configFields) {
  const label = element('label', title), input = document.createElement('input');
  Object.assign(input, {id: `cfg_${key}`, type: 'number', min, max, step, required: true}); label.append(input); $('riskFields').append(label);
}
function readConfig() {
  if (!$('riskForm').reportValidity()) throw new Error('Review the experiment settings.');
  return {...snapshot.defaults, ...Object.fromEntries(configFields.map(([key]) => [key, Number($(`cfg_${key}`).value)]))};
}
function setConfig(config) { for (const [key] of configFields) $(`cfg_${key}`).value = config[key]; }
function resetValidation() { selectedValidation = null; $('validationNote').textContent = 'Uses the current experiment settings. No research run linked.'; }
$('riskForm').oninput = resetValidation;
$('riskForm').onsubmit = e => e.preventDefault();
function safeLink(url, title) {
  try { if (new URL(url).protocol !== 'https:') return element('span', title); } catch { return element('span', title); }
  const a = element('a', title); a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer'; return a;
}

function renderSources() {
  const names = [['binance', 'Spot feed'], ['bybit', 'Derivatives'], ['news', 'News'], ['ai', 'AI model'], ['recorder', 'Recorder'], ['trader', 'Paper / orders'], ['research', 'Research worker']];
  $('sourceStatuses').replaceChildren(...names.map(([key, title]) => {
    const source = snapshot.sources.find(x => x.name === key), status = source?.status || 'not started';
    const card = element('div', undefined, 'source-status'); card.title = source?.detail || 'No worker heartbeat received';
    const text = element('span', status), dot = element('i', undefined, 'dot ' + (['connected', 'running'].includes(status) ? 'ok' : ['error', 'disconnected', 'stale'].includes(status) ? 'bad' : ''));
    text.prepend(dot); card.append(element('strong', title), text); return card;
  }));
}
function currentMarket() { return snapshot?.markets.find(m => m.symbol === $('botSymbol').value); }
function selectAsset(symbol) {
  $('botSymbol').value = symbol;
  resetValidation(); selectedId = null; ++detailSequence; $('runInspector').hidden = true;
  renderAssetOverview(); renderMarket(); renderRunLists(); updateDates(true);
}
function renderAssetOverview() {
  $('assetOverview').replaceChildren(...Object.entries(assets).map(([symbol, name]) => {
    const market = snapshot.markets.find(m => m.symbol === symbol), book = market?.records.book;
    const button = element('button', undefined, 'asset-card'); button.type = 'button';
    button.setAttribute('aria-pressed', String($('botSymbol').value === symbol));
    button.disabled = !market; button.onclick = () => selectAsset(symbol);
    const price = book ? (Number(book.payload.bids[0][0])+Number(book.payload.asks[0][0]))/2 : null;
    button.append(element('span', `${symbol.replace('USDT', '')} · ${name}`), element('strong', price == null ? '—' : fmt(price, price < 10 ? 4 : 2)+' USDT'),
      element('small', `${fmt(market?.candles || 0, 0)} candles · ${!book ? 'waiting for feed' : Date.now()-Date.parse(book.at)>10000 ? 'stale quote' : 'live quote'}`));
    return button;
  }));
}
function renderMarket() {
  const market = currentMarket(); if (!market) return;
  const records = market.records, book = records.book?.payload, symbol = market.symbol;
  $('bookSymbol').textContent = symbol.replace('USDT', ' / USDT'); $('exportEvents').href = '/api/events/?symbol=' + encodeURIComponent(symbol);
  $('candleCount').textContent = fmt(market.candles, 0); $('recordingSince').textContent = when(market.recording_since).slice(0, 16);
  $('datasetCoverage').textContent = market.candles ? `${fmt(market.candles, 0)} ${symbol.replace('USDT', '')} candles · ${when(market.candle_start).slice(0,16)} to ${when(market.candle_end).slice(0,16)} UTC` : 'No historical candles yet.';
  $('exportCandles').href = '/api/candles.csv?symbol='+encodeURIComponent(symbol);
  $('viewAssetChart').href = '/?symbol='+encodeURIComponent(symbol);
  $('bookRows').replaceChildren();
  if (book) {
    const bid = Number(book.bids[0][0]), ask = Number(book.asks[0][0]);
    $('marketPrice').textContent = fmt((bid + ask) / 2, ask < 10 ? 4 : 2) + ' USDT';
    const age = (Date.now() - Date.parse(records.book.at)) / 1000;
    $('bookFreshness').textContent = `${age > 10 ? 'Stale snapshot' : 'Last received'} · ${when(records.book.at)} UTC`;
    $('bookFreshness').classList.toggle('negative', age > 10);
    $('spreadValue').textContent = fmt((ask - bid) / ((ask + bid) / 2) * 10000) + ' bp';
    for (let i = 0; i < Math.min(8, book.bids.length, book.asks.length); i++) {
      const tr = document.createElement('tr');
      [fmt(book.bids[i][1], 5), fmt(book.bids[i][0], bid < 10 ? 4 : 2), fmt(book.asks[i][0], ask < 10 ? 4 : 2), fmt(book.asks[i][1], 5)].forEach(v => tr.append(element('td', v)));
      $('bookRows').append(tr);
    }
  } else { $('marketPrice').textContent = '—'; $('bookFreshness').textContent = 'Waiting for recorded observations'; $('spreadValue').textContent = '—'; }
  const deriv = records.derivatives;
  const derivStale = !deriv || Date.now() - Date.parse(deriv.at) > 60000;
  $('fundingValue').textContent = deriv ? fmt(deriv.payload.funding_rate * 100, 4) + '%' + (derivStale ? ' · stale' : '') : 'Not received';
  $('oiValue').textContent = deriv ? fmt(deriv.payload.open_interest, 2) + ' ' + symbol.replace('USDT', '') + (derivStale ? ' · stale' : '') : 'Not received';
  const liquidation = records.liquidation;
  $('liquidationValue').textContent = liquidation ? `${liquidation.payload.position_side} · ≈${fmt(Number(liquidation.payload.quantity) * Number(liquidation.payload.price), 0)} USDT · ${when(liquidation.at)}` : 'None recorded';
  $('heatmapValue').textContent = records.heatmap ? `${records.heatmap.payload.levels.length} estimated clusters · ${when(records.heatmap.at)}` : 'Not configured / unavailable';
  const ai = records.assessment;
  $('aiTitle').textContent = ai ? 'The latest recorded assessment.' : snapshot.ai_configured ? 'Waiting for fresh news.' : 'AI is not configured.';
  $('newsScore').textContent = ai ? fmt(ai.payload.score, 2) : '—'; $('newsScore').classList.toggle('negative', ai?.payload.score < 0);
  $('aiSummary').textContent = ai?.payload.summary || 'The AI portfolio will remain flat until its model and required market context are available.';
  $('aiAt').textContent = ai ? `Available ${when(ai.at)} UTC · ${ai.payload.model}` : '';
  $('aiSources').replaceChildren(...(ai?.payload.sources || []).map(s => safeLink(s.url, s.title)));
  $('newsItems').replaceChildren(...snapshot.news.slice(0, 5).map(item => {
    const article = element('div', undefined, 'news-item'); article.append(safeLink(item.url, item.title), element('small', `Published ${when(item.published_at)} UTC · received ${when(item.at)} UTC`)); return article;
  }));
  if (!snapshot.news.length) $('newsItems').append(element('p', 'No news recorded yet.', 'empty'));
}
function updateDates(force = false) {
  const market = currentMarket(); if (!market) return;
  const replay = $('researchMode').value === 'replay';
  const start = replay ? market.recording_since : market.candle_start, end = replay ? snapshot.now : market.candle_end;
  if (force || !$('researchStart').value) $('researchStart').value = start ? localInput(start) : '';
  if (force || !$('researchEnd').value) $('researchEnd').value = end ? localInput(end) : '';
}
function actionButton(text, action, className) { const b = element('button', text, className); b.type = 'button'; b.onclick = () => busy(b, action); return b; }
async function busy(button, task) {
  if (button.disabled) return;
  button.disabled = true;
  try { await task(); } catch (error) { message(error.message, true); } finally { button.disabled = false; }
}
function renderRunLists() {
  for (const id of ['paperRuns', 'researchRuns', 'liveRuns']) $(id).replaceChildren();
  const marketRuns = snapshot.runs.filter(r => r.symbol === $('botSymbol').value);
  for (const run of marketRuns) {
    const card = element('article', undefined, 'run-card'), left = element('div'), actions = element('div', undefined, 'run-actions');
    left.append(element('h3', run.name));
    const returnValue = run.metrics?.ai_trend?.return_pct ?? run.metrics?.trend?.return_pct;
    left.append(element('p', `${run.status}${run.entries_paused ? ' · entries paused' : ''}${run.flatten_requested ? ' · flatten pending' : ''} · ${when(run.started_at)} UTC`));
    if (returnValue != null) left.append(element('p', `${run.mode === 'candles' ? 'Trend holdout' : run.mode === 'replay' ? 'AI holdout' : 'AI portfolio'}: ${fmt(returnValue)}% · virtual capital ${fmt(run.config.capital, 0)} USDT`));
    if (run.error) left.append(element('p', run.error, 'negative'));
    actions.append(actionButton('Inspect', () => inspect(run.id, true)));
    if (['candles', 'replay'].includes(run.mode)) actions.append(actionButton('Use for paper', async () => {
      selectedValidation = run.id; setConfig(run.config); $('validationNote').textContent = `Frozen settings from ${run.name} · ${run.id.slice(0, 8)}. Editing settings starts a new, unlinked experiment.`; switchSection('paperPanel');
    }));
    card.append(left, actions); $(run.mode === 'paper' ? 'paperRuns' : run.mode === 'live' ? 'liveRuns' : 'researchRuns').append(card);
  }
  for (const [id, text] of [['paperRuns', 'No paper experiments for this market yet.'], ['researchRuns', 'No completed research for this market yet.'], ['liveRuns', 'No live account is armed.']]) if (!$(id).children.length) $(id).append(element('p', text, 'empty'));
  const selected = $('readinessRun').value;
  $('readinessRun').replaceChildren(...marketRuns.filter(r => r.mode === 'paper').map(r => { const o = element('option', `${when(r.started_at)} · ${r.id.slice(0, 8)} · ${r.status}`); o.value = r.id; return o; }));
  if ([...$('readinessRun').options].some(o => o.value === selected)) $('readinessRun').value = selected;
  $('checkReadiness').disabled = !$('readinessRun').options.length;
}
function renderJobs() {
  $('jobList').replaceChildren(...snapshot.jobs.slice(0, 8).map(job => {
    const row = element('div', undefined, 'job-item'); row.append(element('strong', `${job.params?.symbol?.replace('USDT', '') || ''} ${job.kind} · ${job.status}`));
    if (job.kind === 'history') {
      const done = job.result?.candles || 0, total = job.result?.requested || job.params.count;
      const progress = document.createElement('progress'); progress.max = total; progress.value = done; progress.setAttribute('aria-label', `${job.params.symbol} history import`);
      row.append(progress, element('span', `${fmt(done, 0)} / ${fmt(total, 0)} candles processed${job.result?.exhausted ? ' · reached available provider history' : ''}`));
      if (job.result?.missing_minutes) row.append(element('small', `${fmt(job.result.missing_minutes, 0)} missing minutes in this window; gaps remain unfilled.`));
      if (job.status === 'failed') row.append(actionButton('Resume import', async () => { await api(`jobs/${job.id}/resume/`, {}); await refresh(); }));
    }
    row.append(element('span', job.error || when(job.created_at)));
    if (job.result?.run_id) row.append(actionButton('View result', () => inspect(job.result.run_id, true)));
    return row;
  }));
}
async function refresh() {
  if (!authenticated || polling) return;
  polling = true;
  try {
    snapshot = await api('dashboard/');
    if (!initialized) { setConfig(snapshot.defaults); initialized = true; }
    renderSources(); renderAssetOverview(); renderMarket(); renderRunLists(); renderJobs(); updateDates();
    const activeLive = snapshot.runs.some(r => r.mode === 'live' && ['running', 'reconciling'].includes(r.status));
    $('executionBadge').textContent = activeLive ? snapshot.execution_environment : 'Paper trading';
    const stale = snapshot.sources.some(s => ['stale', 'disconnected', 'error'].includes(s.status));
    message(`Updated ${when(snapshot.now)} UTC · ${stale ? 'Some feeds or workers need attention.' : 'Dashboard connected.'} ${snapshot.ai_configured ? '' : 'AI model not configured.'}`, stale);
    if (selectedId) await inspect(selectedId, false);
  } catch (error) { message(`${error.message} Displayed observations may be stale.`, true); } finally { polling = false; }
}

async function inspect(id, scroll) {
  selectedId = id; const sequence = ++detailSequence;
  const run = await api(`runs/${id}/`);
  if (selectedId !== id || sequence !== detailSequence) return;
  $('runInspector').hidden = false; $('inspectorTitle').textContent = run.name;
  $('inspectorStatus').textContent = `${run.mode} · ${run.status} · ${run.error || 'Settings are frozen for this experiment.'}`;
  $('runControls').replaceChildren();
  if (['paper', 'live'].includes(run.mode) && ['running', 'reconciling'].includes(run.status)) {
    if (!run.flatten_requested) {
      const action = run.entries_paused ? 'resume' : 'pause';
      $('runControls').append(actionButton(run.entries_paused ? 'Resume entries' : 'Pause entries', async () => { await api(`runs/${id}/control/`, {action}); await refresh(); }));
      $('runControls').append(actionButton('Flatten & stop', async () => { await api(`runs/${id}/control/`, {action: 'stop'}); await refresh(); }, 'danger'));
    } else $('runControls').append(element('p', 'Closing positions on available market data. The run stops when flat.', 'muted small'));
  }
  const scores = ['candles', 'replay'].includes(run.mode) ? run.results.holdout.metrics : run.metrics;
  $('comparisonMetrics').replaceChildren(...Object.entries(scores).map(([strategy, m]) => {
    const card = element('article', undefined, 'comparison-card'); card.append(element('strong', labels[strategy]), element('strong', fmt(m.equity) + ' USDT', 'amount'));
    card.append(element('p', `${fmt(m.return_pct)}% net return`, m.return_pct >= 0 ? 'positive' : 'negative'), element('p', `${fmt(m.max_drawdown_pct)}% max drawdown`), element('p', `${m.closed_trades} closed trades · ${fmt(m.fees)} USDT fees`));
    if (m.halted) card.append(element('p', m.halted, 'negative'));
    if (m.open_quantity) card.append(element('span', `Open position: ${fmt(m.open_quantity, 8)}`)); return card;
  }));
  curve = ['candles', 'replay'].includes(run.mode) ? run.results.holdout.curve : run.curve;
  drawCurve();
  $('researchDetails').hidden = !['candles', 'replay'].includes(run.mode);
  $('researchDetails').replaceChildren();
  if (!$('researchDetails').hidden) {
    $('researchDetails').append(element('p', `${run.results.selected_by}. Train ${when(run.results.train_start)}–${when(run.results.train_end)} UTC. Holdout ${when(run.results.holdout_start)}–${when(run.results.holdout_end)} UTC.`, 'small muted'), element('p', run.results.holdout.assumptions || run.results.holdout.end_positions, 'small muted'));
    const details = element('details'), summary = element('summary', `${run.results.candidates.length} parameter candidates · training metrics`), pre = element('pre', JSON.stringify(run.results.candidates.map(c => ({fast: c.config.fast, slow: c.config.slow, metrics: c.train_metrics})), null, 2));
    pre.style.whiteSpace = 'pre-wrap'; details.append(summary, pre); $('researchDetails').append(details);
  }
  $('decisionJournal').replaceChildren(...run.decisions.map(d => {
    const item = element('article', undefined, 'decision'), top = element('div', undefined, 'decision-top');
    top.append(element('strong', `${labels[d.strategy]} · ${d.action.toUpperCase()}`), element('span', when(d.at) + ' UTC'));
    const details = element('details'); details.append(element('summary', `Inputs available at decision · event ${d.event_id}`), element('pre', JSON.stringify(d.features, null, 2)));
    item.append(top, element('p', d.reason), details); return item;
  }));
  if (!run.decisions.length) $('decisionJournal').append(element('p', ['replay', 'candles'].includes(run.mode) ? 'Research summaries are shown above. Forward experiments store their decisions here.' : 'Waiting for a completed candle. Signals will be logged even when the decision is to hold.', 'empty'));
  $('fillRows').replaceChildren(...run.fills.map(f => { const tr = document.createElement('tr'); [when(f.at), labels[f.strategy], f.side, fmt(f.quantity, 8), fmt(f.price), fmt(f.fee, 4), fmt(f.pnl), f.reason].forEach(v => tr.append(element('td', v))); return tr; }));
  $('exportFills').href = `/api/runs/${id}/fills.csv`;
  $('orderJournal').replaceChildren(...run.orders.map(o => element('p', `${o.kind} · ${o.status} · ${o.client_id}${o.error ? ' · ' + o.error : ''}`, 'small muted')));
  if (scroll) $('runInspector').scrollIntoView({behavior: 'smooth', block: 'start'});
}
function drawCurve() {
  const canvas = $('portfolioCurve'), rect = canvas.getBoundingClientRect(); if (rect.width < 1) return;
  const ratio = window.devicePixelRatio || 1; canvas.width = Math.round(rect.width * ratio); canvas.height = Math.round(rect.height * ratio);
  const ctx = canvas.getContext('2d'); ctx.scale(ratio, ratio); const w = rect.width, h = rect.height;
  if (!curve?.length || curve.length < 2) { ctx.fillStyle = '#959eaf'; ctx.font = '13px system-ui'; ctx.fillText('Equity history appears as completed candles arrive.', 20, h / 2); return; }
  const all = curve.flatMap(row => ['trend', 'rsi', 'ai_trend'].map(k => row[k]).filter(Number.isFinite));
  let low = Math.min(...all), high = Math.max(...all); const pad = Math.max((high - low) * .1, high * .001); low -= pad; high += pad;
  const left = 68, right = 18, top = 20, bottom = 30, start = curve[0].at, end = curve.at(-1).at;
  const x = t => left + (t - start) / Math.max(1, end - start) * (w - left - right), y = v => top + (high - v) / (high - low) * (h - top - bottom);
  ctx.font = '10px ui-monospace, monospace';
  for (let i = 0; i < 5; i++) { const value = low + (high - low) * i / 4, yy = y(value); ctx.strokeStyle = '#29313d'; ctx.beginPath(); ctx.moveTo(left, yy); ctx.lineTo(w - right, yy); ctx.stroke(); ctx.fillStyle = '#959eaf'; ctx.fillText(fmt(value, 0), 8, yy + 4); }
  for (const [key, color] of [['trend', '#8ea8ff'], ['rsi', '#dfb773'], ['ai_trend', '#39d3a2']]) {
    ctx.strokeStyle = color; ctx.lineWidth = 1.8; ctx.beginPath(); let drawing = false;
    for (const row of curve) if (Number.isFinite(row[key])) { if (!drawing) { ctx.moveTo(x(row.at), y(row[key])); drawing = true; } else ctx.lineTo(x(row.at), y(row[key])); }
    ctx.stroke();
  }
  ctx.fillStyle = '#959eaf'; ctx.fillText(when(start).slice(5, 16), left, h - 8); ctx.textAlign = 'right'; ctx.fillText(when(end).slice(5, 16) + ' UTC', w - right, h - 8);
}

$('loginForm').onsubmit = e => { e.preventDefault(); busy(e.submitter, async () => {
  await api('session/'); const data = await api('login/', {username: $('username').value, password: $('password').value}); $('password').value = ''; showWorkspace(data.username); await refresh();
}); };
$('logout').onclick = () => busy($('logout'), async () => { await api('logout/', {}); showAuth(); message('Signed out.'); });
$('historyForm').onsubmit = e => { e.preventDefault(); busy(e.submitter, async () => {
  const scope = e.submitter.value === 'all' ? {symbols: snapshot.markets.map(m => m.symbol)} : {symbol: $('botSymbol').value};
  await api('jobs/', {kind: 'history', ...scope, count: Number($('historyCount').value), ...($('historyEnd').value ? {end: Date.parse($('historyEnd').value+'Z')/1000} : {})});
  message('History import queued. Progress and saved candles appear below.'); await refresh();
}); };
$('researchForm').onsubmit = e => { e.preventDefault(); busy(e.submitter, async () => {
  const parseList = id => $(id).value.split(',').map(x => Number(x.trim()));
  await api('jobs/', {kind: 'backtest', symbol: $('botSymbol').value, mode: $('researchMode').value, selection_strategy: $('selectionStrategy').value, config: readConfig(),
    fast_values: parseList('fastGrid'), slow_values: parseList('slowGrid'), start: Date.parse($('researchStart').value + 'Z') / 1000, end: Date.parse($('researchEnd').value + 'Z') / 1000});
  message('Research queued. Parameters will be selected using training data only.'); await refresh();
}); };
$('startPaper').onclick = () => busy($('startPaper'), async () => {
  const run = await api('runs/', {symbol: $('botSymbol').value, config: readConfig(), validation_id: selectedValidation}); await refresh(); await inspect(run.id, true);
});
$('checkReadiness').onclick = () => busy($('checkReadiness'), async () => {
  const report = await api(`runs/${$('readinessRun').value}/readiness/`);
  $('readinessChecks').replaceChildren(...report.checks.map(c => {
    const row = element('div', undefined, 'readiness-check ' + (c.passed ? 'passed' : '')); row.append(element('span', c.passed ? '✓' : '○', 'check-symbol'));
    const text = element('div'); text.append(element('strong', c.label), element('p', c.detail)); row.append(text); return row;
  }));
});
$('botSymbol').onchange = () => selectAsset($('botSymbol').value);
$('researchMode').onchange = () => { if ($('researchMode').value === 'candles' && $('selectionStrategy').value === 'ai_trend') $('selectionStrategy').value = 'trend'; updateDates(true); };
$('closeInspector').onclick = () => { selectedId = null; ++detailSequence; $('runInspector').hidden = true; };
window.addEventListener('resize', drawCurve);
window.addEventListener('offline', () => message('Offline. Displayed values are historical snapshots; trading controls need the server.', true));
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
setInterval(() => { if (!document.hidden) refresh(); }, 5000);
try { const data = await api('session/'); if (data.authenticated) { showWorkspace(data.username); await refresh(); } else { showAuth(); message('Sign in to open your trading workspace.'); } }
catch (error) { message(error.message, true); }
