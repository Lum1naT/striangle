# Striangle trading pipeline

Striangle now has a Django backend, durable observations and trading ledgers, an authenticated operations dashboard at `/bots.html`, and three independently supervised workers. The original static chart, PWA and browser optimizer remain available at `/`.

Exchange execution scope remains **BTC/USDT, XRP/USDT, SOL/USDT and ETH/USDT, unleveraged Binance spot, long only**. The separate automatic research service models long/short USDT perpetual positions with isolated margin and a hard 10× leverage ceiling in paper trading only. No futures order-placement, borrowing or withdrawal API calls are implemented. The frontend remains plain JavaScript modules.

## Local setup

Use Python 3.12 and Node 22+ for the existing frontend tests. From the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.lock.txt
export DJANGO_DEBUG=true
.venv/bin/python backend/manage.py migrate
.venv/bin/python backend/manage.py createsuperuser
.venv/bin/python backend/manage.py runserver 127.0.0.1:8000
```

Open `http://127.0.0.1:8000/bots.html` and sign in. In three other terminals, export the same configuration and run:

```bash
.venv/bin/python backend/manage.py record_market
.venv/bin/python backend/manage.py research_worker
.venv/bin/python backend/manage.py bot_worker
```

Each command is a separate long-running process; do not run them sequentially in one terminal. SQLite is for local development only. Set `DATABASE_URL` to PostgreSQL for hosted multi-process use. `backend/.env.example` documents settings; it is not automatically loaded.

The account creation command prompts for the password without storing it in source. Public registration is intentionally absent. Django sessions and CSRF protect mutations. Account-level login throttling is stored in the database. Every run, job, ledger export and control endpoint is scoped to its owner. Public market feeds are shared between authenticated users. Credentials never enter the frontend or API responses.

## 1. Record real market data and explain decisions

The collector records:

| Feed | Recorded representation | Coverage and limitations |
|---|---|---|
| Binance spot order book | Top 20 bid/ask snapshots, normally every second | Visible depth only, not the full book or queue positions; the partial-depth stream has no exchange timestamp, so receipt time is explicitly labeled |
| Binance executed trades | Buy/sell notional aggregated into one-second receipt buckets | Real aggregate-trade messages; not a lossless individual-tick archive |
| Binance candles | Completed one-minute OHLCV | Unfinished candles are excluded |
| Bybit derivatives | Funding and open interest, at most every five seconds | A different venue; not a consolidated global positioning dataset |
| Bybit liquidations | Reported long/short liquidation events | Notional uses the reported **bankruptcy price**, not a claimed execution price |
| News RSS | Title, short summary, URL and publication timestamp | Configurable HTTPS feeds; RSS is not a low-latency institutional newswire |
| OpenAI | Structured assessment, source IDs, configured/returned model, prompt version and usage | Requires an operator-selected model and API key; absent/refused/invalid/expired assessments block AI entries |
| CoinGlass | Optional latest Model1 liquidation clusters | Estimated levels, not exact account liquidation prices; requires paid API entitlement |

Each immutable event has the provider event time, local receipt time and persistence availability time. Sequential database IDs define the recorder's ingestion order. Provider IDs deduplicate repeated events. A primary-feed disconnect records a gap, invalidates the book and clears indicator warmup. The bounded queue forces a disconnect/gap on overload. The recorder stops on database failure instead of pretending to record.

Source health and timestamps are visible in the dashboard. `Export events` streams up to 100,000 observations in JSONL; use `?symbol=BTCUSDT&after=EVENT_ID` for subsequent pages. Nothing prunes observations automatically. Monitor PostgreSQL growth and export/archive deliberately: even aggregated flows and depth snapshots accumulate substantial data over weeks.

The optional model receives a bounded set of news excerpts, with no tools or exchange credentials. Structured output is schema-validated; unknown source IDs and nonfinite values are rejected. High event risk can veto an entry. The model never changes sizing or risk limits. News is untrusted text, not executable instructions. `AI_DAILY_ATTEMPT_LIMIT` defaults to 1,200 per UTC day across the collector, allowing all four symbols to receive assessments every five minutes (about 1,152 attempts/day). A lower budget can reduce forward coverage; provider project spending budgets should also be set. The score is directional context, **not calibrated confidence or a probability of profit**. Pin `OPENAI_MODEL` to a supported model snapshot for controlled comparisons.

## 2. Backtest competing strategies

Use the asset cards or market selector to view BTC, XRP, SOL or ETH. **Fetch selected asset** and **Fetch all four** enqueue real Binance 1-minute candle imports. Choose 5,000 through 1,000,000 completed candles per asset; the default is 100,000 (about 69 days), and 525,600 is about one year. An optional **Before, UTC** cutoff loads older windows. The cutoff is frozen when queued and checked against exchange time. The provider may have fewer candles or gaps; only real returned bars are stored, and the actual count and gaps are reported.

Imports fetch at most 1,000 candles per provider request, pace requests, and commit each page together with its job cursor. Progress is visible in the dashboard. Failed imports offer **Resume import**; worker shutdowns and restarts requeue imports at their last committed page. Repeated imports do not duplicate candles or overwrite their original receipt timestamps. Eight pending jobs per user allow a four-asset batch. History imports run in the research worker, independently from recording and paper trading.

**Export training CSV** streams the selected asset's entire saved history in time order, with OHLCV, close time, fetched time, source, symbol and interval. It has no 5,000-row limit. Imported price history is available for external model training and parameter research; importing it does not train the news classifier or supply past order books, liquidation heatmaps or news. Preserve chronological splits, avoid using later data in features, and retain the provenance columns when building training datasets.

For an operator to populate all configured assets directly, run `python backend/manage.py import_history --count 100000`. Each page is durable; rerunning safely deduplicates existing candles. Use the dashboard's jobs for automatic cursor recovery. The browser chart has BTC/XRP/SOL/ETH shortcuts, fetch progress and candle CSV export, and loads up to 100,000 crypto candles per chart. Larger training datasets stay in PostgreSQL and can be exported.

Two explicitly different research modes are available:

- **Historical candles:** trend and RSI baselines only. Signals use completed bars and fill at the next open. Fixed assumed spread, adverse slippage and fees apply on both sides. Entry budgets include fees. Gaps use the open; stops take precedence on ambiguous bars; no final-bar entry; remaining exposure closes at the final close. Depth participation is not simulated from candles. This is an unleveraged spot model with no funding payments or borrow costs.
- **Recorded event replay:** trend, RSI and AI-assisted trend share the same recorded stream and independent wallets. A signal can fill only on a later observed book, subject to visible depth, participation caps, fees and adverse slippage. Exits can partially fill. Open end positions are marked at the final bid with estimated exit costs rather than fictitiously filled. Actual market impact and queue priority remain unknown.

The first 70% of observations (split by time so simultaneous observations stay together) select the fast/slow SMA parameters. The selected configuration is evaluated once on the later 30%, with fresh wallets and independent warmup. Holdout values never rank candidates. Limits: 16 parameter combinations, 100,000 historical candles or 250,000 recorded events per job. News APIs and AI generation are never called in a replay. A newly imported historical article cannot appear before its actual observation/assessment availability.

Comparisons include return after modeled costs, drawdown, fees, closed trades and open quantity. Parameter grids are research, not automatic strategy discovery or evidence of profitability. Repeatedly inspecting a holdout and adjusting settings contaminates that holdout. This first implementation provides a chronological holdout, not a rolling walk-forward optimizer or significance-adjusted search.

## 3. Paper trade on live feeds

Select **Use for paper** on a research run to carry its exact configuration into a forward experiment, or choose **Start real-time paper** for an unlinked experiment using the current settings. Active configurations cannot be edited. A new experiment starts after the latest recorded event. Up to 201 already-fetched, completed, consecutive candles seed its indicators, and already-known context retains its original timestamps. Future/unavailable candles and missing-minute bridges are excluded. Seed data creates no trades, returns, decisions or forward-evidence credit. Insufficient or stale history still blocks entries until enough current data is available.

New configurations use `decision_interval_seconds=1` (bounded to 1–60). Incoming books, flow and derivatives trigger checks at this cadence; a new completed candle, assessment or feed gap triggers an immediate check. SMA and RSI inputs remain completed one-minute candles. An adverse news assessment can therefore request an exit between candle closes. The worker polls for new events every 250 ms when idle, immediately drains a full batch, and avoids rewriting idle portfolios. Signals and fills remain ordered by recorded event ID; an order cannot fill on the same book which proposed it. Every pending buy is revalidated against current context before filling. Following a completed exit, re-entry waits for the next completed candle.

The authenticated `/api/realtime/` endpoint omits historical counts and coverage queries. The dashboard requests it approximately every second, with full metadata every 30 seconds; requests never overlap, failures back off, returning online refreshes immediately, and hidden tabs stop polling. Quote freshness continues to age even when requests fail. Inspectors show current signal reasons, indicator readiness, last evaluation and measured receipt-to-processing delay. Controls keep their focus and expanded journal entries survive refreshes. Runs execute on the server independently of the browser. These are software scheduling targets, not a guaranteed exchange-to-execution latency or an HFT service.

RSS is polled every 120 seconds and optional AI news assessments every 300 seconds; the latest valid assessment is reused. Increasing market decision frequency does not increase model-call frequency. Prior configurations without `decision_interval_seconds` keep their candle-close cadence. Engine version 1.2.0 changes the validation fingerprint, so older research evidence cannot automatically promote the new behavior into live trading.

Three independent virtual portfolios receive identical subsequent data and costs:

- **Trend:** enter when the fast SMA is above the slow SMA; exit on the reverse condition.
- **RSI:** enter at/below the lower threshold; exit at/above the upper threshold. RSI uses the simple trailing gains/losses ratio, not Wilder smoothing.
- **AI-assisted trend:** the trend must be positive, with fresh supportive news, acceptable funding, book imbalance and executed flow. Large recent short liquidations defer squeeze chasing. Estimated heatmap filtering is opt-in through `require_heatmap`.

An optional fourth portfolio uses a **trained numerical market model** when a saved model exists for the selected asset. The news interpretation layer remains separate. Neither can rewrite code, change risk limits or establish profitability by itself.

### Trained market models

In **Test strategies**, use **Train selected asset** or **Train all four models**. The research worker loads up to 100,000 already-fetched, completed one-minute candles per asset using a fixed request cutoff. At least 5,000 candles are required. Operators can queue the same work without creating an account:

```sh
python backend/manage.py train_models --enqueue --count 100000
```

This queues public-market jobs on the research worker; training does not consume the web service's memory or block the trader. Training is serialized, CPU thread pools are limited to one, and only the research process imports scientific libraries. Without `--enqueue`, the command trains directly. `--symbol BTCUSDT` limits it to one asset.

The first model is L2-regularized logistic regression, trained separately for BTC, XRP, SOL and ETH. Its ten inputs use 61 consecutive closed prices: five trailing returns, two volatility windows, two SMA distances and centered RSI. The binary target is a next-open long move over 15 minutes that exceeds fees, spread and adverse slippage on both sides. This is a target estimate, not a calibrated guarantee of a profitable trade; protective exits and live execution can produce a different outcome.

The chronological split is 60% fit, 20% validation and 20% final test. Samples whose future label reaches the next partition are removed. Standardization and weights are fitted only on training. Three regularization values (C=0.01, 0.1, 1) compete on validation log loss. The selected model is tested once, without refitting, at a prespecified 55% entry threshold. Missing-minute feature/label windows are excluded. Test classification reports AUC, log loss and Brier score against a constant training-rate baseline. The candle simulation compares model, trend and RSI after identical costs and risk settings, with a fresh portfolio and historical warmup context only. Samples overlap in time and are not independent trials.

Every model saves JSON coefficients, scaling values, software/feature versions, cutoff and source-data SHA-256, split boundaries, cost assumptions and results in PostgreSQL. Imported candles are assumed observable at their historical close for the offline benchmark; their actual import receipt times remain in provenance. Historical books, news, funding and liquidation information are never invented from those candles. Repeated inspection/retraining on the same test period contaminates it; future paper results are the next unseen evidence.

Unlinked new paper experiments automatically attach the latest supported model for that asset as a fourth wallet. The artifact and configuration fingerprint are frozen; retraining cannot replace an active run's weights. Linked historical replays retain their original strategy set. Inference uses a small standard-library dot product, cached per completed candle, while live gates check incoming events every second. A model entry requires fresh depth, flow and funding; adverse depth/flow, crowded funding, large short liquidations and any available negative news assessment can veto it. An API key is unnecessary for the price model. The separate news-assisted strategy still requires a configured news model.

Positions exit at the 15-minute horizon or through the shared protective controls. Fills still need a later observed book. Missing/stale candles, seven-day-old model history or mismatched cost settings block new model entries. Choose **Use evaluated settings** to copy a model's original settings before starting a run. Trained model comparisons are explicitly **paper-only** and fail the exchange activation gate, regardless of historical results.

Implementation references: [scikit-learn logistic regression](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.LogisticRegression.html), [chronological splitting and gaps](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html).

Changed proposals, protective exits and every closed-bar evaluation store the action, human-readable reason, inputs and event ID. Identical holds are journaled at most once a minute while the current signal view continues updating. Risk sizing limits the allocation by both available cash and estimated stop loss plus costs. Daily loss and peak drawdown limits prevent new entries and request exits. **Pause entries** preserves risk exits, which are checked on every book update independently of signal throttling. **Flatten & stop** exits on a later available book and stops only when flat; this is a request, not an instantaneous guaranteed fill. The entry gate rejects books older than the configured receipt-age limit (five seconds by default) and indicators whose last candle closed over 90 seconds ago. Database persistence delays do not make an old book fresh. The database atomically checkpoints wallets, decisions and fills so restart replay cannot double-charge them.

## 4. Controlled live execution

The code includes a Binance spot REST adapter, a persisted order outbox, partial-fill reconciliation and native OCO take-profit/stop orders. **Live operation defaults off** and testnet defaults on. No deployment, model call or real order is triggered by installing the code or opening the dashboard.

The readiness screen checks:

- At least 30 days of forward paper history, and at least 80% of 30 days' one-minute bars observed with fresh AI context across 30 UTC days.
- A stopped, flat paper experiment with at least 20 closed AI trades.
- Positive paper return after costs that exceeds the paired trend baseline, within the configured drawdown limit.
- A prior matching event replay, ending before paper trading begins, with positive AI holdout return and at least five closed holdout trades.
- Identical evaluated engine, prompt, model and configuration; explicit server enablement, account ownership and capital settings.

These are minimum operating gates, not statistical proof of an edge. New settings or sizing need a new evaluation. The code cannot manufacture forward history to pass them.

After verifying exchange product/API eligibility for your jurisdiction and account, the operator configures a **dedicated spot account** with no existing positions or orders in the configured assets, sufficient USDT, and withdrawals disabled on the API key. Set `BINANCE_API_KEY`, `BINANCE_API_SECRET`, `LIVE_OWNER_ID` (Django user ID), `LIVE_ACCOUNT_DEDICATED=true`, `LIVE_WITHDRAWALS_DISABLED_CONFIRMED=true`, `LIVE_MAX_CAPITAL`, `LIVE_TRADING_ENABLED=true` and the intended `BINANCE_TESTNET` environment. Production verifies withdrawal permissions through Binance; testnet has no wallet-permission endpoint. Keep exchange keys restricted to appropriate outbound IPs where supported.

Arming is an explicit operator command, never a browser mutation:

```bash
python backend/manage.py arm_live --paper-run PAPER_RUN_UUID --capital EVALUATED_CAPITAL --acknowledge-real-orders
```

The trader worker then processes the armed run. Only one live run may own the account. Capital must exactly match the evaluated paper allocation and stay within the operator cap. Database session locks prevent multiple traders. Testnet/production cannot be changed under an existing order ledger.

Orders use quantity/tick filters and IOC price limits to bound accepted execution prices. The order intent and client ID are committed before submission. Timeouts and ambiguous errors enter reconciliation; even an order-not-found response never triggers blind resubmission. Confirmed fills are applied once by exchange trade ID, including fees paid in base, quote or a third asset. Third-asset fees use an observed conversion rate and are identified in the ledger. The account's base position must match its ledger before another order.

Native OCO protection is submitted after an entry's final fills are reconciled. **There is a short interval between entry and successful protection.** Protection rejection requests flattening; unknown protection status pauses new entries and is queried before another action. Strategy exits cancel/reconcile protection before sizing the remaining spot sell. Partial positions below exchange minimums, outages, unavailable liquidity and price gaps can prevent immediate protection or flattening. A stop is not a guaranteed maximum loss. Unknown exchange state may require an operator to reconcile the ledger using exchange records; the app deliberately will not guess.

Before funding production, validate the complete integration on the target venue/testnet, including fee assets, permissions, OCO support, partial fills, restarts and failure recovery. Automated tests use an injected broker and never place real orders. No live exchange credentials or AI key are supplied in the repository.

## Automatic training and leveraged paper research

Open **Automatic AI** in the operations dashboard. A workspace administrator can enable the shared service, edit its paper capital/risk assumptions and leverage cap, or stop it. The operator equivalent is:

```bash
python backend/manage.py configure_autonomy --enable --max-leverage 10
python backend/manage.py configure_autonomy --disable
```

The research worker schedules a cycle immediately, then checks for new work every minute. Default retraining is every 24 hours with at least 1,440 new completed spot candles per asset since the last successful snapshot; insufficient data postpones the check by five minutes. Intervals are bounded to 6–168 hours, history to 5,000–100,000 candles per asset, and leverage to an integer 1–10. The cap is validated again in position sizing and fill accounting. Retrying failures waits one hour. An interrupted worker requeues the same immutable cutoff, rather than silently incorporating newer data. Disabling or changing settings cancels queued/ready cycles and closes active portfolios on fresh futures books; an in-progress fit can finish but cannot activate under changed settings.

Each cycle intersects the four assets' available date ranges and uses a common chronological 60% training / 20% validation / 20% final historical test split. Outcome windows crossing either boundary are purged. Four logistic models per asset learn long and short cost-covering outcomes at 15 and 60 minutes. Standardization uses training data only. Regularization C ∈ {0.01, 0.1, 1} is selected on validation log loss. The strategy grid compares two probability thresholds (35%, 55%) for each model, two SMA pairs (10/30, 20/60), RSI 14 (30/70 entries, 50 exits), and a 20-minute close breakout. Every strategy is evaluated at each integer leverage up to the cap: 12 × 10 × 4 = 480 candidates at the default. The numerical AI needs no API key; optional news assessments still require their existing configuration.

Candidates rank on validation net return after fees, assumed spread, adverse slippage and adverse funding accrual. Eligibility requires at least five closed trades, zero modeled liquidations, drawdown below the configured ceiling and no final halt. Numerical ties (eight decimal places) prefer lower drawdown, then lower leverage. The best eligible candidate across all assets is frozen and evaluated once on the final historical period; that period never selects a replacement candidate. The designated capital stance is cash unless the fixed winner has positive validation and test returns and passes the same gates. This bounded search finds the best eligible candidate in its grid, not every possible strategy or a guaranteed future winner. Repeated historical windows overlap and are not repeatedly presented as fresh unseen evidence.

For each asset, its highest-ranked eligible candidate starts an independent virtual challenger after the cycle freezes, even if its validation result is negative. These four balances are experimental comparisons, not a combined real allocation. Actual future observations produce the forward evidence. Positions fill only on a later recorded Bybit futures book with depth participation and adverse slippage; entry proposals are rechecked against fresh candles, futures books/marks, executed spot flow, funding, liquidations and optional news/heatmaps. Those live context vetoes cannot be retrospectively reconstructed from candles, so historical scores evaluate the candle rules and forward results evaluate the complete execution policy. Reasons, available inputs and fills are stored durably. Paper state and event offsets commit atomically and survive restarts. Existing spot paper runs and live-readiness gates are independent.

Paper notional is bounded by both cash committed to isolated margin plus entry fees and planned equity risk at the price stop including costs. More leverage does not automatically mean more exposure when the risk budget already binds. Defaults are 10,000 USDT per virtual portfolio, at most 10% cash committed, 0.5% planned equity risk per trade, a 2% price stop, 4% take profit, 10% maximum drawdown and 3% daily loss limit. These are simulation controls, not guarantees against gaps. A position can remain open across a feed outage until fresh execution data arrives. An expired cycle closes positions before the next ready cycle activates; no model changes an existing position.

The recorder applies every Bybit depth snapshot/delta and saves a normalized top-20 book at most once per second, completed futures candles, and mark prices in derivatives observations. Until all four assets have at least 10,000 saved futures candles (or the configured history count, if lower), the existing Binance spot history is explicitly labeled `binance_spot_proxy`. Spot history omits futures basis and cannot recreate historical depth or mark prices. Candle backtests use the candle range as a mark-price proxy, next-open execution, liquidation first on ambiguous candles, then stop before take profit, and costed closing of remaining exposure at the final close. No historical order books are invented.

Funding is a fixed **adverse accrual allowance** on remaining entry notional for either direction (default 1 bp per eight hours). It is not actual Bybit settlement funding and never grants funding credits. Maintenance margin uses a fixed 50 bp default plus closing fee reserve; venue tiers, margin deductions, ADL, insurance mechanics and other venue-specific liquidation terms are omitted. Live recorded mark prices trigger the model's isolated collateral loss, preserving uncommitted virtual cash. Historical and forward results are therefore estimates under explicit assumptions, and do not qualify these models for real leveraged execution.

The full cycle JSON endpoint `/api/autonomy/cycles/<id>/` exposes all candidate metrics, model-fit selection evidence, data hashes, settings and limitations to authenticated users. Dashboard leadership and historical reports refresh every 30 seconds; current forward paper status and reasons refresh every second. Four trained models are stored per asset per cycle; all artifacts, policies, decisions and results persist in PostgreSQL. Monitor database growth as recordings and decision journals accumulate.

Primary references: [Bybit order books](https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook), [completed klines](https://bybit-exchange.github.io/docs/v5/websocket/public/kline), [ticker mark prices](https://bybit-exchange.github.io/docs/v5/websocket/public/ticker), [isolated liquidation](https://www.bybit.com/en/help-center/article/Liquidation-Price-Calculation-under-Isolated-Mode-Unified-Trading-Account), and [funding settlement](https://www.bybit.com/en/help-center/article/Funding-fee-calculation).

## Render deployment

`render.yaml` describes a Frankfurt project with one Django web service, PostgreSQL and separate recorder, trader and research workers. The web pre-deploy command waits for a database connection before running migrations; workers wait until every migration shipped with their code is applied. Both waits are bounded to five minutes. Research does not block execution. SIGTERM stops new work and drains/checkpoints where possible. Interrupted history imports and automatic cycles requeue with their existing cursor/cutoff; other interrupted research jobs are marked failed on restart.

Render [overlaps worker instances during deploys](https://render.com/docs/deploys#zero-downtime-deploys). A replacement waits up to seven minutes for the previous instance's PostgreSQL advisory lock before processing anything; it never takes ownership from a running worker. This covers Render's 60-second overlap and the research worker's maximum 300-second shutdown. Worker start commands use `exec` so SIGTERM reaches Python. Ownership changes and persisted worker/provider heartbeats appear in Render logs, at most once a minute for an unchanged status. Recorder heartbeats are written only after an observation batch commits.

Create a [Render Blueprint from this repository](https://dashboard.render.com/blueprint/new?repo=https://github.com/Lum1naT/striangle), inspect the proposed services, and apply it. Provisioning these services incurs Render charges. PostgreSQL is pinned to version 17, matching CI, with 15 GB of initial storage; monitor recording growth and expand storage or archive observations before it fills. After creation, add optional `OPENAI_API_KEY`, `OPENAI_MODEL` and `COINGLASS_API_KEY` to the shared **striangle-runtime** environment group in Render, then deploy the affected services. Leave them absent to start with market recording and baseline research. Render [ignores `sync: false` inside environment groups](https://render.com/docs/blueprint-spec#prompting-for-secret-values), so these secrets are deliberately not declared there in YAML. Create the first Django user using `python backend/manage.py createsuperuser` in the web service shell. The service root stays at the repository root so both `backend/` and `dist/` are available; Render excludes files outside a configured [service root directory](https://render.com/docs/monorepo-support). The application uses `RENDER_EXTERNAL_HOSTNAME` automatically; add custom domains to `DJANGO_ALLOWED_HOSTS` when needed. Keep `DJANGO_DEBUG=false` in production. The Pages workflow still serves the static chart only; the bot dashboard requires Django and the workers.

Live exchange secrets are deliberately absent from the Blueprint. Configure them only for a dedicated execution deployment after review. Keep any subsequent Blueprint sync from resetting intentional live environment changes; review the defaults before syncing.

## Verification

```bash
DJANGO_DEBUG=true .venv/bin/python backend/manage.py test trading.tests
DJANGO_DEBUG=true .venv/bin/python backend/manage.py makemigrations --check --dry-run
npm run check
npm test
```

CI runs backend tests against PostgreSQL, matching the hosted database. Tests cover temporal separation, identical baseline costs, accounting, stale feeds, idempotent event processing, partial fills, API ownership, CSRF, readiness gates, unknown order status and fee-asset reconciliation. A separate Chromium test starts an isolated Django server and checks login, dashboard navigation, paper controls, readiness, logout and mobile layout without external data or trading credentials. Its screenshots are uploaded as CI artifacts. Run it locally with `PYTHON=.venv/bin/python node tests/bot-ui.e2e.mjs` after installing Playwright 1.62.1 and its Chromium browser.

## Primary API references

- [Binance market-data-only endpoints](https://developers.binance.com/en/docs/products/spot/faqs/market_data_only)
- [Binance market streams](https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams)
- [Binance spot trading endpoints](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/trading-endpoints)
- [Binance account and fills](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/account-endpoints)
- [Bybit liquidations](https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation)
- [Bybit derivatives tickers](https://bybit-exchange.github.io/docs/v5/websocket/public/ticker)
- [CoinGlass Model1 heatmap](https://docs.coinglass.com/reference/liquidation-heatmap)
- [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- [Render Django](https://render.com/docs/deploy-django) and [background workers](https://render.com/docs/background-workers)
