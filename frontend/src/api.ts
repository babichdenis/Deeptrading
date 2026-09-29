const API = ["5173", "5174"].includes(window.location.port) ? `http://${window.location.hostname}:8000` : "";

export interface CandleDto {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface AnalysisDto {
  figi: string;
  ticker: string;
  name: string;
  interval: string;
  candles: CandleDto[];
  sma20: (number | null)[];
  ema50: (number | null)[];
  rsi: (number | null)[];
  bb_upper: (number | null)[];
  bb_lower: (number | null)[];
  macd: { macd: (number | null)[]; signal: (number | null)[]; hist: (number | null)[] };
}

export async function fetchAnalysis(
  figi: string,
  intervalName: string,
  limit = 2000,
  beforeTs?: string,
  signal?: AbortSignal,
): Promise<AnalysisDto> {
  const q = `interval_name=${encodeURIComponent(intervalName)}&limit=${limit}` + (beforeTs ? `&before_ts=${encodeURIComponent(beforeTs)}` : "");
  const res = await fetch(`${API}/api/analysis/${figi}?${q}`, signal ? { signal } : undefined);
  if (!res.ok) throw new Error(`Ошибка загрузки анализа (${res.status})`);
  return res.json();
}

export interface SyncReport {
  figi: string;
  interval: string;
  cached_bars: number;
  downloaded: number;
  requests: number;
  coverage_from: string | null;
  coverage_to: string | null;
}

export interface ParamSpec {
  type: "int" | "float";
  default: number;
  min?: number;
  max?: number;
}

export interface StrategyCardDto {
  id: string;
  name: string;
  family: string;
  wave: number;
  status: string;
  timeframes: string[];
  long_rule: string;
  short_rule: string;
  params_schema: Record<string, ParamSpec>;
}

export interface SignalDto {
  ts: string;
  side: "BUY" | "SELL" | string;
  status: string;
  reason: string;
  features: Record<string, number>;
}

export interface ComputeResponse {
  run_id: string;
  cached: boolean;
  replaced_stale: boolean;
  count: number;
  signals: SignalDto[];
}

export interface RunRowDto {
  run_id: string;
  figi: string;
  interval_name: string;
  strategy_id: string;
  engine_version: string;
  params: Record<string, number>;
  bars: number;
  created_at: string | null;
}

export async function syncCandles(
  figi: string,
  intervalName: string,
  days: number,
  rangeFromSec?: number,
  rangeToSec?: number,
): Promise<SyncReport> {
  let url = `${API}/api/candles/${figi}/sync?interval_name=${intervalName}&days=${days}`;
  if (rangeFromSec != null && rangeToSec != null) {
    const iso = (sec: number) => new Date(sec * 1000).toISOString();
    url += `&from_ts=${iso(rangeFromSec)}&to_ts=${iso(rangeToSec)}`;
  }
  const res = await fetch(url, { method: "POST" });
  if (!res.ok) throw new Error(`Ошибка синхронизации (${res.status})`);
  return res.json();
}

export async function fetchCatalog(): Promise<{ count: number; strategies: StrategyCardDto[] }> {
  const res = await fetch(`${API}/api/v1/strategies/catalog`);
  if (!res.ok) throw new Error(`Каталог недоступен (${res.status})`);
  return res.json();
}

export async function computeSignals(
  figi: string,
  intervalName: string,
  strategyId: string,
  params: Record<string, number>,
  rangeFromSec?: number,
  rangeToSec?: number,
): Promise<ComputeResponse> {
  const body: Record<string, unknown> = { figi, interval_name: intervalName, strategy_id: strategyId, params };
  if (rangeFromSec != null && rangeToSec != null) {
    const iso = (sec: number) => new Date(sec * 1000).toISOString();
    body.from_ts = iso(rangeFromSec);
    body.to_ts = iso(rangeToSec);
  }
  const res = await fetch(`${API}/api/v1/signals/compute`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`compute ${strategyId}: ${text || res.status}`);
  }
  return res.json();
}

export async function fetchRuns(figi: string): Promise<RunRowDto[]> {
  const res = await fetch(`${API}/api/v1/signals/runs?figi=${figi}`);
  if (!res.ok) throw new Error(`runs ${res.status}`);
  const data = await res.json();
  return data.runs;
}

export interface QuorumResponse extends ComputeResponse {
  run: unknown;
}

export async function computeQuorum(
  memberRunIds: string[],
  k: number,
): Promise<QuorumResponse> {
  const res = await fetch(`${API}/api/v1/quorum/compute`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ member_run_ids: memberRunIds, k }),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`quorum: ${text || res.status}`);
  }
  return res.json();
}

export interface PolicySpec {
  id: string;
  label: string;
  params_schema: Record<string, ParamSpec>;
}

export async function fetchPoliciesCatalog(): Promise<{
  exit_policies: PolicySpec[];
  signal_policies: PolicySpec[];
}> {
  const res = await fetch(`${API}/api/v1/policies/catalog`);
  if (!res.ok) throw new Error(`policies ${res.status}`);
  return res.json();
}

export interface ExperimentResult {
  experiment_id: string;
  status: string;
  figi: string;
  ticker?: string;
  interval_name: string;
  strategy_id: string;
  exit_policy: { id: string; params: Record<string, number> };
  qty: number;
  research_status: string | null;
  error: string | null;
  summary: {
    summary: {
      trades: number; net: number; profit_factor: number; win_rate: number;
      expectancy: number; long_count: number; short_count: number;
    };
    max_drawdown_pct: number;
    verdict: { trading_verdict: string; stable_halves: boolean };
    halves: { h1: { net: number }; h2: { net: number } };
    signals_total: number;
    final_equity: number;
  };
  trades: Array<Record<string, unknown>>;
}

export async function runExperiment(body: Record<string, unknown>): Promise<ExperimentResult> {
  const res = await fetch(`${API}/api/v1/experiments`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await res.text() || `experiment ${res.status}`);
  return res.json();
}

export interface SweepResult {
  trials_done: number;
  elapsed_sec: number;
  plateau_detected: boolean;
  ranked: Array<{ params: Record<string, number>; net: number | null; pf: number | null; trades: number | null; max_dd_pct?: number }>;
  best: Record<string, unknown> | null;
}

export async function runSweep(body: Record<string, unknown>): Promise<SweepResult> {
  const res = await fetch(`${API}/api/v1/lab/sweep`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await res.text() || `sweep ${res.status}`);
  return res.json();
}

export interface BatchResult {
  summary: { stocks_done: number; stocks_total: number; positive_stocks: number; total_net: number; avg_net_per_stock: number; elapsed_sec: number };
  per_stock: Array<{ figi: string; ticker: string; net: number | null; pf: number | null; trades: number | null; research_status: string | null }>;
}

export async function runBatch(body: Record<string, unknown>): Promise<BatchResult> {
  const res = await fetch(`${API}/api/v1/lab/batch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await res.text() || `batch ${res.status}`);
  return res.json();
}

export interface ExperimentListRow {
  experiment_id: string;
  figi: string;
  interval_name: string;
  strategy_id: string;
  research_status: string | null;
  summary_net: number | null;
  summary_pf: number | null;
  summary_trades: number | null;
  created_at: string | null;
}

export async function fetchExperiments(limit = 30): Promise<ExperimentListRow[]> {
  const res = await fetch(`${API}/api/v1/experiments?limit=${limit}`);
  if (!res.ok) throw new Error(`experiments ${res.status}`);
  const d = await res.json();
  return d.experiments;
}

export interface PortfolioDigest {
  cash: number;
  initial_cash: number;
  equity: number;
  market_value: number;
  pnl: number;
  positions_open: number;
  own_in_positions?: number;
  positions_value?: number;
  tinkoff_currencies?: number;
  tinkoff_shares?: number;
  trades?: { total: number; wins: number; winrate: number };
  reconcile?: {
    cash_calc: number; cash_tinkoff: number; delta_cash: number;
    accounting_pnl: number; tinkoff_pnl: number; delta_pnl: number;
    closed_net: number; unrealized: number; ok: boolean;
  };
}

export interface BotStatus {
  running: boolean;
  mode: string;
  started_at: string | null;
  error: string | null;
  candles_seen: number;
  signals_seen: number;
  pending_orders?: number;
  universe: Array<{ figi: string; ticker: string; atr_pct: number }>;
  portfolio: PortfolioDigest;
  session?: string;
  data?: { health: string; source: string; last_candle_ts: string | null };
  risk?: { state: string; daily_pnl: number; daily_loss_limit: number; entries_paused: boolean };
  carousel?: CarouselStatus;
}

export async function botStatus(): Promise<BotStatus> {
  const res = await fetch(`${API}/api/v1/bot/status`);
  if (!res.ok) throw new Error(`bot ${res.status}`);
  return res.json();
}

export async function botStart(body: Record<string, unknown>): Promise<unknown> {
  const res = await fetch(`${API}/api/v1/bot/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await res.text() || `bot start ${res.status}`);
  return res.json();
}

export async function botStop(): Promise<void> {
  const res = await fetch(`${API}/api/v1/bot/stop`, { method: "POST" });
  if (!res.ok) throw new Error(`bot stop ${res.status}`);
}

export interface TestOpts {
  test_name?: string;
  replay_start?: string;
  replay_end?: string;
}

export async function botSetMode(mode: "sandbox" | "live" | "test", opts: TestOpts = {}): Promise<{ mode: string; test_name?: string | null }> {
  const res = await fetch(`${API}/api/v1/bot/mode`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode, ...opts }),
  });
  if (!res.ok) throw new Error(`bot mode ${res.status}`);
  return res.json();
}

export interface TestRunRow {
  name: string;
  replay_start: string;
  replay_end: string;
  created_at: string;
  trades: number;
  wins: number;
  losses: number;
  gross_win: number;
  gross_loss: number;
  net: number;
  pf: number;
  winrate: number;
  positions_open: number;
}

export async function fetchTests(): Promise<TestRunRow[]> {
  const res = await fetch(`${API}/api/v1/bot/tests`);
  if (!res.ok) throw new Error(`tests ${res.status}`);
  const d = await res.json();
  return (d.tests ?? []) as TestRunRow[];
}

export async function deleteTest(name: string): Promise<{ deleted: number }> {
  const res = await fetch(`${API}/api/v1/bot/tests/${encodeURIComponent(name)}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`delete test ${res.status}`);
  return res.json();
}

export async function fetchTestTrades(name: string): Promise<BotTradeRow[]> {
  const res = await fetch(`${API}/api/v1/bot/tests/${encodeURIComponent(name)}`);
  if (!res.ok) throw new Error(`test trades ${res.status}`);
  const d = await res.json();
  return (d.trades ?? []) as BotTradeRow[];
}

export async function botReset(cash: number): Promise<void> {
  const res = await fetch(`${API}/api/v1/bot/reset?initial_cash=${cash}`, { method: "POST" });
  if (!res.ok) throw new Error(`bot reset ${res.status}`);
}

export interface BotPositionRow {
  figi: string; ticker: string; side: string; qty: number;
  entry_price: number; entry_time: string;
  stop_loss: number | null; take_profit: number | null;
}

export async function botPositions(): Promise<BotPositionRow[]> {
  const res = await fetch(`${API}/api/v1/bot/positions`);
  if (!res.ok) throw new Error(`positions ${res.status}`);
  const d = await res.json();
  return d.positions;
}

export interface BotTradeRow {
  figi?: string; ticker: string; side: string; qty: number;
  entry_price: number; exit_price: number;
  entry_time: string; exit_time: string;
  ts?: string;
  price?: number;
  net_pnl: number; commission: number; exit_reason: string;
  regime?: string | null;
  stop_loss?: number | null; take_profit?: number | null;
  exit_meta?: string | null;
  meta?: string | null;
  leverage?: number | null; notional?: number | null; own_money?: number | null;
  max_pnl?: number | null; max_pnl_time?: string | null;
  max_pnl_price?: number | null; max_pnl_atr_pct?: number | null;
  max_pnl_r?: number | null; max_pnl_roi_pct?: number | null;
  max_pnl_atr?: number | null;
  max_pnl_mae_atr?: number | null;
  trail_info?: TrailInfo | null;
}

export interface TrailInfo {
  activated?: boolean;
  trail_stop?: number | null;
  trail_dist_atr?: number | null;
  hit_time?: string | null;
  hit_price?: number | null;
  hit_reason?: string | null;
  hit_pnl?: number | null;
  hit_r?: number | null;
  hit_roi_pct?: number | null;
}

export async function botTrades(limit = 50): Promise<BotTradeRow[]> {
  const res = await fetch(`${API}/api/v1/bot/trades?limit=${limit}`);
  if (!res.ok) throw new Error(`trades ${res.status}`);
  const d = await res.json();
  return d.trades;
}

export interface BotOrderRow {
  id: string;
  figi: string;
  ticker: string;
  action: string;
  side: string;
  qty: number;
  status: string;
  created_at: string;
  filled_at: string | null;
  price: number | null;
}

export async function botOrders(limit = 50): Promise<BotOrderRow[]> {
  const res = await fetch(`${API}/api/v1/bot/orders?limit=${limit}`);
  if (!res.ok) throw new Error(`orders ${res.status}`);
  const d = await res.json();
  return d.orders;
}

export interface BotEventRow {
  ts: string;
  type: string;
  figi: string | null;
  ticker: string | null;
  reason: string | null;
  payload: Record<string, unknown> | null;
}

export async function botEvents(limit = 100): Promise<BotEventRow[]> {
  const res = await fetch(`${API}/api/v1/bot/events?limit=${limit}`);
  if (!res.ok) throw new Error(`events ${res.status}`);
  const d = await res.json();
  return d.events;
}

export async function botPause(paused: boolean): Promise<{ entries_paused: boolean }> {
  const res = await fetch(`${API}/api/v1/bot/pause`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paused }),
  });
  if (!res.ok) throw new Error(`pause ${res.status}`);
  return res.json();
}

export async function botCancelPending(): Promise<{ cancelled: number }> {
  const res = await fetch(`${API}/api/v1/bot/orders/cancel-pending`, { method: "POST" });
  if (!res.ok) throw new Error(`cancel ${res.status}`);
  return res.json();
}

export async function botCloseAll(): Promise<{ closed: number }> {
  const res = await fetch(`${API}/api/v1/bot/positions/close-all`, { method: "POST" });
  if (!res.ok) throw new Error(`close-all ${res.status}`);
  return res.json();
}

// ============ Ensemble config (UI-управление составом кворума) ============

export interface EnsembleSetup {
  strategy_id: string;
  enabled: boolean;
  tf: string;
  params: Record<string, number>;
}

export interface EnsembleConfig {
  quorum: number;
  neutral_mode: string;
  vol_thr?: number;
  setups: EnsembleSetup[];
  regime_setups_filter?: Record<string, string[]>;
  bias?: { tf: string; period: number };
  bias_mode?: string;
  entry_tf?: string;
  _all_strategies?: string[];
}

export async function botEnsembleConfig(): Promise<EnsembleConfig> {
  const res = await fetch(`${API}/api/v1/bot/ensemble`);
  if (!res.ok) throw new Error(`ensemble ${res.status}`);
  return res.json();
}

export async function botEnsemblePatch(payload: Partial<EnsembleConfig>): Promise<{ ok: boolean; applied_strategies: number; config: EnsembleConfig }> {
  const res = await fetch(`${API}/api/v1/bot/ensemble`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`ensemble patch ${res.status}`);
  return res.json();
}

export async function botEnsembleReset(): Promise<{ ok: boolean; config: EnsembleConfig }> {
  const res = await fetch(`${API}/api/v1/bot/ensemble/reset`, { method: "POST" });
  if (!res.ok) throw new Error(`ensemble reset ${res.status}`);
  return res.json();
}

export async function botAiControlPut(payload: { mode?: string }): Promise<{ ok: boolean; updated_ts?: string }> {
  const res = await fetch(`${API}/api/v1/bot/ai_control`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`ai_control ${res.status}`);
  return res.json();
}

// ============ Гейты входа / воронка сигналов (аудит конвейера бота) ============

export interface GateItem {
  key: string;
  title: string;
  category: string;
  stage: string;
  config: string;
  scope: string;
  desc: string;
  enabled: boolean | null;
  logical_enabled: boolean;
  toggleable: boolean;
  flag_value: unknown;
  rejects: number;
}

export interface GatesByStage {
  gates: number;
  enabled: number;
  rejects: number;
}

export interface GatesReport {
  count: number;
  categories: Record<string, GateItem[]>;
  by_stage: Record<string, GatesByStage>;
  skip_counts: Record<string, number>;
  order: string[];
}

export async function botGates(): Promise<GatesReport> {
  const res = await fetch(`${API}/api/v1/bot/gates`);
  if (!res.ok) throw new Error(`gates ${res.status}`);
  return res.json();
}

export async function botGatesToggle(key: string, on: boolean): Promise<GatesReport> {
  const res = await fetch(`${API}/api/v1/bot/gates/toggle`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key, on }),
  });
  if (!res.ok) throw new Error(`gates toggle ${res.status}`);
  const d = await res.json();
  if (!d || d.ok === false) throw new Error(d?.error ?? `gates toggle ${key}`);
  return d.gates as GatesReport;
}

export interface FunnelEntry {
  ts: string;
  stage: string;
  action: string;
  figi: string;
  ticker: string;
  reason: string;
  detail: string;
  n: number;
}

export interface FunnelReport {
  stats: Record<string, number>;
  ring: FunnelEntry[];
  total: number;
  ring_size: number;
}

export async function botFunnel(figi?: string, ticker?: string, limit = 120): Promise<FunnelReport> {
  const p = new URLSearchParams();
  if (figi) p.set("figi", figi);
  if (ticker) p.set("ticker", ticker);
  p.set("limit", String(limit));
  const res = await fetch(`${API}/api/v1/bot/funnel?${p.toString()}`);
  if (!res.ok) throw new Error(`funnel ${res.status}`);
  return res.json();
}

// ============ Статистика прогона теста ============

export interface StatsRow {
  key: string;
  trades: number;
  wins: number;
  losses: number;
  gross_win: number;
  gross_loss: number;
  net: number;
  pf: number | null;
  wr: number;
  avg_win: number;
  avg_loss: number;
}

export interface TestStats {
  test_name: string | null;
  mode: string;
  date_from?: string | null;
  date_to?: string | null;
  overall: StatsRow & { key?: string };
  open_positions: number;
  by_side: StatsRow[];
  by_regime: StatsRow[];
  by_ticker: StatsRow[];
  by_entry_reason: StatsRow[];
  by_exit_reason: StatsRow[];
  by_quorum: StatsRow[];
  by_session: StatsRow[];
  by_strategy: StatsRow[];
  by_bias: StatsRow[];
}

export async function fetchTestStats(
  testName = "",
  dateFrom = "",
  dateTo = "",
  mode = "",
): Promise<TestStats> {
  const p = new URLSearchParams();
  if (testName) p.set("test_name", testName);
  if (dateFrom) p.set("date_from", dateFrom);
  if (dateTo) p.set("date_to", dateTo);
  if (mode) p.set("mode", mode);
  const q = p.toString() ? `?${p.toString()}` : "";
  const res = await fetch(`${API}/api/v1/bot/test_stats${q}`);
  if (!res.ok) throw new Error(`test_stats ${res.status}`);
  return res.json();
}

export interface TestCompareEntry {
  overall: StatsRow;
  by_side: StatsRow[];
  by_regime: StatsRow[];
  by_strategy: StatsRow[];
  by_exit_reason: StatsRow[];
}
export interface TestsCompare {
  tests: Record<string, TestCompareEntry>;
}

export async function fetchTestsCompare(names: string[]): Promise<TestsCompare> {
  const q = names.length ? `?names=${encodeURIComponent(names.join(","))}` : "";
  const res = await fetch(`${API}/api/v1/bot/tests_compare${q}`);
  if (!res.ok) throw new Error(`tests_compare ${res.status}`);
  return res.json();
}

// ============ Sandbox API (T-Invest live data) ============

export interface SandboxStatus {
  running: boolean;
  mode: string;
  error?: string;
  portfolio: PortfolioDigest;
}

export async function sandboxStatus(): Promise<SandboxStatus> {
  const res = await fetch(`${API}/api/v1/sandbox/status`);
  if (!res.ok) throw new Error(`sandbox ${res.status}`);
  return res.json();
}

export interface SandboxPositionRow {
  figi: string; ticker: string; side: string; qty: number;
  entry_price: number; current_price: number; prev_close: number | null;
  unrealized_pnl: number; roi_pct: number; sell_value: number;
  entry_time: string; stop_loss: number | null; take_profit: number | null;
  trail_active?: boolean;
  strategy_id: string;
  leverage: number; own_money: number; leveraged: number;
  notional?: number; trade_leverage?: number; risk_rate?: number | null;
  regime: string;
  regime_reason: string;
  regime_atr_pct: number | null;
  regime_adx: number | null;
  vol: number | null;
  atr?: number | null;
  dist_sl_atr?: number | null;
  dist_tp_atr?: number | null;
  net_pnl_est?: number | null;
}

export async function sandboxPositions(): Promise<SandboxPositionRow[]> {
  const res = await fetch(`${API}/api/v1/sandbox/positions`);
  if (!res.ok) throw new Error(`sandbox positions ${res.status}`);
  const d = await res.json();
  return d.positions;
}

export async function sandboxTrades(limit = 50): Promise<BotTradeRow[]> {
  const res = await fetch(`${API}/api/v1/sandbox/trades?limit=${limit}`);
  if (!res.ok) throw new Error(`sandbox trades ${res.status}`);
  const d = await res.json();
  return d.trades;
}

export async function sandboxOrders(limit = 30): Promise<BotOrderRow[]> {
  const res = await fetch(`${API}/api/v1/sandbox/orders?limit=${limit}`);
  if (!res.ok) throw new Error(`sandbox orders ${res.status}`);
  const d = await res.json();
  return d.orders;
}

export interface ScreenerRow {
  figi: string;
  ticker: string;
  name: string;
  lot: number;
  price: number | null;
  turnover: number | null;
  rng_pct: number | null;
  in_universe: boolean;
}

export async function fetchScreener(): Promise<{ items: ScreenerRow[]; carousel: CarouselStatus }> {
  const res = await fetch(`${API}/api/v1/screener`);
  if (!res.ok) throw new Error(`screener ${res.status}`);
  const d = await res.json();
  return { items: d.items, carousel: d.carousel };
}

export async function screenerAddEligible(ticker: string): Promise<{ ok: boolean; figi?: string; error?: string }> {
  const res = await fetch(`${API}/api/v1/screener/eligible`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ticker }),
  });
  if (!res.ok) throw new Error(`eligible add ${res.status}`);
  return res.json();
}

export async function screenerRemoveEligible(ticker: string): Promise<{ ok: boolean }> {
  const res = await fetch(`${API}/api/v1/screener/eligible/${encodeURIComponent(ticker)}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`eligible remove ${res.status}`);
  return res.json();
}

export interface CarouselInstrument {
  figi: string;
  ticker: string;
  candle_count: number;
  active: boolean;
}

export interface CarouselPendingItem {
  ticker: string;
  candle_count: number;
  need_download: boolean;
}

export interface CarouselLogEntry {
  ts: string;
  action: string;
  ticker: string;
  msg: string;
}

export interface CarouselStatus {
  bot_running: boolean;
  eligible_count: number;
  active_count: number;
  insufficient: number;
  pending: CarouselPendingItem[];
  log: CarouselLogEntry[];
}

export interface ConfigurationDto {
  configuration_id: string;
  name: string;
  status: string;
  interval_name: string;
  members: Array<{ strategy_id: string; params: Record<string, number> }>;
  quorum: number;
  exit_policy: { id: string; params: Record<string, number> };
  allow_short: boolean;
  min_hold_bars: number;
  preview_figi: string | null;
  preview_net?: number | null;
  preview_trades?: number | null;
  lab_totals?: { stocks: number; trades: number; positive_stocks: number; total_net: number } | null;
  research_status?: string | null;
  session_policy?: Record<string, unknown>;
  source_runs?: Record<string, string>;
  filters?: unknown[];
  created_at?: string | null;
  lab_result?: {
    totals?: { stocks: number; trades: number; positive_stocks: number; total_net: number; wins: number; losses: number };
    per_stock?: Array<{ ticker: string; mode: string; active_days: number; wl: string; trades: number; pnl: { total: number; pos: number; neg: number } }>;
    trades?: Array<Record<string, unknown>>;
    progress?: { done: number; total: number; current?: string };
    status?: string;
    error?: string;
    elapsed_sec?: number;
    funnel?: Record<string, unknown>;
    config_snapshot?: {
      name?: string; interval_name?: string; members?: unknown[];
      quorum?: number; exit_policy?: unknown; min_hold_bars?: number;
      allow_short?: boolean; session_policy?: unknown; engine_version?: string;
    };
    test_params?: { tickers?: string[]; periodDays?: number; date_from?: string | null; date_to?: string | null };
    figi?: string;
    days?: number;
    summary?: Record<string, any>;
    universe?: string[];
    trades_preview?: Array<Record<string, unknown>>;
    curve?: Array<{ time: string; equity: number }>;
    by_day?: Array<{ date: string; trades: number; gross: number; commission: number; net: number }>;
    consecutive_losses?: number;
    top1_analysis?: { top1_figi: string | null; top1_net: number; net_without_top1: number; total_net: number; concentration_pct: number };
  };
}

export async function fetchConfigurations(): Promise<ConfigurationDto[]> {
  const res = await fetch(`${API}/api/v1/warehouse/configurations`);
  if (!res.ok) throw new Error(`configs ${res.status}`);
  const d = await res.json();
  return d.configurations;
}

export async function createConfiguration(body: Record<string, unknown>): Promise<ConfigurationDto> {
  const res = await fetch(`${API}/api/v1/warehouse/configurations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await res.text() || `config create ${res.status}`);
  return res.json();
}

export interface PreviewResponse {
  configuration_id: string;
  figi: string;
  interval_name: string;
  funnel: Record<string, { BUY?: number; SELL?: number } | number>;
  summary: Record<string, unknown> & { trades_preview?: Array<Record<string, unknown>> };
  merged_signals_count: number;
}

export async function previewConfiguration(
  configId: string,
  figi: string,
  days = 240
): Promise<PreviewResponse> {
  const res = await fetch(`${API}/api/v1/warehouse/configurations/${configId}/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ figi, days }),
  });
  if (!res.ok) throw new Error(await res.text() || `preview ${res.status}`);
  return res.json();
}

export interface LabRunResult {
  configuration_id: string;
  name: string;
  status: string;
  research_status: string;
  totals: { stocks: number; trades: number; positive_stocks: number; total_net: number; wins: number; losses: number };
  per_stock: Array<{ ticker: string; mode: string; active_days: number; wl: string; trades: number; pnl: { total: number; pos: number; neg: number } }>;
  by_ticker: Record<string, unknown>;
  trades: Array<Record<string, unknown>>;
  errors: Array<{ ticker: string; error: string }>;
}

export async function sendToLab(
  configId: string,
  periodDays = 30,
  topN = 10,
): Promise<LabRunResult> {
  const res = await fetch(`${API}/api/v1/warehouse/configurations/${configId}/send-to-lab`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ period_days: periodDays, top_n: topN }),
  });
  if (!res.ok) throw new Error(await res.text() || `send-to-lab ${res.status}`);
  return res.json();
}

export async function getConfiguration(configId: string): Promise<ConfigurationDto> {
  const res = await fetch(`${API}/api/v1/warehouse/configurations/${configId}`);
  if (!res.ok) throw new Error(`config ${res.status}`);
  return res.json();
}

export async function sendToLabAsync(
  configId: string,
  opts: { periodDays?: number; topN?: number; tickers?: string[]; dateFrom?: string; dateTo?: string },
): Promise<void> {
  const body: Record<string, unknown> = { period_days: opts.periodDays ?? 30, top_n: opts.topN ?? 10 };
  if (opts.tickers?.length) body.tickers = opts.tickers;
  if (opts.dateFrom) body.date_from = opts.dateFrom;
  if (opts.dateTo) body.dateTo = undefined;
  if (opts.dateTo) { delete body.dateTo; body.date_to = opts.dateTo; }
  const res = await fetch(`${API}/api/v1/warehouse/configurations/${configId}/send-to-lab-async`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await res.text() || `send-to-lab ${res.status}`);
}

export async function updateConfiguration(configId: string, body: Record<string, unknown>): Promise<ConfigurationDto> {
  const res = await fetch(`${API}/api/v1/warehouse/configurations/${configId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await res.text() || `update ${res.status}`);
  return res.json();
}

export async function deleteConfiguration(configId: string): Promise<void> {
  const res = await fetch(`${API}/api/v1/warehouse/configurations/${configId}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`delete ${res.status}`);
}

export interface TestRunDto {
  run_id: string;
  config_id: string;
  config_name: string;
  members: Array<{ strategy_id: string; params: Record<string, number> }>;
  quorum: number;
  min_hold_bars?: number;
  allow_short?: boolean;
  exit_policy: { id: string; params: Record<string, number> };
  interval_name: string;
  status: string;
  tickers: string[];
  date_from: string | null;
  date_to: string | null;
  period_days: number;
  progress: { done: number; total: number; current?: string };
  error: string | null;
  lab_result: Record<string, any> | null;
  created_at: string | null;
}

export interface LabQueueResponse {
  max_concurrent: number;
  count: number;
  runs: TestRunDto[];
}

export async function fetchLabQueue(): Promise<LabQueueResponse> {
  const res = await fetch(`${API}/api/v1/lab/queue`);
  if (!res.ok) throw new Error(`queue ${res.status}`);
  return res.json();
}

export async function enqueueTest(
  configId: string,
  tickers: string[],
  dateFrom: string | null,
  dateTo: string | null,
  periodDays: number,
): Promise<void> {
  const res = await fetch(`${API}/api/v1/lab/queue`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ config_id: configId, tickers, date_from: dateFrom, date_to: dateTo, period_days: periodDays }),
  });
  if (!res.ok) throw new Error(await res.text() || `enqueue ${res.status}`);
}

export async function pauseRun(runId: string): Promise<void> {
  await fetch(`${API}/api/v1/lab/queue/${runId}/pause`, { method: "POST" });
}
export async function resumeRun(runId: string): Promise<void> {
  await fetch(`${API}/api/v1/lab/queue/${runId}/resume`, { method: "POST" });
}
export async function stopRun(runId: string): Promise<void> {
  await fetch(`${API}/api/v1/lab/queue/${runId}/stop`, { method: "POST" });
}
export async function deleteRun(runId: string): Promise<void> {
  await fetch(`${API}/api/v1/lab/runs/${runId}`, { method: "DELETE" });
}
export async function recycleRun(runId: string): Promise<void> {
  await fetch(`${API}/api/v1/lab/runs/${runId}/recycle`, { method: "POST" });
}
export interface LabSettings {
  max_concurrent_tests: number;
  commission_rate: number;
  slippage_bps: number;
}

export async function fetchLabSettings(): Promise<LabSettings> {
  const res = await fetch(`${API}/api/v1/lab/settings`);
  if (!res.ok) throw new Error(`settings ${res.status}`);
  return res.json();
}
export async function saveLabSettings(maxConcurrent: number, commissionRate?: number, slippageBps?: number): Promise<LabSettings> {
  const body: Record<string, unknown> = { max_concurrent_tests: maxConcurrent };
  if (commissionRate != null) body.commission_rate = commissionRate;
  if (slippageBps != null) body.slippage_bps = slippageBps;
  const res = await fetch(`${API}/api/v1/lab/settings`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`settings ${res.status}`);
  return res.json();
}

export interface TestPredictRule {
  name: string;
  signals: number;
  precision: number;
  coverage: number;
  base: number;
}

export interface TestMaxProfitResponse {
  ticker: string;
  name: string;
  figi: string;
  interval: string;
  bars: number;
  lot: number;
  from: string;
  to: string;
  params: {
    threshold_pct: number;
    fee_rate_pct: number;
    capital: number;
    slippage_tick: number;
  };
  candles: CandleDto[];
  ceiling_1lot: { pnl: number; bars: number; profitable_bars: number };
  perfect_intraday: { pnl: number; trades: number };
  perfect_multiday: { pnl: number; trades: number };
  buy_hold: { pnl: number; pct: number; lots: number };
  trades: Array<{
    entry_ts: string;
    exit_ts: string;
    entry_px: number;
    exit_px: number;
    pnl: number;
    lots: number;
    open?: boolean;
  }>;
  equity: Array<{ ts: string; equity: number }>;
  predictability: {
    swings: number;
    lows: number;
    highs: number;
    confirm_lag_min_median: number;
    confirm_lag_min_mean: number;
    captured_median_pct: number;
    rules: TestPredictRule[];
    walkforward: {
      entry: { precision: number | null; coverage: number | null; cut?: number; base: number };
      exit: { precision: number | null; coverage: number | null; cut?: number; base: number };
    };
  };
}

export async function fetchMaxProfit(body: Record<string, unknown>): Promise<TestMaxProfitResponse> {
  const res = await fetch(`${API}/api/v1/test/max-profit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `test ${res.status}`);
  }
  return res.json();
}

export interface EnsembleResponse {
  meta: {
    engine_version: string;
    request_hash: string;
    figi: string;
    bars: number;
    lot: number;
    capital: number;
    qty_shares: number;
    from: string;
    to: string;
    params: Record<string, unknown>;
  };
  regime: { tf: string; timeline: Array<{ from: string; to: string; state: string; reason: string }> };
  oracle: { swings: number; trades: number; gross: number; net: number; zones: Array<{ from: string; to: string }> };
  static: EnsembleRun;
  adaptive: EnsembleRun | null;
  comparison: {
    static_net: number;
    adaptive_net: number | null;
    static_trades: number;
    adaptive_trades: number | null;
    static_capture: { causal_net_oracle_gross_pct: number };
    adaptive_capture: { causal_net_oracle_gross_pct: number } | null;
  };
}

export interface EnsembleRun {
  label: string;
  funnel: {
    raw_signals: number;
    unique_raw_ts: number;
    quorum_unique: number;
    quorum_BUY: number;
    quorum_SELL: number;
    entries_raw: number;
    accepted_decisions: number;
    unique_entry_episodes: number;
    entries_rejected: number;
    reentry_rejected?: number;
    reentry_rejected_list?: Array<{ side: string; signal_ts: string; bars_since_exit: number | null; cooldown_bars: number | null; reason: string }>;
    preview_trades: number;
  };
  funnel_tf: {
    bias_states: { LONG_ALLOWED: number; SHORT_ALLOWED: number; NEUTRAL: number };
    setups: { candidates: number; quorum_passed: number; BUY: number; SELL: number };
    entries: { candidates: number; accepted: number; rejected: number };
    rejected_by_reason: Record<string, number>;
  };
  why_no_entry: Array<{ ts: string; side: string; code: string; detail: string }>;
  episodes: {
    quorum_points: number;
    entry_decisions: number;
    unique_episodes: number;
    completed: number;
    unresolved: number;
    list: Array<{
      episode_id: string;
      side: string;
      quorum_event_id: string;
      first_ts: string;
      last_ts: string;
      status: string;
      entry_px: number | null;
      exit_px: number | null;
      net: number | null;
    }>;
  };
  quorum_list: Array<{ ts: string; side: string; votes: number; event_id: string }>;
  entries: Array<{ ts: string; side: string; reason: string; quorum_event_id?: string }>;
  rejected: Array<{ ts: string; side: string; reason: string }>;
  trades: Array<{
    side: string; regime: string; entry_ts: string; exit_ts: string;
    entry_px: number; exit_px: number; stop: number | null; target: number | null;
    gross: number; commission: number; net: number; bars_held: number;
    exit_reason: string; mfe_r: number; mae_r: number;
    entry_notional: number; exit_notional: number;
    entry_commission: number; exit_commission: number;
    entry_slippage: number; exit_slippage: number;
    costs: number; costs_pct_of_notional: number; gross_move_pct: number;
  }>;
  economic: {
    trades: number; gross: number; commission: number; slippage: number; costs: number; net: number;
    profit_factor: number; win_rate_pct: number; avg_hold_bars: number; turnover: number;
    gross_per_trade: number; cost_per_trade: number; cost_gross_ratio: number; break_even_move_pct: number;
    avg_gross_move_pct: number; median_gross_move_pct: number; median_costs: number; trades_above_break_even: number;
    break_even_by_trade?: { n: number; mean: number; median: number; p25: number; p75: number };
    break_even_by_regime?: Record<string, { n: number; mean: number; median: number }>;
    break_even_by_side?: Record<string, { n: number; mean: number; median: number }>;
    equity: Array<{ ts: string; equity: number }>;
  };
  capture_ratio: {
    oracle_gross_potential: number; causal_gross: number; causal_costs: number; causal_net: number;
    gross_capture_pct: number; net_capture_pct: number;
  };
  exit_coverage?: Record<string, number>;
  oracle_coverage?: {
    window_minutes: number;
    point_total: number;
    geometric: { raw_seen_before_point: number; accepted: number; executed: number; coverage_accepted_pct: number };
    causal: { raw_seen_before_confirmation: number; accepted: number; executed: number; coverage_accepted_pct: number };
    rejected_by_gate: Record<string, number>;
    points?: Array<Record<string, unknown>>;
  } | null;
  quality: Array<{
    role: string; strategy_id: string; tf: string; side: string; signals: number;
    oracle_points: number; hits: number; coverage_pct: number; precision_pct: number;
    lead_min_median: number | null; false_positives: number; useless: boolean;
  }>;
  useless_strategies: string[];
  per_regime: Record<string, { trades: number; gross: number; net: number; win_rate_pct: number }>;
}

export async function fetchEnsemble(body: Record<string, unknown>): Promise<EnsembleResponse> {
  const res = await fetch(`${API}/api/v1/test/ensemble`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `ensemble ${res.status}`);
  }
  return res.json();
}

export interface SweepCell {
  figi: string;
  ticker: string;
  price: number;
  cooldown: number;
  exit_confirm: number;
  trades: number;
  episodes: number;
  gross: number;
  costs: number;
  net: number;
  pf: number;
  win_rate_pct: number;
  break_even_median: number | null;
  reentry_rejected: number;
  counterfactual: { n: number; mean_net: number | null; median_net: number | null; wins_pct: number | null };
}

export interface SweepResponse {
  cells: SweepCell[];
  filtered_out: Array<{ figi: string; ticker: string; price: number | null; reason: string }>;
  tf_minutes: number;
  cooldown_minutes: Record<string, number>;
  note: string;
}

export async function fetchEnsembleSweep(body: Record<string, unknown>): Promise<SweepResponse> {
  const res = await fetch(`${API}/api/v1/test/ensemble-sweep`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `sweep ${res.status}`);
  }
  return res.json();
}

export type DecisionRow = {
  signal_id: string; ts: string; side: string; reason: string | null;
  decision: string; reason_code: string;
  details: { position_state_before: string; position_state_after: string };
};

export async function fetchDecisions(
  runId: string, minHoldBars = 0,
): Promise<{ decisions: DecisionRow[]; counts: Record<string, number> }> {
  const res = await fetch(`${API}/api/v1/signals/${runId}/decisions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ policy_id: "ignore_same_side", min_hold_bars: minHoldBars }),
  });
  if (!res.ok) throw new Error(`decisions ${res.status}`);
  return res.json();
}

// ============ Очередь ансамблевых прогонов (Lab → Ансамбль) ============

export interface EnsembleLabRun {
  run_id: string;
  status: string;
  params: Record<string, unknown>;
  progress: {
    done: number;
    total: number;
    current: string;
    by_stock: Record<string, { done: number; total: number }>;
  };
  result: {
    total_net: number;
    positive_stocks: number;
    stocks: number;
    by_stock: Array<{
      ticker: string; figi: string; trades?: number; gross?: number; costs?: number;
      net?: number; pf?: number; coverage_pct?: number; error?: string;
    }>;
  } | null;
  error: string | null;
  created_at: string | null;
}

export async function createEnsembleRun(body: Record<string, unknown>): Promise<{ run_id: string; status: string }> {
  const res = await fetch(`${API}/api/v1/lab/ensemble`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await res.text() || `enslab ${res.status}`);
  return res.json();
}

export async function fetchEnsembleRuns(limit = 20): Promise<EnsembleLabRun[]> {
  const res = await fetch(`${API}/api/v1/lab/ensemble?limit=${limit}`);
  if (!res.ok) throw new Error(`enslab list ${res.status}`);
  const d = await res.json();
  return d.runs as EnsembleLabRun[];
}

export async function cancelEnsembleRun(runId: string): Promise<void> {
  await fetch(`${API}/api/v1/lab/ensemble/${runId}/cancel`, { method: "POST" });
}

export async function deleteEnsembleRun(runId: string): Promise<void> {
  await fetch(`${API}/api/v1/lab/ensemble/${runId}`, { method: "DELETE" });
}
