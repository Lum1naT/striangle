# Striangle

An independent TradingView-inspired strategy research workspace with an installable browser chart and a Django-powered trading operations dashboard. Not affiliated with TradingView.

## Trading operations

The new `/bots.html` dashboard connects real market recording, chronological backtests, paired forward paper trading and explicitly gated Binance spot execution. It includes session authentication, PostgreSQL persistence, structured AI news assessments, decision/fill journals and Render configuration. Live execution defaults off.

**[Setup, architecture, data coverage, operating limits and live activation](docs/trading-system.md)**

BTC, XRP, SOL and ETH have dedicated asset cards. Import up to 1,000,000 real one-minute candles per asset into PostgreSQL, resume interrupted imports, and export the full training dataset as CSV. The default 100,000-candle import can be queued for all four assets at once; chronological research supports 100,000 candles per job.

The existing browser chart and optimizer still run independently without a backend. Crypto candles use a public API; forex and commodities require your own Twelve Data API key. The sections below describe that browser research workspace.

## Use

Open the hosted app, or run `npm start` (Python 3 required) and visit http://localhost:8080. Node 22+ runs the checks: `npm run check` and `npm test`. Serve `dist/` on any static HTTPS host. Relative URLs support a GitHub Pages repository subpath.

- Candlestick and volume chart; scroll to zoom, drag to pan, Fit to show all candles. SMA overlays and entry/exit markers.
- SMA crossover, RSI mean reversion, or custom comparisons of close, fast SMA, slow SMA, RSI and numeric thresholds.
- Capital, percentage position sizing, fees, slippage, stop loss, take profit.
- Equity curve, closed-trade P&L, close-to-close drawdown, win rate, profit factor, and trade CSV export.
- OHLCV CSV import (up to 100,000 candles / 20 MB), synthetic CSV template, local strategy save/load and IndexedDB dataset persistence.
- PWA manifest, 192/512 icons, service-worker app-shell caching and browser install prompt where available. iOS: Safari → Share → Add to Home Screen. Offline requires one successful online load.

## Data

The initial BTC-labelled series is **deterministic synthetic demonstration data**, not historical BTC prices or a market feed. Use the market-data form to fetch real candles, or import a CSV for your own research. Required CSV columns: `time,open,high,low,close`; optional `volume`. `date` and `timestamp` aliases are accepted. Timestamps can be ISO dates or Unix seconds/milliseconds. Supply explicit time zones for date-times. Candles must be oldest first, unique, finite, positive-priced and have valid OHLC bounds. CSV is a simple numeric table, not a general quoted multiline CSV format.

Chart 1× / 4× / 24× controls aggregate consecutive bars for display only. Strategy tests always use the underlying imported timeframe. Prices use the unit of the imported data; fetched-market capital and P&L are displayed in the pair’s quote currency (including USDT, JPY or CZK); CSV/sample data defaults to USD. No currency conversion occurs.

## Backtest assumptions

Long only, one position, no leverage, no shorting. Signals are evaluated after candle close and execute at the next open. Entry budgets include fees. Fees and adverse percentage slippage apply to each side. Stop/target prices are based on the filled entry price. Gap exits use the opening price; same-bar touches of both stop and target resolve to the stop. Stops apply immediately after entry. Existing-position gap exits precede signal exits. No same-bar re-entry after an opening exit. Open positions liquidate at the final close with costs. No new final-bar entry.

Equity is marked at candle close before hypothetical liquidation costs, with actual fees charged on execution. Drawdown is based on these marks and therefore excludes intrabar losses. Buy-and-hold is gross price change, with no fees, and is not a capital-matched alternative simulation. Results omit spread variation, market impact, liquidity constraints, funding, corporate actions, borrow fees and taxes. Historical results do not predict future returns.

## Browser chart scope

This is a functional first version, not feature parity with TradingView. No Pine Script interpreter, arbitrary code execution, streaming tick feed, broker integration, chart drawing tools, cloud sync, or walk-forward framework. Strategies use one entry comparison and one exit comparison plus risk exits. One strategy save slot is available in local storage. Data remains on the user's browser; clearing browser data removes saved work.

## GitHub deployment

The repository includes validation CI and a manually triggered Pages workflow. Enable Pages with **GitHub Actions** as the source in repository settings, then run **Deploy to GitHub Pages**. GitHub plan and repository visibility can affect Pages availability.

## Structure

- `dist/index.html`, `style.css`, `app.mjs`: working UI and chart rendering.
- `dist/engine.mjs`: pure indicators, CSV validation, and backtest engine.
- `dist/sw.js`, `manifest.webmanifest`: offline app shell and installation.
- `tests/engine.test.mjs`: deterministic accounting and signal-timing tests.

When changing cached assets, increment the service-worker cache version. No client telemetry or external scripts are used.

## Real market data

- Crypto: [Binance public market-data API](https://developers.binance.com/en/docs/products/spot/faqs/market_data_only), no key. Quick switches for BTC/USDT, XRP/USDT, SOL/USDT and ETH/USDT; other Binance spot symbols can still be typed. Requests paginate backward with progress and cancellation, up to 100,000 completed bars. Exchange server time determines whether a candle is complete.
- Forex and commodities: [Twelve Data forex](https://twelvedata.com/forex) and [commodity time series](https://twelvedata.com/commodities). Enter your own key in the app. Presets include EUR/USD, GBP/USD, EUR/CZK, gold, silver, WTI and Brent spot. Pair availability, delays and history depend on provider entitlement. These are spot instruments, not futures contracts.
- Supported intervals: 1m, 5m, 15m, 1h, 4h and 1d. Fetch is manual; displayed data is a timestamped snapshot, not a streaming price. The Twelve Data newest candle is always omitted because session closing times vary; a 5,000-point response therefore yields at most 4,999 test candles. Short histories are reported by actual count.
- API requests go directly from the browser to the named provider with omitted credentials, no referrer and no HTTP cache. The user-entered Twelve Data key stays in page memory/input only, never localStorage, IndexedDB, source or service-worker caches. Reload or Clear key removes it. Never put a shared secret into client source. There is no proxy that bypasses provider restrictions.
- Completed datasets and provenance are retained in IndexedDB for offline testing. Provider requests have a 45-second timeout and cancellation; errors keep the existing chart and report failure. Provider rate limits and geographical/browser access restrictions still apply.
- Forex/commodity backtests remain unleveraged spot simulations with fractional units, not broker lot or margin-account simulations. Quote-currency P&L is not converted to the user's account currency. Fees, slippage and exits must be chosen to suit the instrument.

`tests/market-data.test.mjs` validates response normalization, incomplete-bar exclusion, pagination, quote currency and error handling with deterministic provider fixtures. Live upstream access was not verified from the restricted development environment; Twelve Data additionally requires the user's real key and relevant entitlements.

## Strategy optimizer

Open the **Optimizer** tab. Select SMA crossover, RSI mean reversion, and/or the current custom entry/exit rules. Enter lists (`10,20,30`) or inclusive ranges (`10:30:10`) for relevant indicator periods, RSI thresholds, custom numeric thresholds, stops and targets. Blank fields retain current values. Capital, position size, fees and slippage are fixed from the current strategy form. Custom rule operators and operands remain fixed; this is a parameter grid, not arbitrary strategy generation.

The app enumerates the Cartesian product within each family, omits irrelevant dimensions, and skips fast >= slow or RSI entry >= exit. Invalid values stop the search. Limits: 100 values per dimension, 10,000 raw combinations and 50 million candle evaluations. A module Web Worker runs the tests off the UI thread. Stop terminates the worker immediately and retains already delivered results. Changing the dataset clears results to avoid applying an old dataset's ranking to a new chart.

Rank by training return, drawdown (ascending), profit factor or win rate, with a minimum training-trade filter. The UI shows the top 50 eligible results; CSV export includes every completed candidate, parameters, data provenance, date boundaries and split metrics. Load applies that exact configuration to the existing strategy form and runs it against the full dataset.

Optional chronological holdout uses the last 20% or 30% of candles. Training and holdout run independently with fresh capital, flat positions and separate indicator warmup; positions do not cross the boundary. Each segment needs at least 30 candles, though long indicator periods may leave no trades. Ranking never uses holdout values. Repeatedly selecting strategies from displayed holdout performance can overfit that segment; this is not a walk-forward or statistically adjusted optimizer.

Optimizer tests cover range parsing, family products, custom thresholds, caps, independent split accounting, ranking, and an actual worker-thread execution of the production worker with fixture data.
