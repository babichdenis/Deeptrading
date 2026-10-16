"use strict";

const instruments = {
  SBER: { name: "Сбербанк", letter: "S", price: 280.64, change: "+0,71%", decimals: 2, span: 2.5, color: "#284a39" },
  LKOH: { name: "Лукойл", letter: "Л", price: 6802, change: "+0,56%", decimals: 0, span: 65, color: "#4d3035" },
  ROSN: { name: "Роснефть", letter: "Р", price: 517.77, change: "−0,24%", decimals: 2, span: 4.1, color: "#4b4530" }
};
const positions = [
  { ticker: "SBER", name: "Сбербанк", letter: "S", css: "", qty: 40, entry: 278.8, stop: 276.4, pnl: 73.6, pct: "+0,66%", signal: "RSI + MACD", time: "13:42" },
  { ticker: "LKOH", name: "Лукойл", letter: "Л", css: "lukoil", qty: 2, entry: 6749, stop: 6670, pnl: 106, pct: "+0,79%", signal: "Пробой + тренд", time: "12:18" },
  { ticker: "ROSN", name: "Роснефть", letter: "Р", css: "rosneft", qty: 24, entry: 520.3, stop: 513.2, pnl: -60.72, pct: "−0,49%", signal: "Откат к EMA", time: "14:05" }
];
const events = [
  { kind: "system", title: "Данные синхронизированы", time: "14:32:01", text: "Новых пропусков не обнаружено. Демонстрационный набор обновлён.", meta: "30 инструментов · 1м" },
  { kind: "trade", title: "ROSN · открыта позиция", time: "14:05:00", text: "Откат к средней в направлении старшего таймфрейма.", meta: "LONG · 24 шт. · 520,30 ₽" },
  { kind: "trade", title: "SBER · подтверждён вход", time: "13:42:00", text: "Сочетание RSI и MACD. Риск укладывается в лимит.", meta: "LONG · 40 шт. · 278,80 ₽" },
  { kind: "system", tone: "warning", title: "NVTK · вход пропущен", time: "13:35:00", text: "Недостаточно подтверждений. Новая позиция не открыта.", meta: "Кворум 1 / 2" }
];
const state = { ticker: "SBER", timeframe: 5, chart: "price", events: "all", paused: false };
const currency = new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const integer = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 });
const $ = (id) => document.getElementById(id);
let toastTimer;

function toast(message) {
  $("toast").textContent = message;
  $("toast").classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("toast").classList.remove("visible"), 4000);
}

function renderPositions() {
  const search = $("position-search").value.trim().toLocaleLowerCase("ru");
  const shown = positions.filter((p) => `${p.ticker} ${p.name}`.toLocaleLowerCase("ru").includes(search));
  $("positions-body").innerHTML = shown.length ? shown.map((p) => {
    const sign = p.pnl >= 0 ? "+" : "−";
    const cls = p.pnl >= 0 ? "positive" : "negative";
    return `<tr><td><div class="position-name"><span class="mini-logo ${p.css}">${p.letter}</span><span><strong>${p.ticker}</strong><small>${p.name}</small></span></div></td><td><span class="side-badge">LONG</span></td><td class="numeric">${p.qty}</td><td class="numeric">${currency.format(p.entry)}<small>→ ${currency.format(instruments[p.ticker].price)}</small></td><td class="numeric">${currency.format(p.stop)}</td><td class="numeric ${cls}">${sign}${currency.format(Math.abs(p.pnl))} ₽<small class="${cls}">${p.pct}</small></td><td><span class="signal-badge">${p.signal}</span><small>Вход ${p.time} МСК</small></td></tr>`;
  }).join("") : '<tr><td colspan="7" class="empty-row">Позиции по этому запросу не найдены</td></tr>';
  $("position-count").textContent = search ? `Показано ${shown.length} из ${positions.length}` : "3 позиции";
}

function renderEvents() {
  $("events-list").innerHTML = events.filter((e) => state.events === "all" || e.kind === state.events).map((e) => `<article class="event"><span class="event-indicator ${e.tone || e.kind}" aria-hidden="true"></span><div><div class="event-top"><strong class="event-title">${e.title}</strong><time datetime="2026-10-15T${e.time}+03:00">${e.time}</time></div><p>${e.text}</p><span class="event-meta">${e.meta}</span></div></article>`).join("");
}

function selectGroup(button, selector) {
  document.querySelectorAll(selector).forEach((b) => {
    const active = b === button;
    b.classList.toggle("active", active);
    b.setAttribute("aria-pressed", String(active));
  });
}

function candlesFor(instrument, count) {
  const items = [];
  let last = instrument.price - instrument.span * 0.7;
  for (let i = 0; i < count; i++) {
    const progress = i / (count - 1);
    const drift = instrument.price - instrument.span * 0.7 + instrument.span * progress * 0.7;
    const oscillation = (Math.sin(i * 0.57) * 0.11 + Math.sin(i * 0.19) * 0.17 + Math.cos(i * 1.41) * 0.035) * instrument.span;
    const close = i === count - 1 ? instrument.price : drift + oscillation;
    const open = last;
    const extra = (0.025 + Math.abs(Math.sin(i * 3.1)) * 0.045) * instrument.span;
    items.push({ open, close, high: Math.max(open, close) + extra, low: Math.min(open, close) - extra, volume: 14 + Math.abs(Math.sin(i * 1.7)) * 23 + Math.abs(close - open) / instrument.span * 140 });
    last = close;
  }
  return items;
}

function renderChart() {
  const instrument = instruments[state.ticker];
  const svg = $("market-chart");
  const width = 1000;
  const right = 913;
  const left = 14;
  const top = 14;
  const bottom = 260;
  const count = { 1: 100, 5: 72, 15: 38, 60: 22 }[state.timeframe];
  let markup = '';
  const grid = (labels, map) => {
    for (let i = 0; i < 5; i++) {
      const value = labels[i];
      const y = map(value);
      markup += `<path d="M${left} ${y}H${right}" stroke="#ffffff09"/><text x="927" y="${y + 4}" fill="#a0a3ab" font-size="11" font-family="monospace">${state.chart === "equity" ? integer.format(value) : currency.format(value)}</text>`;
    }
    for (let i = 0; i < 6; i++) {
      const x = left + (right - left) * i / 5;
      markup += `<path d="M${x} ${top}V${bottom + 42}" stroke="#ffffff05"/><text x="${x}" y="331" text-anchor="${i === 0 ? 'start' : i === 5 ? 'end' : 'middle'}" fill="#a0a3ab" font-size="11" font-family="monospace">${["10:00", "10:55", "11:50", "12:45", "13:40", "14:35"][i]}</text>`;
    }
  };
  if (state.chart === "equity") {
    const points = Array.from({ length: 80 }, (_, i) => 123000 + i / 79 * 1386 + Math.sin(i * 0.19) * 110 + Math.sin(i * 0.78) * 24);
    points[0] = 123000;
    points[79] = 124386;
    const y = (p) => bottom - (p - 122800) / 1900 * (bottom - top);
    grid([124600, 124200, 123800, 123400, 123000], y);
    const line = points.map((p, i) => `${i === 0 ? "M" : "L"}${left + i / 79 * (right - left)},${y(p)}`).join(" ");
    markup += `<defs><linearGradient id="equity-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#6ed9ac" stop-opacity=".16"/><stop offset="100%" stop-color="#6ed9ac" stop-opacity="0"/></linearGradient></defs><path d="${line} L${right},${bottom} L${left},${bottom} Z" fill="url(#equity-fill)"/><path d="${line}" fill="none" stroke="#6ed9ac" stroke-width="2"/><circle cx="${right}" cy="${y(124386)}" r="4" fill="#6ed9ac"/><text x="${left + 8}" y="${top + 11}" fill="#a0a3ab" font-size="11">Капитал демо-портфеля · ₽ · модельный net</text>`;
    svg.setAttribute("aria-label", "Демонстрационная кривая капитала от 123 000 до 124 386 рублей. Не реальные результаты.");
    $("chart-context").textContent = "Весь портфель · демо-день";
    $("bar-count").textContent = "80 точек";
  } else {
    const data = candlesFor(instrument, count);
    const min = Math.min(...data.map((c) => c.low)) - instrument.span * 0.1;
    const max = Math.max(...data.map((c) => c.high)) + instrument.span * 0.1;
    const y = (p) => bottom - (p - min) / (max - min) * (bottom - top);
    grid(Array.from({ length: 5 }, (_, i) => max - i / 4 * (max - min)), y);
    const step = (right - left) / count;
    const barWidth = Math.min(9, step * .59);
    let ema = data[0].close;
    let emaPath = "";
    data.forEach((c, i) => {
      const x = left + i * step + step / 2;
      const color = c.close >= c.open ? "#76bda1" : "#c7888b";
      const bodyY = Math.min(y(c.open), y(c.close));
      const bodyHeight = Math.max(1.3, Math.abs(y(c.open) - y(c.close)));
      markup += `<path d="M${x} ${y(c.high)}V${y(c.low)}" stroke="${color}" stroke-width="1"/><rect x="${x - barWidth / 2}" y="${bodyY}" width="${barWidth}" height="${bodyHeight}" rx=".5" fill="${color}"/><rect x="${x - barWidth / 2}" y="${307 - c.volume * .58}" width="${barWidth}" height="${c.volume * .58}" fill="${color}" opacity=".22"/>`;
      ema += (c.close - ema) * 2 / 21;
      emaPath += `${i === 0 ? "M" : "L"}${x},${y(ema)} `;
    });
    const entryIndex = Math.floor(count * .53);
    const x = left + entryIndex * step + step / 2;
    const entryY = y(data[entryIndex].low) + 17;
    markup += `<path d="${emaPath}" fill="none" stroke="#d7bb82" stroke-width="1.4" opacity=".85"/><path d="M${left} ${y(instrument.price)}H${right}" stroke="#6ed9ac" opacity=".35" stroke-dasharray="4 4"/><rect x="${right + 3}" y="${y(instrument.price) - 10}" width="80" height="20" rx="3" fill="#314e40"/><text x="${right + 10}" y="${y(instrument.price) + 4}" fill="#bce7d0" font-size="11" font-family="monospace">${currency.format(instrument.price)}</text><path d="M${x} ${entryY - 9}l-4 7h8Z" fill="#6ed9ac"/><text x="${x}" y="${entryY + 13}" text-anchor="middle" fill="#6ed9ac" font-size="9" font-family="monospace">ВХОД · ДЕМО</text><text x="${left + 5}" y="290" fill="#a0a3ab" font-size="10" font-family="monospace">ОБЪЁМ</text>`;
    svg.setAttribute("aria-label", `Синтетический свечной график ${state.ticker}, визуальная демонстрация таймфрейма ${state.timeframe} минут. Не рыночные данные.`);
    $("chart-context").textContent = `${state.timeframe === 60 ? "1ч" : `${state.timeframe}м`} · демонстрация вида`;
    $("bar-count").textContent = `${count} баров`;
  }
  svg.setAttribute("viewBox", `0 0 ${width} 340`);
  svg.innerHTML = markup;
}

function updateInstrument() {
  const instrument = instruments[state.ticker];
  $("chart-title").textContent = state.ticker;
  $("instrument-name").textContent = instrument.name;
  $("ticker-logo").textContent = instrument.letter;
  $("ticker-logo").style.background = instrument.color;
  $("quote-value").textContent = instrument.decimals ? currency.format(instrument.price) : integer.format(instrument.price);
  $("quote-change").textContent = instrument.change;
  $("quote-change").className = instrument.change.startsWith("−") ? "negative" : "positive";
  renderChart();
}

const dialogs = {
  settings: { title: "Настройки представления", html: '<p>Изменения действуют только в этом макете до перезагрузки страницы.</p><label class="setting-row"><span>Компактная таблица<small>Меньше высота строк открытых позиций</small></span><input id="dense-setting" type="checkbox"></label><label class="setting-row"><span>Спокойный режим<small>Скрыть второстепенные подсказки</small></span><input id="quiet-setting" type="checkbox"></label><p class="dialog-note">Риск, счёт и торговые настройки здесь не изменяются.</p>' },
  research: { title: "Исследования", html: '<p>Предлагаемая структура экрана. Примеры ниже иллюстрируют организацию работы; это не статусы реальных запусков.</p><div class="dialog-row"><div>01 · Сочетания функций<small>Правило → сигналы → сделки → расходы</small></div><span class="subtle-badge">ПРИМЕР</span></div><div class="dialog-row"><div>02 · Экономический ML<small>Прогноз net-return и сравнение с базой</small></div><span class="subtle-badge">ПРИМЕР</span></div><div class="dialog-row"><div>03 · Дневной контекст<small>Прошлый тренд и отбор акций</small></div><span class="subtle-badge">ПРИМЕР</span></div><p class="dialog-note">Следующим этапом здесь появятся реальные отчёты с периодом, источником данных и статусом приёмки.</p>' },
  data: { title: "Состояние данных", html: '<p>Место для качества данных по каждому инструменту: покрытие, свежесть и пропуски отдельно от доступности соединения.</p><div class="dialog-row"><div>Покрытие истории<small>Какие интервалы доступны для исследования</small></div><span>Не подключено</span></div><div class="dialog-row"><div>Свежесть свечей<small>Время последнего закрытого бара</small></div><span>Не подключено</span></div><div class="dialog-row"><div>Качество и неизвестные исходы<small>Без подмены пропуска нулевым результатом</small></div><span>Не подключено</span></div><p class="dialog-note">Этот макет не запрашивает БД или API.</p>' },
  help: { title: "Новый вид Deeptrading", html: '<p>Демонстрационный торговый обзор в эстетике Solid Glass: графитовые поверхности, тонкие световые границы и цвет только для значимых состояний.</p><p>Можно выбирать инструмент, менять вид графика, фильтровать позиции и журнал, а также ставить демо-входы на паузу. Свечи и капитал созданы для иллюстрации; переключение таймфрейма показывает вариант рисунка, не агрегацию настоящих баров.</p><p>Рабочий интерфейс остаётся по прежнему адресу. Все элементы этого экрана изолированы от торговли.</p>' }
};

function openDialog(name) {
  const content = dialogs[name];
  if (!content) return;
  $("dialog-title").textContent = content.title;
  $("dialog-content").innerHTML = content.html;
  if (name === "settings") {
    $("dense-setting").checked = document.body.classList.contains("dense");
    $("quiet-setting").checked = document.body.classList.contains("quiet");
    $("dense-setting").addEventListener("change", (e) => document.body.classList.toggle("dense", e.target.checked));
    $("quiet-setting").addEventListener("change", (e) => document.body.classList.toggle("quiet", e.target.checked));
  }
  $("detail-dialog").showModal();
}

document.querySelectorAll("[data-dialog]").forEach((b) => b.addEventListener("click", () => openDialog(b.dataset.dialog)));
$("dialog-close").addEventListener("click", () => $("detail-dialog").close());
$("detail-dialog").addEventListener("click", (e) => {
  const bounds = $("detail-dialog").getBoundingClientRect();
  if (e.target === $("detail-dialog") && (e.clientX < bounds.left || e.clientX > bounds.right || e.clientY < bounds.top || e.clientY > bounds.bottom)) $("detail-dialog").close();
});
$("ticker-select").addEventListener("change", (e) => { state.ticker = e.target.value; updateInstrument(); });
$("position-search").addEventListener("input", renderPositions);
document.querySelectorAll("[data-chart]").forEach((b) => b.addEventListener("click", () => {
  state.chart = b.dataset.chart;
  selectGroup(b, "[data-chart]");
  document.querySelectorAll("[data-tf]").forEach((item) => { item.disabled = state.chart === "equity"; });
  document.querySelector(".chart-legend").style.visibility = state.chart === "equity" ? "hidden" : "visible";
  renderChart();
}));
document.querySelectorAll("[data-tf]").forEach((b) => b.addEventListener("click", () => { state.timeframe = Number(b.dataset.tf); selectGroup(b, "[data-tf]"); renderChart(); }));
document.querySelectorAll("[data-events]").forEach((b) => b.addEventListener("click", () => { state.events = b.dataset.events; selectGroup(b, "[data-events]"); renderEvents(); }));
$("pause-button").addEventListener("click", () => {
  state.paused = !state.paused;
  $("pause-button").setAttribute("aria-pressed", String(state.paused));
  $("pause-label").textContent = state.paused ? "Возобновить входы" : "Пауза входов";
  $("pause-icon").textContent = state.paused ? "▷" : "Ⅱ";
  $("bot-status").innerHTML = state.paused ? '<i class="dot amber"></i>Демо-входы на паузе' : '<i class="dot green"></i>Демо-бот активен';
  toast(state.paused ? "Демо: новые входы приостановлены. Позиции под защитой." : "Демо: новые входы возобновлены.");
});
document.querySelectorAll(".nav a").forEach((a) => a.addEventListener("click", () => {
  document.querySelectorAll(".nav a").forEach((item) => { item.classList.remove("selected"); item.removeAttribute("aria-current"); });
  a.classList.add("selected");
  a.setAttribute("aria-current", "location");
}));
renderPositions();
renderEvents();
updateInstrument();
