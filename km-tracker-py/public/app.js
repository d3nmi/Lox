/* ============================================================
   Клиентская логика: все данные приходят с локального сервера
   (Python + FastAPI + SQLite, см. /app). Здесь нет БД — только
   рендер и обращения к API.

   Один склад, но каждый браузер/ПК — своя "станция" (до 20 одновременно).
   ID станции запрашивается один раз и хранится в localStorage этого
   браузера — сервер использует его, чтобы не путать наборы разных
   станций между собой (см. app/validation.py).
   ============================================================ */
const API = '';
const STATION_KEY = 'km_station_id';

let STATION_ID = null;
let templates = [];        // [{id, kit_sku, kit_name, items:[...]}]
let stationState = null;   // последний известный state с сервера
let lastAcceptedCode = null; // код последнего УСПЕШНОГО скана (для демо-повтора дубля)

/* ---------- утилиты ---------- */
function shortName(name) { return (name || '').split(':')[0]; }
function fmtTime(t) { return (t || '').split(' ')[1] || t || ''; }
function rndSerial(len = 8) {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  let s = '';
  for (let i = 0; i < len; i++) s += chars[Math.floor(Math.random() * chars.length)];
  return s;
}
function rndBoxCode() {
  // Реальный формат: префикс DTV + 10 цифр, пример DTV0003111664
  const digits = String(Math.floor(Math.random() * 1e10)).padStart(10, '0');
  return `DTV${digits}`;
}
function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function beepError() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'square'; osc.frequency.value = 220;
    gain.gain.setValueAtTime(0.05, ctx.currentTime);
    osc.connect(gain); gain.connect(ctx.destination);
    osc.start(); osc.stop(ctx.currentTime + 0.12);
  } catch (e) { /* автоплей может быть заблокирован — не критично */ }
}

async function api(path, opts) {
  const res = await fetch(API + path, opts);
  setConn(true);
  if (!res.ok && res.status >= 500) setConn(false);
  return res.json();
}
function setConn(ok) {
  const dot = document.getElementById('conn-dot');
  const txt = document.getElementById('conn-text');
  dot.classList.toggle('down', !ok);
  txt.textContent = ok ? 'сервер на связи' : 'нет связи с сервером';
}

/* ============================================================
   ИДЕНТИФИКАЦИЯ СТАНЦИИ (один раз на браузер/ПК, хранится локально)
   ============================================================ */
function getSavedStationId() {
  try { return localStorage.getItem(STATION_KEY); } catch (e) { return null; }
}
function saveStationId(id) {
  try { localStorage.setItem(STATION_KEY, id); } catch (e) { /* приватный режим браузера — переживём и без сохранения */ }
}

function askStationId(prefill) {
  return new Promise((resolve) => {
    const backdrop = document.getElementById('station-modal-backdrop');
    const input = document.getElementById('station-input');
    const confirmBtn = document.getElementById('station-confirm');
    backdrop.style.display = 'flex';
    input.value = prefill || '';
    input.focus();

    function submit() {
      const val = input.value.trim();
      if (!val) { input.focus(); return; }
      backdrop.style.display = 'none';
      confirmBtn.removeEventListener('click', submit);
      input.removeEventListener('keydown', onKey);
      resolve(val);
    }
    function onKey(e) { if (e.key === 'Enter') submit(); }
    confirmBtn.addEventListener('click', submit);
    input.addEventListener('keydown', onKey);
  });
}

async function ensureStationId() {
  let id = getSavedStationId();
  if (!id) {
    id = await askStationId('');
    saveStationId(id);
  }
  STATION_ID = id;
  renderStationBadge();
}

function renderStationBadge() {
  document.getElementById('station-badge').textContent = `станция: ${STATION_ID}`;
}

document.getElementById('station-badge').addEventListener('click', async () => {
  const newId = await askStationId(STATION_ID);
  if (newId && newId !== STATION_ID) {
    saveStationId(newId);
    STATION_ID = newId;
    renderStationBadge();
    stationState = await api(`/api/stations/${encodeURIComponent(STATION_ID)}/state`);
    renderConsole({ focusInput: true });
  }
});

/* ============================================================
   ИНИЦИАЛИЗАЦИЯ
   ============================================================ */
async function init() {
  await ensureStationId();

  try {
    templates = await api('/api/templates');
    stationState = await api(`/api/stations/${encodeURIComponent(STATION_ID)}/state`);
  } catch (e) {
    setConn(false);
    document.getElementById('consoles-root').innerHTML =
      '<div class="empty">Не удалось подключиться к серверу. Проверьте, что сервер запущен и станция в той же сети, обновите страницу.</div>';
    return;
  }

  document.getElementById('consoles-root').innerHTML = '<div class="console" id="console-root"></div>';
  renderConsole({ focusInput: true });

  await refreshControl();
  await refreshReport();

  setInterval(pollState, 4000);
}

async function pollState() {
  try {
    stationState = await api(`/api/stations/${encodeURIComponent(STATION_ID)}/state`);
    renderConsole();
  } catch (e) { /* тихо пропускаем — индикатор связи уже покажет проблему */ }
}

/* ============================================================
   СКАНИРОВАНИЕ
   ============================================================ */
async function submitScan(code, opts = {}) {
  code = (code || '').trim();
  if (!code) return;
  let resp;
  try {
    resp = await api(`/api/stations/${encodeURIComponent(STATION_ID)}/scan`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code })
    });
  } catch (e) {
    setConn(false);
    return;
  }
  stationState = resp.state;
  if (resp.result === 'ok') lastAcceptedCode = code;
  renderConsole({
    lastEvent: { result: resp.result, message: resp.message },
    focusInput: opts.focusInput !== false
  });
  if (resp.result === 'error') {
    const el = document.getElementById('console-root');
    el.classList.add('flash-err');
    setTimeout(() => el.classList.remove('flash-err'), 350);
    beepError();
  }
  refreshControlStats(); // счётчики держим свежими постоянно, дерево — по запросу
}

/* ============================================================
   РЕНДЕР: КОНСОЛЬ СБОРКИ

   Скелет консоли (включая <input>) строится ОДИН РАЗ и больше не
   пересоздаётся — иначе пересборка DOM могла бы попасть в момент,
   когда USB-сканер ещё дописывает код в поле, и часть ввода терялась
   бы вместе с удалённым узлом. Дальнейшие рендеры обновляют только
   текст/классы конкретных под-элементов по id.
   ============================================================ */
function ensureConsoleSkeleton() {
  const root = document.getElementById('console-root');
  if (root.dataset.built === '1') return root;
  root.dataset.built = '1';
  root.innerHTML = `
    <div class="console-head">
      <div class="wh-title"><span class="wh-dot"></span>Рабочее место</div>
      <span class="badge" id="badge-collected">наборов собрано: 0</span>
    </div>
    <div class="console-body">
      <div class="scan-input-row">
        <input class="scan-input" id="input-station" placeholder="Скан → сюда (отправится автоматически)" autocomplete="off">
      </div>
      <div class="scan-hint" id="hint-station"></div>

      <div class="progress-row">
        <div class="progress-block">
          <div class="progress-label" id="kitlabel-station">Текущий набор</div>
          <div class="dots" id="dots-station"></div>
        </div>
        <div class="progress-block">
          <div class="progress-label">Короб (общий пул склада)</div>
          <div class="count-badge" id="boxcount-station"></div>
        </div>
        <div class="progress-block">
          <div class="progress-label">Паллета (общий пул склада)</div>
          <div class="count-badge" id="palletcount-station"></div>
        </div>
      </div>

      <div class="quickscan">
        <div class="quickscan-label">Тестовые коды (клик = скан сканером; серия каждый раз новая)</div>
        <div class="chip-row" id="chips-station"></div>
      </div>

      <div class="feed" id="feed-station"></div>
    </div>
  `;
  const input = root.querySelector('.scan-input');

  // Автоотправка скана без ручного подтверждения оператором:
  //  - если сканер сам шлёт Enter/Tab после кода (так делает большинство
  //    USB/Bluetooth HID-сканеров) — отправляем немедленно по Enter;
  //  - во всех остальных случаях (посимвольный ввод без Enter, вставка
  //    через Ctrl+V, программная вставка от ПО сканера/автоматизации) —
  //    отправляем сами, как только значение поля перестало меняться
  //    SCAN_IDLE_MS миллисекунд.
  //
  // Ловим изменение значения НЕ через события input/paste (некоторые
  // способы записи текста в поле — например, программный вызов от ПО
  // сканера или удалённого рабочего стола — меняют .value, но не всегда
  // порождают стандартные DOM-события), а прямым опросом value с малым
  // интервалом. Это работает всегда, независимо от того, как именно
  // текст оказался в поле.
  const SCAN_IDLE_MS = 150;
  const POLL_MS = 60;
  let idleTimer = null;
  let lastSeenValue = '';

  function submitFromInput() {
    clearTimeout(idleTimer);
    idleTimer = null;
    const val = input.value;
    lastSeenValue = '';
    if (!val.trim()) { input.value = ''; return; }
    submitScan(val, { focusInput: true });
    input.value = '';
  }

  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submitFromInput();
  });

  setInterval(() => {
    const v = input.value;
    if (v !== lastSeenValue) {
      lastSeenValue = v;
      clearTimeout(idleTimer);
      idleTimer = setTimeout(submitFromInput, SCAN_IDLE_MS);
    }
  }, POLL_MS);
  return root;
}

function renderConsole(opts = {}) {
  const { lastEvent, focusInput } = opts;
  const s = stationState;
  if (!s) return;
  ensureConsoleSkeleton();

  const k = s.currentKit;
  let hint = 'Отсканируйте первый товар набора';
  let hintClass = '';
  if (k && k.items_count < k.items_required) { hint = `Отсканируйте ещё товар (${k.items_count}/${k.items_required})`; }
  else if (k && k.items_count === k.items_required) { hint = 'Состав полон — отсканируйте агрегационный код набора'; hintClass = 'ok'; }
  else if (!k && s.kitsInBox > 0) { hint = 'Можно начать новый набор, либо (с любой станции) закрыть короб'; }
  if (lastEvent && lastEvent.result === 'error') { hint = lastEvent.message; hintClass = 'err'; }

  const kitDots = k ? Array.from({ length: k.items_required }).map((_, i) =>
    `<span class="dot ${i < k.items_count ? 'filled' : ''}"></span>`).join('') : '';

  document.getElementById('badge-collected').textContent = `наборов собрано: ${s.kitsAssembled}`;

  const hintEl = document.getElementById('hint-station');
  hintEl.textContent = hint;
  hintEl.className = `scan-hint ${hintClass}`;

  document.getElementById('kitlabel-station').textContent = 'Текущий набор' + (k ? ' · ' + shortName(k.kit_name) : '');
  document.getElementById('dots-station').innerHTML = kitDots || '<span class="count-badge">— не начат —</span>';
  document.getElementById('boxcount-station').textContent = `${s.kitsInBox} наборов в пуле`;
  document.getElementById('palletcount-station').textContent = `${s.boxesOnPallet} коробов в пуле`;

  renderChips(k);

  const feedEl = document.getElementById('feed-station');
  feedEl.innerHTML = (s.feed || []).map(f => `
    <div class="feed-line ${f.result}">
      <span class="feed-time">${fmtTime(f.t)}</span><span class="feed-msg">${escapeHtml(f.message)}</span>
    </div>`).join('') || '<div class="feed-line info"><span class="feed-msg">Событий пока нет</span></div>';

  // Фокус переставляем ТОЛЬКО по осознанному действию (см. submitScan/init),
  // а не на каждом фоновом опросе — иначе он бы дёргался у оператора из-под курсора.
  if (focusInput) {
    const input = document.getElementById('input-station');
    if (input) input.focus({ preventScroll: true });
  }
}

function renderChips(currentKit) {
  const chipRow = document.getElementById('chips-station');
  if (!chipRow) return;
  chipRow.innerHTML = ''; // родитель персистентный — очистка перед перестройкой обязательна
  const addChip = (label, code, danger) => {
    const chip = document.createElement('div');
    chip.className = 'chip' + (danger ? ' danger' : '');
    chip.textContent = label;
    chip.title = code;
    chip.onclick = () => submitScan(code);
    chipRow.appendChild(chip);
    return code;
  };

  if (currentKit) {
    if (currentKit.items_count < currentKit.items_required) {
      const tpl = templates.find(t => t.kit_sku === currentKit.kit_sku);
      if (tpl) {
        const scannedCounts = {};
        (currentKit.items || []).forEach(it => { scannedCounts[it.item_sku] = (scannedCounts[it.item_sku] || 0) + 1; });
        tpl.items
          .filter(it => (scannedCounts[it.item_sku] || 0) < it.qty_required)
          .forEach(it => addChip(`Товар: ${shortName(it.item_name)}`, `01${it.item_sku}21${rndSerial()}`));
      }
    } else {
      const tpl = templates.find(t => t.kit_sku === currentKit.kit_sku);
      if (tpl) addChip(`Агрегат набора «${shortName(tpl.kit_name)}»`, `01${tpl.kit_sku}21${rndSerial()}`);
    }
  } else {
    templates.forEach(tpl => {
      addChip(`Начать: ${shortName(tpl.kit_name)}`, `01${tpl.items[0].item_sku}21${rndSerial()}`);
    });
  }
  addChip('Код короба', rndBoxCode());
  addChip('ШК паллеты', `PLT-${Date.now().toString(36).toUpperCase()}${rndSerial(3)}`);

  const dupChip = document.createElement('div');
  dupChip.className = 'chip danger' + (lastAcceptedCode ? '' : ' disabled');
  dupChip.textContent = '⚠ повторить последний ПРИНЯТЫЙ код (дубль)';
  if (lastAcceptedCode) {
    dupChip.title = lastAcceptedCode;
    dupChip.onclick = () => submitScan(lastAcceptedCode);
  } else {
    dupChip.title = 'Пока нечего повторять — не было ни одного успешного скана';
    dupChip.style.opacity = '0.4';
    dupChip.style.cursor = 'default';
  }
  chipRow.appendChild(dupChip);
}

/* ============================================================
   ВКЛАДКА КОНТРОЛЬ
   ============================================================ */
async function refreshControlStats() {
  const stats = await api('/api/stats');
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
  const tree = await api(`/api/tree?q=${encodeURIComponent(q)}`);
  const root = document.getElementById('tree-root');
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
  await refreshTree();
}

/* ============================================================
   ВКЛАДКА ОТЧЁТ
   ============================================================ */
async function refreshReport() {
  const data = await api('/api/report/preview?limit=300');
  const body = document.getElementById('report-body');
  const empty = document.getElementById('report-empty');
  const countEl = document.getElementById('report-count');

  if (data.rows.length === 0) {
    body.innerHTML = '';
    empty.style.display = 'block';
    countEl.textContent = '';
    return;
  }
  empty.style.display = 'none';
  body.innerHTML = data.rows.map(r => `<tr><td>${escapeHtml(r.kit_name)}</td><td>${escapeHtml(r.kit_agg_code)}</td><td>${escapeHtml(r.item_name)}</td><td>${escapeHtml(r.km_code)}</td></tr>`).join('');
  countEl.textContent = data.total > data.rows.length
    ? `Показаны последние ${data.rows.length} из ${data.total} строк. Полная выгрузка — кнопкой «Скачать CSV».`
    : `Всего строк: ${data.total}.`;
}

document.getElementById('export-btn').addEventListener('click', () => {
  window.location.href = '/api/export.csv';
});

/* ============================================================
   НАВИГАЦИЯ
   ============================================================ */
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    document.getElementById('view-' + btn.dataset.view).classList.add('active');
    if (btn.dataset.view === 'control') refreshControl();
    if (btn.dataset.view === 'report') refreshReport();
  });
});
document.getElementById('tree-refresh').addEventListener('click', refreshTree);
let searchDebounce;
document.getElementById('search-input').addEventListener('input', () => {
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(refreshTree, 250);
});

init();
