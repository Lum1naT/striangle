export function setupAutonomy({$, element, fmt, when, api, busy, message, refresh}) {
  let current, configKey = '', reportKey = '', recordsKey = '';
  const fields = [
    ['capital', 'Virtual capital per challenger, USDT', 10, 10000000, 1],
    ['allocation_pct', 'Maximum cash committed to margin and entry fee, %', .1, 100, .1],
    ['risk_per_trade_pct', 'Planned equity risk per trade, %', .01, 5, .01],
    ['stop_pct', 'Price stop, %', .1, 30, .1], ['take_pct', 'Price take profit, %', .1, 100, .1],
    ['max_drawdown_pct', 'Maximum drawdown, %', .1, 50, .1], ['daily_loss_pct', 'Daily loss limit, %', .1, 20, .1],
    ['fee_bps', 'Fee per side, basis points', 0, 100, 1], ['slippage_bps', 'Adverse slippage, basis points', 0, 100, 1],
    ['funding_bps_8h', 'Adverse funding allowance / 8 hours, bp', 0, 100, .1],
    ['maintenance_margin_bps', 'Assumed maintenance margin, bp', 10, 500, 1]
  ];
  for (const [key, title, min, max, step] of fields) {
    const label = element('label', title), input = element('input');
    Object.assign(input, {id: `auto_${key}`, type: 'number', min, max, step, required: true});
    label.append(input); $('autoRiskFields').append(label);
  }
  $('autoForm').onsubmit = e => { e.preventDefault(); busy(e.submitter, async () => {
    const config = {...current.config, risk: {...current.config.risk}, max_leverage: Number($('autoLeverage').value),
      interval_hours: Number($('autoInterval').value), min_new_candles: Number($('autoFresh').value)};
    for (const [key] of fields) (key in config ? config : config.risk)[key] = Number($(`auto_${key}`).value);
    await api('autonomy/control/', {enabled: true, config});
    await refresh(); message('Automatic research enabled. Training and paper testing continue on the server.');
  }); };
  $('autoStop').onclick = () => busy($('autoStop'), async () => {
    await api('autonomy/control/', {enabled: false}); await refresh();
    message('Automatic research stopped. Open paper positions will close on fresh futures books.');
  });
  function table(headers, rows, caption) {
    const t = element('table'), head = element('thead'), tr = element('tr'), body = element('tbody');
    if (caption) t.append(element('caption', caption));
    for (const h of headers) { const th = element('th', h); th.scope = 'col'; tr.append(th); }
    head.append(tr); t.append(head, body);
    for (const values of rows) { const row = element('tr'); for (const v of values) row.append(element('td', v)); body.append(row); }
    return t;
  }
  return function render(data, canControl, full) {
    if (!data) return;
    current = data;
    $('autoStatus').textContent = `${data.enabled ? 'Enabled' : 'Stopped'} · ${data.status}${data.next_run_at && data.enabled ? ` · Next scheduled check ${when(data.next_run_at)} UTC` : ''}`;
    const key = JSON.stringify([data.config, canControl]);
    if (key !== configKey) {
      configKey = key;
      $('autoLeverage').value = data.config.max_leverage; $('autoInterval').value = data.config.interval_hours; $('autoFresh').value = data.config.min_new_candles;
      for (const [name] of fields) $(`auto_${name}`).value = data.config[name] ?? data.config.risk[name];
      for (const input of $('autoForm').querySelectorAll('input')) input.disabled = !canControl;
      $('autoEnable').disabled = !canControl;
    }
    $('autoStop').disabled = !canControl || !data.enabled;
    $('autoEnable').textContent = data.enabled ? 'Save automatic settings' : 'Enable automatic research';
    $('autoControlNote').textContent = canControl ? 'These controls manage the shared paper research service for this workspace.' : 'A workspace administrator manages automatic research. You can inspect all results here.';
    const cycle = data.cycle;
    $('autoForward').textContent = cycle ? `${cycle.status} · ${when(cycle.forward_start)} to ${when(cycle.forward_end)} UTC · Designated capital: ${cycle.selected_symbol?.replace('USDT', '') || 'cash'} · Last processed ${when(cycle.processed_at)} UTC` : 'A completed search starts these portfolios automatically.';
    const openSymbols = new Set([...$('autoPortfolios').querySelectorAll('details[open]')].map(x => x.dataset.symbol));
    $('autoPortfolios').replaceChildren(...Object.entries(cycle?.portfolios || {}).map(([symbol, p]) => {
      const m = p.metrics, c = p.candidate, card = element('article', undefined, 'comparison-card');
      card.append(element('strong', `${symbol.replace('USDT', '')} · ${c ? `${c.leverage}×` : 'cash'}`),
        element('p', c?.name || 'No eligible candidate'), element('strong', `${fmt(m.equity)} USDT`, 'amount'),
        element('p', `${fmt(m.return_pct)}% net · ${fmt(m.max_drawdown_pct)}% drawdown`, m.return_pct >= 0 ? 'positive' : 'negative'),
        element('p', `${m.closed_trades} trades · ${fmt(m.fees)} fees · ${fmt(m.funding)} funding allowance`),
        element('p', m.quantity ? `${m.side === 1 ? 'Long' : 'Short'} ${fmt(m.quantity, 6)} · Margin ${fmt(m.margin)} · Liquidation estimate ${fmt(m.liquidation_price, 4)}` : 'Flat'),
        element('p', `${p.signal?.action.toUpperCase() || 'WAIT'} · ${p.signal?.reason || 'Waiting for fresh futures observations.'}`, 'signal-reason'),
        element('p', `Futures book ${when(p.book_at)} · Mark ${when(p.mark_at)} UTC`, 'small muted'));
      if (m.halted) card.append(element('p', m.halted, 'negative'));
      if (p.signal?.inputs) { const d = element('details'); d.dataset.symbol = symbol; d.open = openSymbols.has(symbol); d.append(element('summary', 'Current decision inputs'), element('pre', JSON.stringify(p.signal.inputs, null, 2))); card.append(d); }
      return card;
    }));
    if (!full) return;
    const latest = data.latest, report = latest?.report;
    const nextReportKey = JSON.stringify(latest);
    if (nextReportKey !== reportKey) {
      reportKey = nextReportKey;
      const winner = report?.champion;
      $('autoWinner').textContent = winner ? `${winner.symbol.replace('USDT', '')} · ${winner.candidate.name} · ${winner.candidate.leverage}×` : latest ? `Research ${latest.status}` : 'Waiting for the first search.';
      $('autoResult').textContent = latest?.error || (winner ? `Validation net ${fmt(winner.metrics.return_pct)}% · Fixed-winner historical test ${fmt(report.holdout?.return_pct)}% · ${report.candidate_count} candidates. ${report.allocation_reason}` : report?.allocation_reason || 'The daily worker will train and compare strategies after you enable it.');
      $('autoLeaderboard').replaceChildren(table(['Asset', 'Strategy', 'Leverage', 'Validation net', 'Drawdown', 'Trades'],
        (report?.leaders || []).map(r => [r.symbol.replace('USDT', ''), r.candidate.name, `${r.candidate.leverage}×`, `${fmt(r.metrics.return_pct)}%`, `${fmt(r.metrics.max_drawdown_pct)}%`, r.metrics.closed_trades]), 'Eligible candidates ranked on validation only; ties favor lower drawdown, then lower leverage.'));
      $('autoMethod').replaceChildren(...(report?.method ? [`Source: ${report.source}. Snapshot ${when(latest.cutoff)} UTC.`, report.method, report.assumptions, report.forward_method, report.evidence] : ['Historical spot candles are a price proxy until enough futures candles are recorded. Funding is an adverse allowance, not actual settlement history. No future profitability is guaranteed.']).map(t => element('p', t)));
      $('autoExport').hidden = !latest; if (latest) $('autoExport').href = `/api/autonomy/cycles/${latest.id}/`;
    }
    const nextRecordsKey = JSON.stringify(data.records);
    if (nextRecordsKey !== recordsKey) {
      recordsKey = nextRecordsKey;
      $('autoRecords').replaceChildren(...(data.records || []).map(r => {
        const item = element('article', undefined, 'decision'), d = element('details');
        d.append(element('summary', 'Recorded inputs'), element('pre', JSON.stringify(r.payload, null, 2)));
        item.append(element('strong', `${r.symbol.replace('USDT', '')} · ${r.kind} · ${r.payload.action} · ${when(r.at)} UTC`), element('p', r.payload.reason), d); return item;
      }));
    }
    $('autoHistory').replaceChildren(table(['Cycle', 'Asset', 'Designated stance', 'Net return', 'Trades', 'Liquidations'], (data.history || []).flatMap(c => Object.entries(c.result).map(([s, m]) => [c.id.slice(0, 8), s.replace('USDT', ''), c.selected_symbol?.replace('USDT', '') || 'cash', `${fmt(m.return_pct)}%`, m.closed_trades, m.liquidations]))));
  };
}
