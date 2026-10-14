/* ============================================================
   Страница «Контроль» (отдельная ссылка /control; из интерфейса станций убрана).
   Вкладки: Статистика | Список кодов | Короба и паллеты. Только чтение — данные не меняет.
   ============================================================ */
const $ = (id) => document.getElementById(id);
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function shortName(name) { return (name || '').split(':')[0]; }
function hhmm(t) { const x = (t || '').split(' ')[1] || ''; return x.slice(0, 5); }
function fmtDuration(sec) {                  // секунды -> «м:сс»
  if (sec == null) return '—';
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, '0')}`;
}
function fmtWork(sec) {                      // секунды -> «1 ч 05 мин»
  if (!sec) return '—';
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  return h ? `${h} ч ${String(m).padStart(2, '0')} мин` : `${m} мин`;
}
function num(v) { return v == null ? '—' : String(v).replace('.', ','); }
/* код для показа: настоящий GS выделяется как [GS] (в данных остаётся настоящий символ) */
function codeHtml(code) {
  return String(code == null ? '' : code).split(KmUtils.GS).map(esc).join('<span class="gs">[GS]</span>');
}
async function api(path) {
  try {
    const res = await fetch(path);
    if (!res.ok) { console.error('Ошибка запроса', res.status, path); return null; }
    return await res.json();
  } catch (e) { console.error('Нет связи', path); return null; }
}

/* ---------------- вкладки ---------------- */
let currentTab = 'stats';
function showTab(name) {
  if (!['stats', 'codes', 'tree'].includes(name)) name = 'stats';
  currentTab = name;
  document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.id === 'tab-' + name));
  history.replaceState(null, '', '#' + name);
  refreshCurrent();
  if (name === 'codes') $('code-search').focus();
}
document.querySelectorAll('#tabs button').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));

function refreshCurrent() {
  if (currentTab === 'stats') refreshStats();
  else if (currentTab === 'codes') refreshCodes();
  else refreshTree();
}

/* ---------------- СТАТИСТИКА ---------------- */
function localToday() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}
function kpi(label, value, sub, cls) {
  return `<div class="kpi"><div class="kpi-label">${label}</div><div class="kpi-num ${cls || ''}">${value}</div><div class="kpi-sub">${sub || ''}</div></div>`;
}

async function refreshStats() {
  const day = $('day').value;
  const [data, all] = await Promise.all([
    api('/api/stats/stations' + (day ? `?date=${encodeURIComponent(day)}` : '')),
    api('/api/stats'),
  ]);
  if (all) {
    $('totals').innerHTML = `За всё время: наборов закрыто <b>${all.kitsClosed}</b> · в сборке <b>${all.kitsOpen}</b> · ` +
      `коробов <b>${all.boxesClosed}</b> · паллет <b>${all.palletsClosed}</b> · товаров <b>${all.itemsScanned}</b>`;
  }
  if (!data || !Array.isArray(data.stations)) {
    $('kpis').innerHTML = '<div class="empty">Не удалось загрузить статистику. Нажмите «Обновить».</div>';
    return;
  }
  const t = data.total;
  const when = data.is_today ? 'сегодня' : data.date;
  $('stations-date').textContent = data.is_today ? '· сегодня' : `· ${data.date}`;
  $('kpis').innerHTML =
    kpi('Наборов собрано', t.kits, when) +
    kpi('Товаров отсканировано', t.items, when) +
    kpi('Наборов в час', num(t.kits_per_hour), 'в среднем на один стол') +
    kpi('Товаров в час', num(t.items_per_hour), 'в среднем на один стол') +
    kpi('Ошибок при сканировании', t.errors, `${num(t.error_pct)}% от ${t.scans} сканов`, t.error_pct >= 10 ? 'err' : '') +
    kpi('Столов работало', t.stations, `время работы: ${fmtWork(t.active_sec)}`);
  $('rate-note').textContent = `Скорость считается по времени реальной работы: паузы между сканами дольше ${data.idle_gap_min} минут ` +
    `(перерывы, простои) не учитываются. Пока стол отработал меньше ${data.idle_gap_min} минут, скорость не показывается.`;

  const body = $('stations-body');
  if (!data.stations.length) {
    body.innerHTML = `<tr><td colspan="9" class="empty">${data.is_today ? 'Сегодня сканирований ещё не было.' : 'В этот день сканирований не было.'}</td></tr>`;
  } else {
    const maxK = Math.max(0, ...data.stations.map(d => d.kits_per_hour || 0));
    const maxI = Math.max(0, ...data.stations.map(d => d.items_per_hour || 0));
    const bar = (v, max, cls) => v == null ? '<span class="dim">—</span>'
      : `<div class="cellbar ${cls}"><span>${num(v)}</span><i style="width:${max ? Math.round(100 * v / max) : 0}%"></i></div>`;
    body.innerHTML = data.stations.map(d =>
      `<tr><td class="name">${esc(d.station_id)}</td>` +
      `<td class="num">${d.kits}</td><td class="num">${d.items}</td>` +
      `<td class="num">${bar(d.kits_per_hour, maxK, '')}</td><td class="num">${bar(d.items_per_hour, maxI, 'items')}</td>` +
      `<td class="num">${fmtDuration(d.median_kit_sec)}</td><td class="num">${fmtWork(d.active_sec)}</td>` +
      `<td class="num ${d.error_pct >= 10 ? 'bad' : ''}">${d.errors} <span class="dim">(${num(d.error_pct)}%)</span></td>` +
      `<td class="dim">${hhmm(d.first)} – ${hhmm(d.last)}</td></tr>`).join('') +
      `<tr class="total"><td>Всего</td><td class="num">${t.kits}</td><td class="num">${t.items}</td>` +
      `<td class="num">${num(t.kits_per_hour)}</td><td class="num">${num(t.items_per_hour)}</td>` +
      `<td></td><td class="num">${fmtWork(t.active_sec)}</td><td class="num">${t.errors}</td><td></td></tr>`;
  }

  const hb = $('hours-box');
  if (!data.hourly.length) {
    hb.innerHTML = `<div class="hours"><div class="empty" style="grid-column:1/-1;">Нет данных по часам.</div></div>`;
  } else {
    const maxKits = Math.max(1, ...data.hourly.map(h => h.kits));
    const maxItems = Math.max(1, ...data.hourly.map(h => h.items));
    const line = (v, max, cls) =>
      `<div class="bar-line ${cls}"><div class="bar" style="width:${v ? Math.max(2, Math.round(100 * v / max)) : 0}%;max-width:calc(100% - 70px)"></div>` +
      `<span class="val ${v ? '' : 'zero'}">${v}</span></div>`;
    hb.innerHTML = `<div class="hours"><div class="h-head">Час</div><div class="h-head">Наборов</div><div class="h-head">Товаров</div>` +
      data.hourly.map(h => {
        const a = String(h.hour).padStart(2, '0'), b = String((h.hour + 1) % 24).padStart(2, '0');
        return `<div class="h-time">${a}:00–${b}:00</div>${line(h.kits, maxKits, '')}${line(h.items, maxItems, 'items')}`;
      }).join('') + `</div>`;
  }
}
$('day').max = localToday();
$('day').addEventListener('change', refreshStats);
$('day-today').addEventListener('click', () => { $('day').value = ''; refreshStats(); });
$('stats-refresh').addEventListener('click', refreshStats);

/* ---------------- СПИСОК КОДОВ ---------------- */
const TYPE_NAMES = { item: 'Товар', kit_agg: 'Набор (агрегат)', box_agg: 'Короб', pallet: 'Паллета', unknown: '—' };
async function refreshCodes() {
  const q = $('code-search').value;
  const errors = $('only-errors').checked ? 1 : 0;
  const data = await api(`/api/codes?q=${encodeURIComponent(q)}&errors=${errors}&limit=200`);
  const body = $('codes-body');
  if (!data || !Array.isArray(data.rows)) {
    body.innerHTML = '<tr><td colspan="7" class="empty">Не удалось загрузить список. Нажмите «Обновить».</td></tr>';
    $('codes-found').textContent = '';
    return;
  }
  $('codes-found').textContent = data.total === 0 ? 'Ничего не найдено.'
    : (data.total > data.rows.length ? `Найдено: ${data.total}. Показаны последние ${data.rows.length}.` : `Найдено: ${data.total}.`);
  if (!data.rows.length) {
    body.innerHTML = `<tr><td colspan="7" class="empty">${q ? 'По вашему запросу ничего нет.' : 'Сканирований пока не было.'}</td></tr>`;
    return;
  }
  body.innerHTML = data.rows.map(r => {
    const isErr = r.result === 'error';
    const kit = r.kit_no == null ? '<span class="dim">—</span>'
      : `№${esc(r.kit_no)} <span class="pill ${r.kit_status === 'open' ? 'open' : ''}">${r.kit_status === 'open' ? 'в сборке' : 'закрыт'}</span>`;
    return `<tr class="${isErr ? 'err-row' : ''}">` +
      `<td class="dim" style="white-space:nowrap;">${esc((r.created_at || '').slice(5, 16))}</td>` +
      `<td>${esc(r.station_id)}</td>` +
      `<td class="code">${r.code ? codeHtml(r.code) : '<span class="dim">(пусто)</span>'}</td>` +
      `<td>${esc(TYPE_NAMES[r.code_type] || '—')}</td>` +
      `<td><span class="pill ${isErr ? 'err' : 'ok'}">${isErr ? 'ошибка' : 'принят'}</span>` +
      (isErr ? `<span class="msg">${codeHtml(r.message)}</span>` : '') + `</td>` +
      `<td>${kit}</td>` +
      `<td class="code">${r.box_code ? esc(r.box_code) : '<span class="dim">—</span>'}</td></tr>`;
  }).join('');
}
let codesDebounce;
$('code-search').addEventListener('input', () => { clearTimeout(codesDebounce); codesDebounce = setTimeout(refreshCodes, 250); });
$('only-errors').addEventListener('change', refreshCodes);
$('codes-refresh').addEventListener('click', refreshCodes);

/* ---------------- КОРОБА И ПАЛЛЕТЫ (дерево) ---------------- */
async function refreshTree() {
  const q = ($('search-input').value || '').trim();
  const root = $('tree-root');
  const tree = await api(`/api/tree?q=${encodeURIComponent(q)}`);
  if (!Array.isArray(tree)) { root.innerHTML = '<div class="empty">Не удалось загрузить данные. Нажмите «Обновить».</div>'; return; }
  if (!tree.length) { root.innerHTML = `<div class="empty">${q ? 'Ничего не найдено по запросу.' : 'Пока пусто. Начните сканирование на станции.'}</div>`; return; }
  root.innerHTML = tree.map(nodeHtml).join('');
}
function nodeHtml(node) {
  if (node.type === 'item') {
    return `<div class="item-row">${esc(node.name)} — <span class="tcode">${codeHtml(node.code)}</span></div>`;
  }
  const children = (node.children || []).map(nodeHtml).join('');
  if (node.type === 'kit') {
    const open = node.status === 'open' ? `<span class="badge open">в сборке ${node.items_count}/${node.items_required}</span>` : '';
    return `<details class="kit"><summary>🧩 Набор «${esc(shortName(node.name))}» ${node.code ? `— <span class="tcode">${codeHtml(node.code)}</span>` : ''} <span class="badge">станция ${esc(node.station_id)}</span>${open}</summary>${children}</details>`;
  }
  if (node.type === 'box') {
    return `<details class="box"><summary>📦 Короб <span class="tcode">${esc(node.code)}</span> <span class="badge">${node.kits_count} наб.</span> <span class="badge">закрыт со станции ${esc(node.station_id)}</span></summary>${children}</details>`;
  }
  if (node.type === 'pallet') {
    return `<details class="pallet"><summary>🟧 Паллета <span class="tcode">${esc(node.code)}</span> <span class="badge">${node.boxes_count} короб.</span></summary>${children}</details>`;
  }
  return '';
}
let treeDebounce;
$('search-input').addEventListener('input', () => { clearTimeout(treeDebounce); treeDebounce = setTimeout(refreshTree, 250); });
$('tree-refresh').addEventListener('click', refreshTree);

/* ---------------- запуск и автообновление ---------------- */
showTab((location.hash || '#stats').slice(1));
let tick = 0;
setInterval(() => {
  tick++;
  if (currentTab === 'codes') refreshCodes();             // список — каждые 10 с
  else if (currentTab === 'stats' && tick % 3 === 0) refreshStats();   // статистика — каждые 30 с
}, 10000);
