/* ============================================================
   Страница «Контроль» (отдельная ссылка /control, из интерфейса станций
   убрана): общая статистика, работа столов за сегодня, дерево кодов
   и поиск. Только чтение — ничего в данных не меняет.
   ============================================================ */
function shortName(name) { return (name || '').split(':')[0]; }
function fmtTime(t) { return (t || '').split(' ')[1] || t || ''; }
function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
async function api(path) {
  const res = await fetch(path);
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) { console.error('Ошибка запроса', res.status, path); return null; }
  return data;
}

/* ============================================================
   ВКЛАДКА КОНТРОЛЬ
   ============================================================ */
async function refreshControlStats() {
  let stats = null;
  try { stats = await api('/api/stats'); } catch (e) { stats = null; }
  if (!stats) return;
  const stat = document.getElementById('stat-strip');
  stat.innerHTML = `
    <div class="stat"><div class="stat-num">${stats.itemsScanned}</div><div class="stat-label">Товаров</div></div>
    <div class="stat"><div class="stat-num">${stats.kitsClosed}</div><div class="stat-label">Наборов закрыто</div></div>
    <div class="stat"><div class="stat-num">${stats.kitsOpen}</div><div class="stat-label">Наборов в сборке</div></div>
    <div class="stat"><div class="stat-num">${stats.boxesClosed}</div><div class="stat-label">Коробов</div></div>
    <div class="stat"><div class="stat-num">${stats.palletsClosed}</div><div class="stat-label">Паллет</div></div>
    <div class="stat"><div class="stat-num">${stats.kitsClosedToday}</div><div class="stat-label">Наборов сегодня</div></div>
    <div class="stat"><div class="stat-num">${stats.activeStationsToday}</div><div class="stat-label">Станций сегодня</div></div>
    <div class="stat"><div class="stat-num" style="color:${stats.errorsToday > 0 ? 'var(--err)' : 'var(--text)'}">${stats.errorsToday}</div><div class="stat-label">Ошибок сегодня</div></div>
  `;
}

async function refreshTree() {
  const q = (document.getElementById('search-input').value || '').trim();
  const root = document.getElementById('tree-root');
  let tree;
  try {
    tree = await api(`/api/tree?q=${encodeURIComponent(q)}`);
  } catch (e) { tree = null; }
  if (!Array.isArray(tree)) {
    root.innerHTML = '<div class="empty">Не удалось загрузить данные. Нажмите «Обновить» ещё раз.</div>';
    return;
  }
  if (tree.length === 0) {
    root.innerHTML = `<div class="empty">${q ? 'Ничего не найдено по запросу.' : 'Пока пусто. Начните сканирование во вкладке «Сборка».'}</div>`;
    return;
  }
  root.innerHTML = tree.map(nodeHtml).join('');
}

function nodeHtml(node) {
  if (node.type === 'item') {
    return `<div class="item-row">${escapeHtml(node.name)} — <span class="code">${escapeHtml(node.code)}</span></div>`;
  }
  const childrenHtml = (node.children || []).map(nodeHtml).join('');
  if (node.type === 'kit') {
    const openBadge = node.status === 'open' ? `<span class="badge open">в сборке ${node.items_count}/${node.items_required}</span>` : '';
    return `<details class="kit"><summary>🧩 Набор «${escapeHtml(shortName(node.name))}» ${node.code ? `— <span class="code">${escapeHtml(node.code)}</span>` : ''} <span class="badge">станция ${escapeHtml(node.station_id)}</span>${openBadge}</summary>${childrenHtml}</details>`;
  }
  if (node.type === 'box') {
    return `<details class="box"><summary>📦 Короб <span class="code">${escapeHtml(node.code)}</span> <span class="badge">${node.kits_count} наб.</span> <span class="badge">закрыт со станции ${escapeHtml(node.station_id)}</span></summary>${childrenHtml}</details>`;
  }
  if (node.type === 'pallet') {
    return `<details class="pallet"><summary>🟧 Паллета <span class="code">${escapeHtml(node.code)}</span> <span class="badge">${node.boxes_count} короб.</span></summary>${childrenHtml}</details>`;
  }
  return '';
}

async function refreshControl() {
  await refreshControlStats();
  await refreshStationStats();
  await refreshTree();
}

/* ============================================================
   СТАТИСТИКА ПО СТОЛАМ (вкладка «Контроль»)
   ============================================================ */
function fmtDuration(sec) {
  if (sec == null) return '—';
  const m = Math.floor(sec / 60), s = sec % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}
async function refreshStationStats() {
  const body = document.getElementById('stations-body');
  const note = document.getElementById('stations-note');
  let data = null;
  try { data = await api('/api/stats/stations'); } catch (e) { data = null; }
  if (!data || !Array.isArray(data.stations)) {
    body.innerHTML = '';
    note.textContent = 'Не удалось загрузить статистику. Нажмите «Обновить».';
    return;
  }
  document.getElementById('stations-date').textContent = data.date || '';
  if (data.stations.length === 0) {
    body.innerHTML = '';
    note.textContent = 'Сегодня сканирований ещё не было.';
    return;
  }
  note.textContent = 'Время на набор — типичное (медиана) время от первого скана до закрытия набора. «Наборов/час» считается по времени между первым и последним сканом стола.';
  const rows = data.stations.map(d =>
    `<tr><td>${escapeHtml(d.station_id)}</td><td>${d.kits}</td><td>${d.items}</td><td>${d.scans}</td>` +
    `<td>${d.errors}</td><td class="${d.error_pct >= 10 ? 'err-high' : ''}">${d.error_pct}%</td>` +
    `<td>${fmtDuration(d.median_kit_sec)}</td><td>${d.kits_per_hour == null ? '—' : d.kits_per_hour}</td>` +
    `<td>${d.first ? fmtTime(d.first).slice(0, 5) : '—'}</td><td>${d.last ? fmtTime(d.last).slice(0, 5) : '—'}</td></tr>`);
  const t = data.total;
  rows.push(`<tr class="total"><td>Всего</td><td>${t.kits}</td><td>${t.items}</td><td>${t.scans}</td><td>${t.errors}</td><td></td><td></td><td></td><td></td><td></td></tr>`);
  body.innerHTML = rows.join('');
}

document.getElementById('tree-refresh').addEventListener('click', () => { refreshControl(); });
let searchDebounce;
document.getElementById('search-input').addEventListener('input', () => {
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(refreshTree, 250);
});

refreshControl();
setInterval(() => { refreshControlStats(); refreshStationStats(); }, 30000);  // числа обновляются сами
