export function setupAutonomy({$, element, fmt, when, api, busy, message, refresh}) {
  let current, latestStatus, configKey = '', reportKey = '', recordsKey = '';
  const fields = [
    ['capital', 'Virtual capital per challenger, USDT', 10, 10000000, 1],
    ['allocation_pct', 'Maximum cash committed to margin and entry fee, %', .1, 100, .1],
    ['risk_per_trade_pct', 'Planned equity risk per trade, %', .01, 5, .01],
    ['stop_pct', 'Maximum price stop for search, %', .1, 30, .1], ['take_pct', 'Maximum profit target for search, %', .1, 100, .1],
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
  $('autoRunNow').onclick = () => busy($('autoRunNow'), async () => {
    await api('autonomy/control/', {enabled: true, run_now: true}); await refresh();
    message('Search queued with saved settings. The current paper cycle will flatten when the new search is ready.');
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
    if (full) latestStatus = data.latest?.status;
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
    $('autoRunNow').disabled = !canControl || !data.enabled || ['queued', 'training', 'ready'].includes(latestStatus);
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
      if (c?.entry_description) card.append(element('p', c.entry_description, 'small'), element('p', c.exit_description, 'small'));
      if (p.baseline) card.append(element('p', `Baseline shadow: ${p.baseline.candidate.name} · ${p.baseline.candidate.leverage}× · ${fmt(p.baseline.metrics.return_pct)}% net · ${p.baseline.metrics.closed_trades} trades`, 'small muted'));
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
      $('autoResult').textContent = latest?.error || (winner ? `Validation net ${fmt(winner.metrics.return_pct)}% · Fixed-winner historical test ${fmt(report.holdout?.return_pct)}% · ${report.candidate_count} validation candidates${report.search_trial_count ? ` after ${report.search_trial_count} development trials` : ''}. ${report.allocation_reason}` : report?.allocation_reason || 'The daily worker will train and compare strategies after you enable it.');
      const searches = Object.entries(report?.fitting || {}).filter(([, fit]) => fit.optimization);
      $('autoSearchProgress').replaceChildren(searches.length ? table(['Asset', 'Round', 'Tested', 'Model-guided', 'Exploration', 'Best development score'],
        searches.flatMap(([symbol, fit]) => fit.optimization.rounds.map(r => [symbol.replace('USDT', ''), r.round, r.trials, r.model_guided, r.exploration, fmt(r.best_objective, 3)])),
        'Development scores guide exploration only. Final choices use the later validation period.') : element('p', latest ? `Research ${latest.status}. Search rounds appear when the cycle completes.` : 'Enable automatic research to start the entry/exit search.', 'muted'));
      $('autoAssetChoices').replaceChildren(...Object.entries(report?.comparisons || {}).map(([symbol, comparison]) => {
        const card = element('article', undefined, 'comparison-card'), chosen = comparison.selected, c = chosen?.candidate;
        card.append(element('strong', symbol.replace('USDT', '')), element('p', c ? `${c.name} · ${c.leverage}×` : 'No eligible candidate'),
          element('p', chosen?.origin === 'scikit_search' ? 'Selected from scikit-learn search' : chosen ? 'Fixed-family baseline won validation' : 'Cash', 'small muted'));
        if (c?.entry_description) card.append(element('p', c.entry_description), element('p', c.exit_description));
        if (chosen) card.append(element('p', `Validation ${fmt(chosen.metrics.return_pct)}% · Holdout ${fmt(chosen.holdout?.return_pct)}% · ${chosen.holdout?.closed_trades ?? 0} holdout trades`));
        for (const [role, label] of [['searched', 'Searched combination'], ['baseline', 'Fixed baseline']]) {
          const row = comparison[role];
          card.append(element('p', row ? `${label}: ${row.candidate.name} · ${row.candidate.leverage}× · validation ${fmt(row.metrics.return_pct)}% · holdout ${fmt(row.holdout?.return_pct)}%` : `${label}: no eligible candidate`, 'small muted'));
        }
        return card;
      }));
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
        item.append(element('strong', `${r.symbol.replace('USDT', '')} · ${r.role || 'selected'} · ${r.kind} · ${r.payload.action} · ${when(r.at)} UTC`), element('p', r.payload.reason), d); return item;
      }));
    }
    $('autoHistory').replaceChildren(table(['Cycle', 'Asset', 'Designated stance', 'Net return', 'Trades', 'Liquidations'], (data.history || []).flatMap(c => Object.entries(c.result).map(([s, m]) => [c.id.slice(0, 8), s.replace('USDT', ''), c.selected_symbol?.replace('USDT', '') || 'cash', `${fmt(m.return_pct)}%`, m.closed_trades, m.liquidations]))));
  };
}
