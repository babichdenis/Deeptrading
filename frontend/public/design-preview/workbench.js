"use strict";

const byId = (id) => document.getElementById(id);
const money = (n) => new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (x) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[x]));
const set = (id, value) => { const el = byId(id); if (el) el.textContent = value; };
const tickers = ["SBER","LKOH","ROSN","NVTK","GAZP","TATN","GMKN","PLZL","CHMF","NLMK","MAGN","RUAL","ALRS","AFLT","MTSS","SNGS","SNGSP","VTBR","MOEX","IRAO","HYDR","FEES","PHOR","PIKK","SMLT","MVID","ASTR","WUSH","SFIN","LENT","HHRU"];
const strategies = ["MACD Cross","RSI Reversal","EMA Pullback","Donchian Breakout","Bollinger Reversal","Volume Impulse","Trend Follow"];
const positions = [
  { ticker: "SBER", side: "LONG", qty: 40, entry: 278.8, price: 280.64, own: 11152, sl: 276.4, tp: 285.2, time: "13:42", vol: .8 },
  { ticker: "LKOH", side: "LONG", qty: 2, entry: 6749, price: 6802, own: 13498, sl: 6670, tp: 6907, time: "12:18", vol: 1.2 },
  { ticker: "ROSN", side: "LONG", qty: 24, entry: 520.3, price: 517.77, own: 12487.2, sl: 513.2, tp: 534.5, time: "14:05", vol: 1.1 }
];
const demoRuns = [
  { name: "ДЕМО · main_5m", n: 124, wins: 65, losses: 59, net: 1386, pf: 1.12, wr: 52.4, date: "01.10 → 08.10", preset: "main_5m" },
  { name: "ДЕМО · no_evening", n: 87, wins: 43, losses: 44, net: -425, pf: .91, wr: 49.4, date: "01.10 → 08.10", preset: "no_evening" },
  { name: "ДЕМО · baseline", n: 156, wins: 76, losses: 80, net: -1820, pf: .84, wr: 48.7, date: "01.10 → 08.10", preset: "baseline" }
];
const logs = Array.from({ length: 32 }, (_, i) => ({
  time: `14:${String(35 - i).padStart(2, "0")}:00`,
  category: ["signals","candles","trades","events","tech"][i % 5],
  ticker: tickers[i % tickers.length],
  level: i % 9 === 0 ? "warn" : "info",
  message: ["Кворум 2 / 2. Направление совпадает с контекстом.","Закрыт бар 5м. Partial не участвует в сигнале.","Защитные уровни позиции сохранены.","Вход пропущен: позиция уже открыта.","Контроль демонстрационного потока выполнен."][i % 5]
}));
let currentTicker = "SBER";
let timeframe = "5м";
let page = "bot";
let paused = false;
let running = true;
let toastTimer;
let reportTab = "strategies";
let reportRun = 0;
let marketSort = { key: "turnover", direction: -1 };
const chosenRuns = new Set();

function toast(message) {
  set("mock-toast", `ДЕМО · ${message}`);
  byId("mock-toast").classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => byId("mock-toast").classList.remove("visible"), 4200);
}

function table(headers, rows, cls = "stats-table") {
  return `<div class="mock-report-table"><table class="${cls}"><thead><tr>${headers.map((h) => `<th scope="col">${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${row.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

function dialog(title, content) {
  set("mock-detail-title", title);
  byId("mock-detail-body").innerHTML = content;
  byId("mock-detail").showModal();
}

function showPage(next) {
  page = next;
  document.querySelectorAll(".page").forEach((el) => el.classList.toggle("active", el.id === `page-${next}`));
  document.querySelectorAll("[data-page]").forEach((el) => { el.classList.toggle("active", el.dataset.page === next); el.setAttribute("aria-pressed", String(el.dataset.page === next)); });
  document.querySelectorAll("[data-only-page]").forEach((el) => { el.style.display = next === "bot" ? "" : "none"; });
  byId("app").dataset.activePage = next;
  document.body.classList.remove("table-focus");
  drawCharts();
}

function chartSvg() {
  const p = positions.find((x) => x.ticker === currentTicker);
  const price = p?.price ?? 250 + tickers.indexOf(currentTicker) * 19;
  const scale = price * .009;
  const points = Array.from({ length: 72 }, (_, i) => price - scale * .65 + scale * i / 110 + Math.sin(i * .4) * scale * .12 + Math.cos(i * 1.3) * scale * .025);
  points[71] = price;
  const lo = Math.min(...points) - scale * .13;
  const hi = Math.max(...points) + scale * .13;
  const y = (v) => 215 - (v - lo) / (hi - lo) * 190;
  let s = '';
  for (let i = 0; i < 5; i++) {
    const level = hi - (hi - lo) * i / 4;
    s += `<path d="M12 ${y(level)}H905" stroke="#ffffff0b"/><text x="916" y="${y(level) + 4}" fill="#bec2c9" font-size="12" font-family="monospace">${money(level)}</text>`;
  }
  let ema = points[0], line = '';
  points.forEach((close, i) => {
    const open = points[Math.max(0, i - 1)];
    const x = 18 + i * 12.2;
    const color = close >= open ? "#89c8aa" : "#cf9198";
    const top = Math.min(y(open), y(close));
    s += `<path d="M${x} ${top - 5}V${Math.max(y(open), y(close)) + 5}" stroke="${color}"/><rect x="${x - 3.2}" y="${top}" width="6.4" height="${Math.max(1.5, Math.abs(y(open) - y(close)))}" fill="${color}"/><rect x="${x - 3.2}" y="${245 - Math.abs(Math.sin(i * 1.5)) * 20}" width="6.4" height="${Math.abs(Math.sin(i * 1.5)) * 20}" fill="${color}" opacity=".3"/>`;
    ema += (close - ema) * .1;
    line += `${i ? "L" : "M"}${x},${y(ema)} `;
  });
  s += `<path d="${line}" fill="none" stroke="#d7bf8e" stroke-width="1.5"/><path d="M12 ${y(price)}H905" stroke="#89c8aa" stroke-dasharray="4 4"/><rect x="909" y="${y(price) - 10}" width="88" height="21" fill="#345042" rx="3"/><text x="916" y="${y(price) + 5}" fill="#d9efdf" font-size="12" font-family="monospace">${money(price)}</text>`;
  ["10:00","11:00","12:00","13:00","14:00"].forEach((t, i) => { s += `<text x="${18 + i * 215}" y="265" fill="#bec2c9" font-size="12" font-family="monospace">${t}</text>`; });
  return `<svg viewBox="0 0 1000 278" preserveAspectRatio="none" role="img" aria-label="Синтетический график ${currentTicker}; ${timeframe}; не рыночные данные">${s}</svg>`;
}

function drawCharts() {
  const host = byId("bot-chart-frame");
  host.innerHTML = `<div class="mock-chart-toolbar"><strong>${currentTicker}</strong><span class="pos">${money(positions.find((p) => p.ticker === currentTicker)?.price ?? 250 + tickers.indexOf(currentTicker) * 19)} ₽</span>${["1м","5м","10м","15м","30м","1ч","1д"].map((t) => `<button data-mock-tf="${t}" aria-pressed="${timeframe === t}">${t}</button>`).join("")}<span class="chart-context">Синтетические свечи · MSK</span></div>${chartSvg()}`;
  byId("embedded-chart").classList.remove("hidden");
  byId("embedded-chart").innerHTML = chartSvg();
  set("symbol-ticker", currentTicker);
  set("symbol-name", "Демонстрационный инструмент");
  set("ohlc-legend", "Синтетический ряд · индикаторы и маркеры — пример вида");
}

function focusTicker(ticker) {
  currentTicker = ticker;
  drawCharts();
  document.querySelectorAll("tr[data-ticker]").forEach((el) => el.classList.toggle("mock-row-selected", el.dataset.ticker === ticker));
}

function renderPositions() {
  document.querySelector("#bot-positions-table tbody").innerHTML = positions.map((p) => {
    const net = (p.price - p.entry) * p.qty;
    return `<tr tabindex="0" data-ticker="${p.ticker}"><td>15.10 ${p.time}</td><td><b>${p.ticker}</b></td><td class="pos">${p.side}</td><td>${p.qty}</td><td>${money(p.entry)}</td><td>${money(p.entry * p.qty)} ₽<small>свои ${money(p.own)} ₽</small></td><td>${money(p.price)}</td><td class="${net >= 0 ? 'pos' : 'neg'}">${net >= 0 ? '+' : ''}${money(net)}</td><td>${(net / p.own * 100).toFixed(2)}%</td><td>1,0×</td><td>TREND_UP / RANGE</td><td>${p.vol}%</td><td>${money(p.sl)} · trail</td><td>${money(p.tp)}</td><td><button class="btn-sm" data-mock-close="${p.ticker}">Продать · демо</button></td></tr>`;
  }).join("");
}

function tradeDetail(ticker) {
  return `<div class="mock-detail-grid"><div><h4>Вход · ${ticker}</h4><p>RSI 1м 34,5 ↑ · RSI 5м 41,2 ↑ · MACD 10м растёт</p><p>Кворум 2 / 2 · bias 30м UP · TREND_UP</p><p>Объём 1,8× · ATR 1,25 · SL 2 ATR · TP 3 ATR</p></div><div><h4>Выход · пример</h4><p>Причина: take_profit · R +1,42</p><p>MAE −0,4% · MFE +1,8% · удержание 43 минуты</p><p>Gross +124 ₽ · комиссия 12 ₽ · модельный net +112 ₽</p></div></div><h4>Тепловая карта дня · демонстрация</h4>${heatmap([ticker])}`;
}

function renderTrades() {
  document.querySelector("#bot-trades-table tbody").innerHTML = Array.from({ length: 7 }, (_, i) => {
    const ticker = tickers[i];
    const net = i % 3 ? 112 + i * 17 : -53 - i * 4;
    return `<tr tabindex="0" data-trade-detail="${ticker}" data-ticker="${ticker}"><td>15.10 10:${20 + i} → 11:${10 + i}</td><td>${ticker}</td><td><span class="pos">LONG</span><small>RSI 1м 34↑ · 5м 42↑ · TRUP</small></td><td>10</td><td>10 000 ₽<small>свои 10 000 ₽</small></td><td>1,0×</td><td>278,80 → 281,40</td><td class="${net > 0 ? 'pos' : 'neg'}">${net > 0 ? '+' : ''}${money(net)} ₽</td><td>12,00 ₽</td><td>${net > 0 ? 'Take profit' : 'Stop loss'}</td><td>MACD + RSI · ↑2 / ↓0<small>TREND_UP</small></td><td>${net > 0 ? '+1,42' : '−1,00'}</td><td>⌄</td></tr>`;
  }).join("");
}

function renderRuns() {
  const query = byId("bot-test-search").value.toLowerCase();
  document.querySelector("#bot-tests-table tbody").innerHTML = demoRuns.map((r,i)=>({r,i})).filter(({r}) => r.name.toLowerCase().includes(query)).map(({r,i}) => `<tr data-demo-run="${esc(r.name)}"><td><input aria-label="Выбрать ${esc(r.name)}" type="checkbox" data-run-check="${esc(r.name)}" ${chosenRuns.has(r.name) ? 'checked' : ''}></td><td><button class="mock-small-btn" data-run-open="${i}">${esc(r.name)}</button></td><td>${r.date}</td><td>${r.n}</td><td>${r.wins}</td><td>${r.losses}</td><td>12 800 ₽</td><td>−11 414 ₽</td><td class="${r.net >= 0 ? 'pos' : 'neg'}">${money(r.net)}</td><td>${r.pf}</td><td>${r.wr}%</td><td>0</td><td><button class="btn-sm" data-run-repeat="${i}" title="Копия только в макете">↻</button> <button class="btn-sm" data-run-delete="${i}" title="Удалить демонстрационную строку">✕</button></td></tr>`).join("");
  set("bot-test-count", `${demoRuns.length} демонстрационных прогонов`);
  set("bot-test-selcount", chosenRuns.size);
  byId("btn-test-delete-selected").disabled = !chosenRuns.size;
}

function openRun(index) {
  const run = demoRuns[index];
  if (!run) return;
  byId("bot-test-drawer").classList.remove("hidden");
  set("btd-title", run.name);
  set("btd-sub", "Пример карточки · реальные результаты не загружены");
  byId("btd-cards").innerHTML = cards([["Сделок",run.n],["Wins",run.wins],["Losses",run.losses],["Net ₽",money(run.net)],["PF",run.pf],["WR",run.wr+"%"]]);
  byId("btd-body").innerHTML = table(["Вход","Выход","Тикер","Side","Qty","Вход ₽","Выход ₽","SL","TP","Net ₽","Комиссия","Причина выхода","Голоса"], [["15.10 10:20","15.10 11:03","SBER","LONG","40","278,80","281,40","276,40","285,20","92,00","12,00","take_profit","RSI + MACD"]]);
}

function renderLogs() {
  const q = byId("lg-q").value.toLowerCase();
  const ticker = byId("lg-ticker").value.toUpperCase();
  const level = byId("lg-level").value;
  const date = byId("log-filter-date").value;
  const shown = logs.filter((l) => byId(`lg-${l.category}`).checked && (!level || level === l.level) && (!q || `${l.message} ${l.ticker}`.toLowerCase().includes(q)) && (!ticker || l.ticker.includes(ticker)) && (!date || date === "2026-10-15"));
  byId("mock-log-list").innerHTML = shown.map((l) => `<article class="mock-log"><time>${l.time}</time> <strong>${l.ticker}</strong> <span class="${l.level === 'warn' ? 'neg' : 'dim'}">${l.level.toUpperCase()}</span><p>${l.message}</p><em>${l.category} · пример события</em></article>`).join("") || '<p class="dim">Нет демонстрационных событий по фильтру.</p>';
  set("log-count", shown.length);
  set("lg-err-pill", `${shown.filter((x) => x.level === "warn").length} warn`);
}

function initLogs() {
  byId("mock-logs").innerHTML = `<div class="mock-log-panel"><div class="logs-heading"><strong>ЛОГИ · МСК</strong><button id="lg-copy" class="mock-small-btn">Копировать</button><button id="lg-clear" class="mock-small-btn">Очистить</button></div><div class="mock-log-tools">${[["candles","Свечи"],["signals","Сигналы"],["trades","Сделки"],["events","События"],["tech","Тех"]].map(([key,label]) => `<label><input type="checkbox" id="lg-${key}" checked>${label}</label>`).join("")}</div><div class="mock-log-search"><select id="lg-level" aria-label="Уровень логов"><option value="">Все уровни</option><option>debug</option><option>info</option><option>warn</option><option>error</option></select><input id="lg-ticker" placeholder="Тикер" aria-label="Тикер в логах"></div><div class="mock-log-search"><input id="lg-q" placeholder="Поиск по логам" aria-label="Поиск в логах"></div><div class="mock-log-search"><input type="date" id="log-filter-date" aria-label="Дата логов"><span id="lg-err-pill"></span></div><div id="mock-log-list"></div><div class="mock-log-tools"><button class="mock-small-btn" id="mock-older">Старые события</button><span>Видно: <b id="log-count"></b></span></div></div>`;
  byId("mock-logs").addEventListener("input", renderLogs);
  byId("mock-logs").addEventListener("change", renderLogs);
  renderLogs();
}

function renderMarket() {
  const q = byId("sr-query").value.toLowerCase();
  const min = Number(byId("sr-min-t").value) || 0;
  const only = byId("sr-univ-only").checked;
  let rows = tickers.map((ticker,i) => ({ ticker, price: positions.find((p) => p.ticker === ticker)?.price ?? 250 + i * 19, turnover: 1540 - i * 39, rng_pct: .7 + i % 5 * .2, atr_pct: .18 + i % 7 * .09, votes: i % 4, regime: i % 3 ? "TRUP" : "RNG", i }));
  rows = rows.filter((x) => (!q || x.ticker.toLowerCase().includes(q)) && x.turnover >= min && (!only || x.i < 20));
  rows.sort((a,b) => (typeof a[marketSort.key] === "string" ? a[marketSort.key].localeCompare(b[marketSort.key]) : a[marketSort.key] - b[marketSort.key]) * marketSort.direction);
  byId("sr-tbody").innerHTML = rows.map((m) => `<tr tabindex="0" data-ticker="${m.ticker}"><td><b>${m.ticker}</b></td><td class="sr-num">${money(m.price)}</td><td class="sr-num">${money(m.turnover)} М</td><td class="sr-num">${m.rng_pct.toFixed(2)}%</td><td class="sr-num">${m.atr_pct.toFixed(2)}%</td><td class="sr-num"><span class="mock-pill ${m.votes >= 2 ? 'up' : ''}">↑${m.votes} / ↓0</span></td><td class="sr-num">${m.regime}</td><td><button class="mock-small-btn" data-universe="${m.ticker}" title="Изменить выбор только в макете">${m.i < 20 ? '✓' : '+'}</button></td></tr>`).join("");
  set("sr-count", `${rows.length} из 31 · демонстрация`);
  set("sr-ts", "14:35 МСК");
}

function renderAI() {
  set("ag-sub", "Пример · не подключён");
  set("ag-summary", "Таймаут 20с · default SKIP · в ожидании 2 · демонстрационные значения");
  byId("ag-list").innerHTML = ["SBER","ROSN","NVTK"].map((ticker,i) => `<button class="ag-row ${i === 2 ? 'reject' : 'approve'}" data-ticker="${ticker}" style="width:100%;text-align:left;background:transparent;color:inherit;border:0;border-bottom:1px solid var(--border);padding:12px"><div class="ag-r1"><span class="ag-t">14:${30 - i}</span><b class="ag-tk">${ticker} LONG</b><span class="ag-dec">${i === 2 ? 'Отклонено' : 'Одобрено'} · ДЕМО</span></div><div class="ag-r2">${i === 2 ? 'Недостаточно подтверждений старшего таймфрейма.' : 'Направление и риск согласованы с заданными правилами.'}<div class="ag-meta">пример модели · conf 0,72 · 840 мс · shadow</div></div></button>`).join("");
  set("ag-watch-sub", "2 примера");
  byId("ag-notes").innerHTML = `<div class="ag-note"><b>SBER · держать</b><p>SL не достигнут. Пример объяснения вахтёра.</p></div><div class="ag-note"><b>ROSN · наблюдать</b><p>Расстояние до SL: 1,3 ATR. Торговое действие не выполнялось.</p></div>`;
  set("ag-prompt-info", "Демонстрационный текст, не серверный промпт");
  byId("ag-prompt-edit").value = "ДЕМО-ПРОМПТ. Описать текущий контекст, риск и основания решения. Не выдавать уверенность за вероятность прибыли.";
}

function cards(values) {
  return values.map(([label,value]) => `<div><span>${esc(label)}</span><b>${esc(value)}</b></div>`).join("");
}

function heatmap(names) {
  return `<div class="mock-report-table"><div style="min-width:900px">${names.map((ticker,j) => `<h4>${ticker}</h4><div class="mock-heatmap">${Array.from({ length:12 },(_,i) => { const v = Math.sin(i*.8+j)*.7; return `<button data-ticker="${ticker}" style="background:${v>=0?'#345243':'#583c43'}">${10 + Math.floor(i/2)}:${i%2?'30':'00'}<small>${v>=0?'+':''}${v.toFixed(2)}%</small><small>${v>=0?'UP':'DOWN'} · 30м</small></button>`; }).join("")}</div>`).join("")}</div></div>`;
}

function renderAnalytics() {
  set("stats-test", "ДЕМО · все примеры 01.10–08.10");
  set("an-sizing", "слот 20% · до 5 позиций · экспозиция 100% · пример");
  byId("an-aigate").innerHTML = '<div class="an-ag-head">AI-гейт · пример контрфактического отчёта</div><p>Влияние, сэкономлено/упущено, согласия, провайдер, задержка — отдельные показатели. В макете не вычислялись.</p>';
  byId("an-aireport").innerHTML = '<div class="an-ag-head">AI о рынке · пример</div><p>Анализ, предложения по механизму, действия и расход токенов будут показаны с источником и временем. Реальный AI-отчёт не запрашивался.</p>';
  byId("an-heatmap").innerHTML = '<h3 class="pg-h3">Тепловая карта · изменение, bias и режим</h3>'+heatmap(tickers.slice(0,8));
  byId("an-movers").innerHTML = '<h3 class="pg-h3">Движения по горизонтам · пример</h3>'+table(["Горизонт","Рост","Падение","≥5% / ≥10% / ≥20%"], ["1 день","1 неделя","1 месяц","3 месяца"].map((h) => [h,"SBER +1,2%","ROSN −0,8%","8/4 · 3/2 · 1/1"]));
  byId("an-portfolio").innerHTML = '<h3 class="pg-h3">Портфель, риск и очередь · пример</h3>'+table(["Показатель","Значение","Контекст"],[["Экспозиция","L 30% · S 0% · net 30%","gross 0,3×"],["Маржа","0% · свободно 87 130 ₽","Стресс ±5%: −1,5% / +1,5%"],["Очередь","SBER · LKOH","Score / veto / причины"],["Рейтинг","top-10 + explore","только прошлые данные"],["Просадка","−0,4% от пика","пример"],["Сектора","Финансы 9% · нефть 21%","концентрация"],["Пропуски","Уже в позиции 6 · cooldown 2","счётчики причин"]]);
  const sections = ["По направлению","По bias","По режиму","По сессии","По акциям","По входам","По выходам","По стратегиям (голоса)","По кворуму"];
  byId("stats-body").innerHTML = '<div class="stats-overall">'+cards([["Сделок",124],["WR","52,4%"],["Net","1 386 ₽"],["PF","1,12"],["Gross+","12 800 ₽"],["Gross−","−11 414 ₽"],["Avg win","196 ₽"],["Avg loss","−193 ₽"],["Открыто",3]])+'</div><div class="mock-stats-grid">'+sections.map((s) => `<section class="stats-sec"><h3 class="stats-title">${s}</h3>${table(["Ключ","N","WR","Net","PF"],[["Пример A",61,"54%","+824 ₽","1,2"],["Пример B",63,"51%","+562 ₽","1,08"]])}</section>`).join("")+'</div>';
  byId("stats-cmp-list").innerHTML = demoRuns.map((r,i) => `<label style="display:block;padding:5px"><input type="checkbox" data-compare="${i}"> ${esc(r.name)}</label>`).join("");
  renderReports();
}

function renderReports() {
  const query = byId("rep-query").value.toLowerCase();
  byId("rep-list").innerHTML = demoRuns.map((r,i) => ({r,i})).filter(({r}) => r.name.toLowerCase().includes(query)).map(({r,i}) => `<button class="rep-list-item ${i === reportRun ? 'selected' : ''}" data-report="${i}"><strong>${esc(r.name)}</strong><small>${r.date} · ${r.n} сделок</small><small>Пресет ${r.preset} · демо</small></button>`).join("");
  const run = demoRuns[reportRun] || demoRuns[0];
  if (!run) { byId("rep-runhead").innerHTML = '<p>Демонстрационных прогонов нет.</p>'; return; }
  set("rep-sub", "Примеры представления, не принятые исследования");
  byId("rep-runhead").innerHTML = `<h3 class="pg-h3">${esc(run.name)}</h3><p class="mini-hint">${run.date} · DEMO INPUT · MODELLED · неизвестные исходы: пример поля</p><div class="rep-cards">${cards([["Сделок",run.n],["Net",money(run.net)+" ₽"],["PF",run.pf],["WR",run.wr+"%"]])}</div><div class="mock-log-tools">${["Сессии: день","Слот: 20%","Вход: 5м","Исполнение: 1м","Издержки: 5/2","Гейты: 3","SL/TP: ATR","Unknown: отдельно"].map((s)=>`<span class="mock-pill">${s}</span>`).join("")}</div>`;
  byId("rep-actions").classList.remove("hidden");
  byId("rep-actions").innerHTML = '<button class="btn-secondary" data-report-repeat>Новый демо-прогон</button> <button class="btn-secondary" data-report-delete>Удалить демо-строку</button>';
  byId("rep-pane-strategies").innerHTML = table(["Робот","Сделок","Wins","Losses","WR","Gross+","Gross−","Net","PF","Expectancy","DD%","Комиссия","Тикеров"],strategies.slice(0,4).map((name,i) => [name,30+i,16+i,14,"53%","1 400","−1 200","200","1,17","6,7","0,4","42",5]));
  byId("rep-dims").innerHTML = ["Тикер","Направление","Сессия","Режим","Выход","Вход","Час","День недели"].map((d,i) => `<button class="btn-secondary ${i===0?'active':''}" data-mock-dim="${d}">${d}</button>`).join(" ");
  byId("rep-slices").innerHTML = table(["Срез","N","Wins","Losses","WR","Gross+","Gross−","Net","PF","Exp"],tickers.slice(0,5).map((t)=>[t,20,11,9,"55%",1200,-1000,200,"1,2",10]));
  byId("rep-trades-filters").innerHTML = '<div class="mock-log-tools">'+["Результат","Тикер","Side","Причина выхода","Причина входа"].map((label)=>`<label>${label} <select data-mock-report-filter><option>Все</option><option>${label==='Тикер'?'SBER':label==='Side'?'LONG':'Пример A'}</option></select></label>`).join("")+'</div>';
  byId("rep-trades-summary").innerHTML = cards([["Известные закрытия",run.n],["Открытые",0],["Неизвестные","пример поля"]]);
  byId("rep-trades").innerHTML = table(["Тикер","Side","Вход","Выход","SL","TP","MAE","MFE","Удержание","Net","Причина"],[["SBER","LONG","15.10 10:20","15.10 11:03","276,4","285,2","−0,4%","1,8%","43м","92 ₽","take_profit"]]);
  byId("rep-trades-more").innerHTML = '<button class="btn-secondary" id="mock-more-trades">Показать ещё · демо</button>';
  byId("rep-market").innerHTML = table(["Тикер","Доходность","Волатильность","Оборот"],tickers.slice(0,12).map((t,i)=>[t,`${i%2?'+':'−'}${(i*.1).toFixed(1)}%`,`${(.8+i*.1).toFixed(1)}%`,`${1500-i*30} М₽`]));
  selectReportTab(reportTab);
}

function selectReportTab(next) {
  reportTab = next;
  document.querySelectorAll("[data-reptab]").forEach((b)=>{b.classList.toggle("active",b.dataset.reptab===next);b.setAttribute("aria-pressed",String(b.dataset.reptab===next));});
  document.querySelectorAll(".rep-pane").forEach((el)=>el.classList.toggle("hidden",el.id!==`rep-pane-${next}`));
}

function renderWarehouse() {
  set("sklad-status", "Пример карточек · структура рабочего каталога");
  byId("strategy-cards").innerHTML = strategies.map((name,i)=>`<article class="scard"><label class="scard-head"><input type="checkbox" data-demo-strategy="${i}" checked><b>${name}</b><span class="badge">SETUP</span></label><div class="scard-rules">▲ Пример условия LONG по закрытому бару<br>▼ Пример условия SHORT по закрытому бару</div><div class="scard-params"><label>Период <input aria-label="Период ${name}" type="number" value="${14+i*2}" min="2"></label><label>Порог <input aria-label="Порог ${name}" type="number" value="${30+i}" min="0"></label></div><label class="scard-role">Роль <select aria-label="Роль ${name}"><option>SETUP</option><option>BIAS</option><option>ENTRY</option></select></label><button class="mock-small-btn" data-reset-strategy="${i}">Сбросить</button></article>`).join("");
  byId("pipe-holder").innerHTML = '<div class="mock-flow">'+["BIAS · направление","SETUP · голосующие функции","ENTRY · вход","EXIT · выход"].map((x)=>`<div><strong>${x}</strong><span>Выбранные блоки · демо</span></div>`).join("")+'</div><label>Политика выхода <select id="mock-exit"><option>Fixed SL/TP</option><option>ATR stop</option><option>Trailing</option></select></label><div class="scard-params"><label>Stop % <input type="number" value="1" step=".1"></label><label>Target % <input type="number" value="2" step=".1"></label></div>';
  const gates = ["IMOEX guard","Trend alignment","AI approval","Loss streak hold","Ранжирование","Очередь сделок","Фильтр сессии","Баланс Long / Short"];
  byId("gates-container").innerHTML = '<div class="mock-gates">'+gates.map((g,i)=>`<label><input type="checkbox" ${i<3?'checked':''}> ${g}<small>Демо-переключатель · не изменяет runtime</small></label>`).join("")+'</div>';
  set("gates-status", "Пример текущего состояния");
  byId("logic-container").innerHTML = '<div class="mock-log-tools">'+["vol_thr=0","bias=info","regime=2","quorum=2","semi_flip"].map((s)=>`<span class="mock-pill">${s}</span>`).join("")+'</div><div class="mock-flow">'+[["BIAS","H1 / EMA50"],["SETUPS","7 функций · 5м"],["QUORUM","2 / 2 · next open"],["ORDER","после всех гейтов"]].map(([a,b])=>`<div><strong>${a}</strong><span>${b}</span></div>`).join("")+'</div>'+table(["Этап воронки","Число · пример"],[["Нет сигнала",240],["Кворум набран",18],["Гейт отклонил",6],["Заявка выставлена",12],["Исполнено",12],["Закрыто",9]]);
  set("logic-status", "Пример потока сигнала");
}

function resizePanel(handle, element, axis, min, max, reverse=false) {
  handle.addEventListener("pointerdown",(e)=>{
    if (e.button!==0) return;
    e.preventDefault();
    const start=axis==='x'?e.clientX:e.clientY;
    const size=axis==='x'?element.getBoundingClientRect().width:element.getBoundingClientRect().height;
    handle.setPointerCapture(e.pointerId);
    const move=(ev)=>{const now=axis==='x'?ev.clientX:ev.clientY;const value=Math.max(min,Math.min(max,size+(now-start)*(reverse?-1:1)));element.style[axis==='x'?'width':'height']=`${value}px`;};
    const end=()=>{handle.removeEventListener('pointermove',move);handle.removeEventListener('pointerup',end);handle.removeEventListener('pointercancel',end);};
    handle.addEventListener('pointermove',move);handle.addEventListener('pointerup',end);handle.addEventListener('pointercancel',end);
  });
}

function mapDialog() {
  dialog("Карта рабочего фронтенда", '<p>Макет 02 построен из полной рабочей разметки, а не из обзорной карточки. Все значения ниже — иллюстрация интерфейса, торговля не подключена.</p>'+table(["Зона","Что сохранено","Как открыть"],[["Верхняя панель","3 раздела, сессия, MOEX, 5 AI-режимов, Live/Sandbox/Test, пауза, старт, настройки","Всегда сверху"],["Финансы","Equity, свободные средства, обеспечение, P&L, деньги, шорты","6 компактных показателей"],["Быстрые настройки","Сессии/overnight/сверка, Long/Short, маржа/плечо, режимы, слот/слоты/×EQ","Под показателями"],["Логи","5 категорий, уровень, текст, тикер, дата, старые события, копирование","Левая панель"],["Бот","График, позиции 15 полей, сделки 13 полей + раскрытие, тесты 13 полей + карточка","Центр; таблицы ↔ или развернуть"],["Рынок / AI","Скринер 8 полей, карусель, кворум; решения, промпты, вахтёр, сообщение","Правая панель и её вкладки"],["Склад","График/индикаторы, каталог, роли и параметры, конструктор, exits, гейты, воронка","Склад"],["Анализ","Живая, heatmap, движения, портфель/риск/очередь, AI, 9 срезов","Анализ → Живая"],["Отчёты","Harness/replay, виды, поиск, теги, роботы/срезы/сделки/рынок","Анализ → Прогоны"],["Формы","Все исходные поля настроек бота и теста, сравнение пресет/UI","Настройки / Новый тест"]])+ '<p>Размер шрифта: основные данные 13–14 px. Широкие таблицы не теряют столбцы: есть горизонтальная прокрутка и разворот на всю ширину. Боковые панели изменяют ширину и сворачиваются.</p>');
}

function init() {
  document.querySelectorAll('.workbench-table-scroll').forEach((x)=>x.remove());
  ["bot-positions-table","bot-trades-table","bot-tests-table"].forEach((id)=>{const el=byId(id);const wrap=document.createElement('div');wrap.className='workbench-table-scroll';wrap.tabIndex=0;wrap.setAttribute('role','region');wrap.setAttribute('aria-label',`${id}: все столбцы, горизонтальная прокрутка`);el.before(wrap);wrap.append(el);});
  const values={"bs-equity":"124 386 ₽","bs-cash":"87 130 ₽","bs-positions-value":"37 256 ₽","bs-pnl":"+1 386 ₽","bs-currencies":"87 130 ₽","bs-shares":"0 ₽","bs-session":"День · демо","bs-trading-status":"Умеренный рост · демо","bot-state-text":"Демо-бот активен","topbar-status":"Синтетические данные · нет подключения"};
  Object.entries(values).forEach(([k,v])=>set(k,v));
  byId('bot-pill-top').classList.remove('off');
  byId('ai-mode-select').value='bot';
  set('btn-bot-toggle','Остановить · демо');
  const modeHint=document.querySelector('.botbar-hint');if(modeHint)modeHint.textContent='Макет · без счёта';
  byId('sr-carousel-panel').innerHTML='<div class="mock-market-note">Карусель · top-20 · цикл 5 минут · демо</div><label class="mini-hint"><input type="checkbox" checked> Включена</label> <button class="mock-small-btn" data-carousel-settings>Настройки отбора</button><div class="mock-market-note">Все 8 полей скринера доступны прокруткой ↔</div>';
  byId('votes-editor').innerHTML='<h3>Состав кворума · демо</h3>'+strategies.map((s)=>`<label><input type="checkbox" checked> ${s}</label>`).join('')+'<label>Кворум <input type="number" value="2" min="1" max="7"></label>';
  byId('ts-preset').innerHTML='<option value="">Текущие настройки — демо</option>'+demoRuns.map((r)=>`<option value="${r.preset}">${r.preset}</option>`).join('');
  byId('ts-engine').innerHTML='<option>ensemble_v4</option><option>macd_cross</option><option>donchian_breakout</option>';
  byId('ts-interval').innerHTML=['1min','5min','10min','30min','hour'].map((x)=>`<option>${x}</option>`).join('');
  set('pc-hint-choice','Демонстрация разрешения конфликта пресета и UI: выбор только локальный, автоматический запуск и таймер отключены.');
  byId('pc-timer-wrap').classList.add('hidden');
  byId('pc-list').innerHTML=table(['Поле','UI','Пресет'],[['Позиция','30%','20%'],['Сессия','День','Утро + день']]);
  for(const modal of document.querySelectorAll('.bot-modal-body'))modal.insertAdjacentHTML('afterbegin','<p class="mini-hint" style="border:1px solid #958265;padding:9px;color:#e4c894">МАКЕТ: все поля сохранены для оценки размещения. Сохранение и запуск только демонстрационные.</p>');
  initLogs();renderPositions();renderTrades();renderRuns();renderMarket();renderAI();renderWarehouse();renderAnalytics();showPage('bot');
  resizePanel(document.querySelector('#sidebar .sidebar-grip'),byId('sidebar'),'x',190,560);
  resizePanel(document.querySelector('#sidebar-right .sidebar-grip'),byId('sidebar-right'),'x',280,800,true);
  resizePanel(byId('bot-resizer'),byId('bot-chart-wrap'),'y',180,650);
  resizePanel(byId('ag-resizer'),byId('ag-list'),'y',100,650);
}

init();

document.addEventListener('click',(e)=>{
  const b=e.target.closest('button');
  if(b){
    if(b.dataset.page)return showPage(b.dataset.page);
    if(b.dataset.mode){document.querySelectorAll('.mode-btn').forEach((x)=>x.classList.toggle('active',x===b));set('topbar-status',`${b.dataset.mode.toUpperCase()} · только режим макета, счёт не подключён`);return toast('Режим реального бота не изменён.');}
    if(b.dataset.int){timeframe=b.textContent.trim();document.querySelectorAll('.ct-int').forEach((x)=>x.classList.toggle('active',x===b));return drawCharts();}
    if(b.dataset.srtab){document.querySelectorAll('.sr-tab').forEach((x)=>x.classList.toggle('active',x===b));document.querySelectorAll('.sr-tabpane').forEach((x)=>x.classList.toggle('hidden',x.id!==`sr-pane-${b.dataset.srtab}`));return;}
    if(b.dataset.antab){document.querySelectorAll('.an-tab').forEach((x)=>x.classList.toggle('active',x===b));document.querySelectorAll('.an-tabpane').forEach((x)=>x.classList.toggle('hidden',x.id!==`an-pane-${b.dataset.antab}`));return;}
    if(b.dataset.reptab)return selectReportTab(b.dataset.reptab);
    if(b.dataset.mockTf){timeframe=b.dataset.mockTf;return drawCharts();}
    if(b.dataset.focusTable){document.body.classList.toggle('table-focus');document.querySelectorAll('.workbench-table-scroll').forEach((x)=>x.classList.remove('focused'));const w=byId(b.dataset.focusTable).parentElement;w.classList.add('focused');w.scrollIntoView({block:'center'});b.textContent=document.body.classList.contains('table-focus')?'Вернуть панели':'Развернуть таблицу';return;}
    if(b.hasAttribute('data-mock-close'))return toast(`Пример закрытия ${b.dataset.mockClose}. Заявка не отправлялась, позиция не изменена.`);
    if(b.dataset.runOpen!==undefined)return openRun(Number(b.dataset.runOpen));
    if(b.dataset.runRepeat!==undefined){const item=demoRuns[Number(b.dataset.runRepeat)];demoRuns.push({...item,name:item.name+' · копия'});renderRuns();renderReports();return toast('Добавлена демонстрационная копия. Исследование не запускалось.');}
    if(b.dataset.runDelete!==undefined){demoRuns.splice(Number(b.dataset.runDelete),1);renderRuns();renderReports();return;}
    if(b.dataset.report!==undefined){reportRun=Number(b.dataset.report);return renderReports();}
    if(b.dataset.mockDim){byId('rep-slices').innerHTML=table([b.dataset.mockDim,'N','WR','Net'],[['Пример A',61,'54%','824 ₽'],['Пример B',63,'51%','562 ₽']]);return;}
    if(b.hasAttribute('data-report-repeat'))return toast('Новый реальный прогон не запускался. Это пример действия отчёта.');
    if(b.hasAttribute('data-report-delete'))return toast('Реальные отчёты не удаляются. Это макет действия.');
    if(b.dataset.resetStrategy!==undefined){b.closest('.scard').querySelectorAll('input[type=number]').forEach((i)=>i.value=i.defaultValue);return;}
    if(b.dataset.kind){document.querySelectorAll('.ag-ptab').forEach((x)=>x.classList.toggle('active',x===b));byId('ag-prompt-edit').value=`ДЕМО-ПРОМПТ ${b.dataset.kind}. Описать контекст и ограничения; не выполнять торговые действия.`;return;}
    if(b.dataset.sort){const key=b.dataset.sort==='atr_pct'?'atr_pct':b.dataset.sort==='votes'?'votes':b.dataset.sort==='regime'?'regime':b.dataset.sort;marketSort={key,direction:marketSort.key===key?-marketSort.direction:-1};return renderMarket();}
    if(b.hasAttribute('data-carousel-settings'))return dialog('Карусель и отбор · пример','<p>Top-N, частота пересчёта, порог волатильности, лимит стоимости лота, hysteresis и blacklist остаются отдельными параметрами отбора.</p><p>В макете не запрашиваются MOEX/T-Invest и не изменяется universe.</p>');
    const id=b.id;
    if(id==='layout-map')return mapDialog();
    if(id==='mock-detail-close')return byId('mock-detail').close();
    if(id==='sidebar-collapse'||id==='rail-collapse'){const panel=byId(id==='sidebar-collapse'?'sidebar':'sidebar-right');panel.classList.toggle('collapsed');return;}
    if(id==='votes-config-btn')return byId('votes-editor').classList.toggle('hidden');
    if(id==='ag-prompt-btn')return byId('ag-prompt').classList.toggle('hidden');
    if(id==='ag-full')return byId('sr-pane-aigate').classList.toggle('full-ai');
    if(id==='ag-prompt-save')return set('ag-prompt-status','Демо: текст оставлен в памяти страницы. На сервер не отправлен.');
    if(id==='ag-prompt-reset'){renderAI();return;}
    if(id==='ag-send-btn'){set('ag-send-status','Демо: сообщение не отправлено AI.');return;}
    if(id==='btn-bot-settings'){byId('bot-modal-overlay').classList.remove('hidden');return;}
    if(id==='bs-close'||id==='bs-save'){byId('bot-modal-overlay').classList.add('hidden');if(id==='bs-save')toast('Настройки не отправлены в runtime.');return;}
    if(id==='btn-test-new'){byId('test-modal-overlay').classList.remove('hidden');return;}
    if(id==='ts-close'){byId('test-modal-overlay').classList.add('hidden');return;}
    if(id==='ts-run'){byId('test-modal-overlay').classList.add('hidden');byId('pc-modal-overlay').classList.remove('hidden');return;}
    if(['pc-close','pc-use-preset','pc-use-ui'].includes(id)){byId('pc-modal-overlay').classList.add('hidden');if(id!=='pc-close')toast('Выбор показан только в макете. Реальный тест не запускался.');return;}
    if(id==='btn-bot-pause'){paused=!paused;b.textContent=paused?'Возобновить · демо':'Пауза входов · демо';set('bot-state-text',paused?'Входы на паузе · демо':running?'Демо-бот активен':'Демо-бот выключен');return;}
    if(id==='btn-bot-toggle'){running=!running;b.textContent=running?'Остановить · демо':'Запустить · демо';set('bot-state-text',running?'Демо-бот активен':'Демо-бот выключен');return toast('Изменилось только состояние макета.');}
    if(id==='btd-close')return byId('bot-test-drawer').classList.add('hidden');
    if(id==='btd-open-analysis'){showPage('analytics');document.querySelector('[data-antab=reports]').click();return;}
    if(id==='btn-test-delete-selected'){for(let i=demoRuns.length-1;i>=0;i--)if(chosenRuns.has(demoRuns[i].name))demoRuns.splice(i,1);chosenRuns.clear();renderRuns();return renderReports();}
    if(id==='lg-clear'){logs.length=0;return renderLogs();}
    if(id==='lg-copy'){navigator.clipboard?.writeText(byId('mock-log-list').innerText).then(()=>toast('Скопированы только демонстрационные логи.')).catch(()=>toast('Копирование недоступно.'));return;}
    if(id==='mock-older'){logs.push({time:'09:55:00',category:'tech',ticker:'SBER',level:'info',message:'Пример более старого события'});return renderLogs();}
    if(id==='stats-cmp-toggle')return byId('stats-cmp-panel').classList.toggle('hidden');
    if(id==='stats-cmp-run'){byId('stats-cmp-result').innerHTML=table(['Прогон','Сделок','WR','Net','PF'],demoRuns.filter((_,i)=>document.querySelector(`[data-compare="${i}"]`)?.checked).map((r)=>[esc(r.name),r.n,r.wr+'%',money(r.net),r.pf]));return;}
    if(id==='btn-indicators')return byId('indicators-menu').classList.toggle('hidden');
    if(id==='btn-sklad-clear'){document.querySelectorAll('[data-demo-strategy]').forEach((x)=>x.checked=false);return;}
    if(id==='btn-run-strategies')return toast(`Выбрано функций: ${document.querySelectorAll('[data-demo-strategy]:checked').length}. Реальный расчёт не выполняется.`);
    return toast(`${b.textContent.trim().slice(0,65)||b.title||'Действие'}: только пример интерфейса.`);
  }
  const sort=e.target.closest('th[data-sort]');
  if(sort){marketSort={key:sort.dataset.sort,direction:marketSort.key===sort.dataset.sort?-marketSort.direction:-1};return renderMarket();}
  const trade=e.target.closest('[data-trade-detail]');
  if(trade){focusTicker(trade.dataset.ticker);const next=trade.nextElementSibling;if(next?.classList.contains('mock-detail-row'))next.remove();else trade.insertAdjacentHTML('afterend',`<tr class="mock-detail-row"><td colspan="13">${tradeDetail(trade.dataset.ticker)}</td></tr>`);return;}
  const ticker=e.target.closest('[data-ticker]');if(ticker)focusTicker(ticker.dataset.ticker);
});

document.addEventListener('change',(e)=>{
  const el=e.target;
  if(el.dataset.runCheck){el.checked?chosenRuns.add(el.dataset.runCheck):chosenRuns.delete(el.dataset.runCheck);return renderRuns();}
  if(el.id==='bot-test-checkall'){demoRuns.forEach((r)=>el.checked?chosenRuns.add(r.name):chosenRuns.delete(r.name));return renderRuns();}
  if(['sr-min-t','sr-univ-only'].includes(el.id))return renderMarket();
  if(el.id==='ts-custom-on')return byId('ts-custom-body').classList.toggle('hidden',!el.checked);
  if(el.id==='ts-gates-mode'){document.querySelectorAll('#ts-gates input').forEach((x)=>x.disabled=el.value!=='custom');return;}
  if(el.name==='sl-mode'){byId('bs-atr-section').classList.toggle('bs-hidden',el.value!=='atr');byId('bs-fixed-section').classList.toggle('bs-hidden',el.value!=='fixed');return;}
  if(el.name==='sl-source'){byId('bs-optuna-section').classList.toggle('bs-hidden',el.value!=='optuna');return;}
  if(el.id==='rep-source'){byId('rep-kind').disabled=el.value==='replay';return renderReports();}
  if(el.id==='rep-kind')return renderReports();
  if(el.classList.contains('ct-int'))return;
  if(el.matches('#indicators-menu input'))return toast('Вид переключателя индикатора сохранён; индикатор по реальным данным не рассчитывался.');
  if(!el.closest('#mock-logs'))toast('Изменение только в макете. На сервер ничего не отправлено.');
});

document.addEventListener('input',(e)=>{
  if(e.target.id==='bot-test-search')renderRuns();
  if(e.target.id==='sr-query')renderMarket();
  if(e.target.id==='rep-query')renderReports();
  const labels={'sz-pos':'sz-pos-label','sz-max':'sz-max-label','sz-eq':'sz-eq-label','mg-lev':'mg-lev-label'};
  if(labels[e.target.id])set(labels[e.target.id],e.target.value+(e.target.id==='sz-pos'?'%':''));
});

document.addEventListener('keydown',(e)=>{
  if(e.key==='Escape'){document.querySelectorAll('.modal-overlay').forEach((el)=>el.classList.add('hidden'));byId('sr-pane-aigate').classList.remove('full-ai');document.body.classList.remove('table-focus');}
  if(e.key==='Enter'&&e.target.matches('tr[data-ticker]'))e.target.click();
});
