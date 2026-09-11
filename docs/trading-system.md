# Striangle trading pipeline

Striangle now has a Django backend, durable observations and trading ledgers, an authenticated operations dashboard at `/bots.html`, and three independently supervised workers. The original static chart, PWA and browser optimizer remain available at `/`.

Initial execution scope is **BTC/USDT and ETH/USDT, unleveraged Binance spot, long only, one-minute signals**. Bybit derivatives provide context. No futures positions, borrowing, leverage or withdrawal API calls are implemented. The frontend remains plain JavaScript modules; there is no frontend framework migration.

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

The optional model receives a bounded set of news excerpts, with no tools or exchange credentials. Structured output is schema-validated; unknown source IDs and nonfinite values are rejected. High event risk can veto an entry. The model never changes sizing or risk limits. News is untrusted text, not executable instructions. `AI_DAILY_ATTEMPT_LIMIT` defaults to 600 per UTC day across the collector, allowing two symbols to receive assessments every five minutes (about 576 attempts/day). A lower budget can reduce forward coverage; provider project spending budgets should also be set. The score is directional context, **not calibrated confidence or a probability of profit**. Pin `OPENAI_MODEL` to a supported model snapshot for controlled comparisons.

## 2. Backtest competing strategies

Use **Fetch history** to enqueue a real Binance candle import, then choose a UTC research interval. Imports paginate up to 5,000 completed bars using exchange time. No fabricated data is inserted into the dashboard. Provider failures leave a failed job and preserve existing data.

Two explicitly different research modes are available:

- **Historical candles:** trend and RSI baselines only. Signals use completed bars and fill at the next open. Fixed assumed spread, adverse slippage and fees apply on both sides. Entry budgets include fees. Gaps use the open; stops take precedence on ambiguous bars; no final-bar entry; remaining exposure closes at the final close. Depth participation is not simulated from candles. This is an unleveraged spot model with no funding payments or borrow costs.
- **Recorded event replay:** trend, RSI and AI-assisted trend share the same recorded stream and independent wallets. A signal can fill only on a later observed book, subject to visible depth, participation caps, fees and adverse slippage. Exits can partially fill. Open end positions are marked at the final bid with estimated exit costs rather than fictitiously filled. Actual market impact and queue priority remain unknown.

The first 70% of observations (split by time so simultaneous observations stay together) select the fast/slow SMA parameters. The selected configuration is evaluated once on the later 30%, with fresh wallets and independent warmup. Holdout values never rank candidates. Limits: 16 parameter combinations, 20,000 historical candles or 250,000 recorded events per job. News APIs and AI generation are never called in a replay. A newly imported historical article cannot appear before its actual observation/assessment availability.

Comparisons include return after modeled costs, drawdown, fees, closed trades and open quantity. Parameter grids are research, not automatic strategy discovery or evidence of profitability. Repeatedly inspecting a holdout and adjusting settings contaminates that holdout. This first implementation provides a chronological holdout, not a rolling walk-forward optimizer or significance-adjusted search.

## 3. Paper trade on live feeds

Select **Use for paper** on a research run to carry its exact configuration into a forward experiment, or start an unlinked experiment using the current settings. Active configurations cannot be edited. A new experiment starts after the latest recorded event and warms up from new, consecutive candles.

Three independent virtual portfolios receive identical subsequent data and costs:

- **Trend:** enter when the fast SMA is above the slow SMA; exit on the reverse condition.
- **RSI:** enter at/below the lower threshold; exit at/above the upper threshold. RSI uses the simple trailing gains/losses ratio, not Wilder smoothing.
- **AI-assisted trend:** the trend must be positive, with fresh supportive news, acceptable funding, book imbalance and executed flow. Large recent short liquidations defer squeeze chasing. Estimated heatmap filtering is opt-in through `require_heatmap`.

AI is currently a **news interpretation layer feeding explicit entry rules**. There is no trained numerical price prediction model, autonomous code rewriting, arbitrary strategy code execution, reinforcement learner or profitability guarantee.

Every closed-bar evaluation stores the action, human-readable reason, inputs and event ID. Risk sizing limits the allocation by both available cash and estimated stop loss plus costs. Daily loss and peak drawdown limits prevent new entries and request exits. **Pause entries** preserves risk exits. **Flatten & stop** exits on the next available book and stops only when flat; this is a request, not an instantaneous guaranteed fill. A delayed worker never retrospectively opens paper positions at old quotes. The database atomically checkpoints wallets, decisions and fills so restart replay cannot double-charge them.

## 4. Controlled live execution

The code includes a Binance spot REST adapter, a persisted order outbox, partial-fill reconciliation and native OCO take-profit/stop orders. **Live operation defaults off** and testnet defaults on. No deployment, model call or real order is triggered by installing the code or opening the dashboard.

The readiness screen checks:

- At least 30 days of forward paper history, and at least 80% of 30 days' one-minute bars observed with fresh AI context across 30 UTC days.
- A stopped, flat paper experiment with at least 20 closed AI trades.
- Positive paper return after costs that exceeds the paired trend baseline, within the configured drawdown limit.
- A prior matching event replay, ending before paper trading begins, with positive AI holdout return and at least five closed holdout trades.
- Identical evaluated engine, prompt, model and configuration; explicit server enablement, account ownership and capital settings.

These are minimum operating gates, not statistical proof of an edge. New settings or sizing need a new evaluation. The code cannot manufacture forward history to pass them.

After verifying exchange product/API eligibility for your jurisdiction and account, the operator configures a **dedicated spot account** with no existing BTC/ETH positions or orders, sufficient USDT, and withdrawals disabled on the API key. Set `BINANCE_API_KEY`, `BINANCE_API_SECRET`, `LIVE_OWNER_ID` (Django user ID), `LIVE_ACCOUNT_DEDICATED=true`, `LIVE_WITHDRAWALS_DISABLED_CONFIRMED=true`, `LIVE_MAX_CAPITAL`, `LIVE_TRADING_ENABLED=true` and the intended `BINANCE_TESTNET` environment. Production verifies withdrawal permissions through Binance; testnet has no wallet-permission endpoint. Keep exchange keys restricted to appropriate outbound IPs where supported.

Arming is an explicit operator command, never a browser mutation:

```bash
python backend/manage.py arm_live --paper-run PAPER_RUN_UUID --capital EVALUATED_CAPITAL --acknowledge-real-orders
```

The trader worker then processes the armed run. Only one live run may own the account. Capital must exactly match the evaluated paper allocation and stay within the operator cap. Database session locks prevent multiple traders. Testnet/production cannot be changed under an existing order ledger.

Orders use quantity/tick filters and IOC price limits to bound accepted execution prices. The order intent and client ID are committed before submission. Timeouts and ambiguous errors enter reconciliation; even an order-not-found response never triggers blind resubmission. Confirmed fills are applied once by exchange trade ID, including fees paid in base, quote or a third asset. Third-asset fees use an observed conversion rate and are identified in the ledger. The account's base position must match its ledger before another order.

Native OCO protection is submitted after an entry's final fills are reconciled. **There is a short interval between entry and successful protection.** Protection rejection requests flattening; unknown protection status pauses new entries and is queried before another action. Strategy exits cancel/reconcile protection before sizing the remaining spot sell. Partial positions below exchange minimums, outages, unavailable liquidity and price gaps can prevent immediate protection or flattening. A stop is not a guaranteed maximum loss. Unknown exchange state may require an operator to reconcile the ledger using exchange records; the app deliberately will not guess.

Before funding production, validate the complete integration on the target venue/testnet, including fee assets, permissions, OCO support, partial fills, restarts and failure recovery. Automated tests use an injected broker and never place real orders. No live exchange credentials or AI key are supplied in the repository.

## Render deployment

`render.yaml` describes a Frankfurt project with one Django web service, PostgreSQL and separate recorder, trader and research workers. The web pre-deploy command waits for a database connection before running migrations; workers wait for the initial schema. Both waits are bounded to five minutes because a newly provisioned database can report available before connections succeed. Research does not block execution. SIGTERM stops new work and drains/checkpoints where possible. Interrupted research jobs are marked failed on restart rather than silently reused.

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
