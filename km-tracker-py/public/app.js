/* ============================================================
   Клиентская логика: все данные приходят с локального сервера
   (Python + FastAPI + SQLite, см. /app). Здесь нет БД — только
   рендер и обращения к API.

   Один склад, но каждый браузер/ПК — своя "станция" (до 20 одновременно).
   ID станции запрашивается один раз и хранится в localStorage этого
   браузера — сервер использует его, чтобы не путать наборы разных
   станций между собой (см. app/validation.py).

   Порядок работы оператора: выбрать набор в списке → сканировать состав
   (позиции в чек-листе из серых становятся зелёными) → агрегационный код набора.
   Дальше — по галочке «Код короба» (хранится на сервере для станции):
     включена  → после агрегационного кода сканируется код короба DTV…;
     выключена → набор закрывается автоматически, короб сканировать не нужно.
   Номер набора в отчёте присваивается сразу при закрытии набора.
   Выбор набора хранится на сервере и сохраняется после закрытия набора.
   ============================================================ */
const API = '';
const STATION_KEY = 'km_station_id';
const FAKE_AGG_GTIN = '09999999999990'; // для тестовой кнопки агрегата набора без GTIN в справочнике

let STATION_ID = null;
let templates = [];        // [{kit_code, kit_sku, kit_name, ready, missing, items:[...]}]
let stationState = null;   // последний известный state с сервера
let lastAcceptedCode = null; // код последнего УСПЕШНОГО скана GS1 (для демо-повтора дубля)

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
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
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
   ВЫБОР НАБОРА, РЕЖИМ «КОД КОРОБА» И СКАНИРОВАНИЕ
   ============================================================ */
async function selectKit(kitCode) {
  let resp;
  try {
    resp = await api(`/api/stations/${encodeURIComponent(STATION_ID)}/select-kit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kit_code: kitCode })
    });
  } catch (e) {
    setConn(false);
    return;
  }
  stationState = resp.state;
  renderConsole({
    lastEvent: resp.result === 'error' ? { result: 'error', message: resp.message } : undefined,
    focusInput: true
  });
}

async function setRequireBox(on) {
  let resp;
  try {
    resp = await api(`/api/stations/${encodeURIComponent(STATION_ID)}/settings`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ require_box: !!on })
    });
  } catch (e) {
    setConn(false);
    return;
  }
  stationState = resp.state;
  renderConsole({ focusInput: true });
}

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
  if (resp.result === 'ok' && /^01\d{14}/.test(code)) lastAcceptedCode = code;
  renderConsole({
    lastEvent: { result: resp.result, message: resp.message, code_type: resp.code_type },
    focusInput: opts.focusInput !== false
  });
  const el = document.getElementById('console-root');
  if (resp.result === 'error') {
    el.classList.add('flash-err');
    setTimeout(() => el.classList.remove('flash-err'), 350);
    beepError();
  } else if (resp.code_type === 'kit_agg') {
    el.classList.add('flash-ok');
    setTimeout(() => el.classList.remove('flash-ok'), 700);
  }
  refreshControlStats(); // счётчики держим свежими постоянно, дерево — по запросу
}

/* ============================================================
   РЕНДЕР: КОНСОЛЬ СБОРКИ

   Скелет консоли (включая <input>) строится ОДИН РАЗ и больше не
   пересоздаётся — иначе пересборка DOM могла бы попасть в момент,
   когда USB-сканер ещё дописывает код в поле, и часть ввода терялась
   бы вместе с удалённым узлом. Дальнейшие рендеры обновляют только
   содержимое конкретных под-элементов по id (список наборов, чек-лист,
   ленту), сам <input> не трогается.
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
      <label id="box-toggle" style="display:flex;align-items:center;gap:9px;margin-bottom:16px;cursor:pointer;font-size:14px;"
             title="Включено: в конце набора сканируется код короба DTV… Выключено: набор закрывается автоматически">
        <input type="checkbox" id="chk-require-box" style="width:17px;height:17px;accent-color:var(--accent);cursor:pointer;">
        <span>Код короба <span style="color:var(--text-dim);font-size:12.5px;">— сканировать в конце набора (DTV…)</span></span>
      </label>

      <div class="section-label">Набор для сборки (авто — по КМ, либо выбрать вручную)</div>
      <div class="kit-picker" id="kit-picker"></div>

      <div class="section-label" id="checklist-label">Что нужно отсканировать</div>
      <div class="checklist" id="checklist"></div>

      <div class="scan-input-row">
        <input class="scan-input" id="input-station" placeholder="Скан → сюда (отправится автоматически)" autocomplete="off">
      </div>
      <div class="scan-hint" id="hint-station"></div>

      <div class="progress-row">
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

  // Галочка «Код короба» — сохраняется на сервере для этой станции
  root.querySelector('#chk-require-box').addEventListener('change', (e) => setRequireBox(e.target.checked));

  // Выбор набора — делегирование клика (карточки перерисовываются при каждом опросе)
  root.querySelector('#kit-picker').addEventListener('click', (e) => {
    const card = e.target.closest('.kit-card');
    if (!card) return;
    if (card.classList.contains('disabled')) {
      renderConsole({ lastEvent: { result: 'error', message: card.dataset.reason || 'Этот набор сейчас выбрать нельзя' }, focusInput: true });
      return;
    }
    if (card.classList.contains('active') || card.classList.contains('detected')) { renderConsole({ focusInput: true }); return; }
    selectKit(card.dataset.code);
  });

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

function renderKitPicker() {
  const picker = document.getElementById('kit-picker');
  if (!picker) return;
  const s = stationState;
  const sel = s.selectedKit;
  const auto = s.mode === 'auto';
  const locked = !!(s.currentKit && !s.currentKit.pending);
  const detectedCode = (auto && locked && sel) ? sel.kit_code : null;

  const autoCard =
    `<div class="kit-card auto${auto ? ' active' : ''}" role="button" data-code="auto" title="Набор определяется по первому отсканированному КМ">` +
    `<span class="kit-card-code">АВТО</span><span>Определять набор по сканированию КМ</span>` +
    (detectedCode ? '<span class="kit-card-note">набор определён — см. ниже</span>' : '') +
    `</div>`;

  const cards = templates.map(t => {
    const pinned = !auto && !!sel && sel.kit_code === t.kit_code;
    const detected = detectedCode === t.kit_code;
    let reason = '';
    if (!t.ready) reason = `Набор пока нельзя собирать — в справочнике не заполнены GTIN (${t.missing.length} поз.)`;
    else if (locked && !pinned && !detected) reason = 'Сначала завершите набор, который уже начат на этой станции';
    const disabled = !!reason;
    return `<div class="kit-card${pinned ? ' active' : ''}${detected ? ' detected' : ''}${disabled ? ' disabled' : ''}" role="button" data-code="${escapeHtml(t.kit_code)}"` +
      ` data-reason="${escapeHtml(reason)}" title="${escapeHtml(reason)}">` +
      `<span class="kit-card-code">${escapeHtml(t.kit_code)}${detected ? ' · определён' : ''}</span>` +
      `<span>${escapeHtml(t.kit_name)}</span>` +
      (!t.ready ? '<span class="kit-card-note">нет GTIN в справочнике</span>' : '') +
      `</div>`;
  }).join('');
  picker.innerHTML = autoCard + (cards || '<div class="check-empty">Справочник наборов пуст</div>');
}

function renderChecklist() {
  const el = document.getElementById('checklist');
  const label = document.getElementById('checklist-label');
  if (!el) return;
  const s = stationState;
  const sel = s.selectedKit;

  // набор ещё не определён: показываем принятое и варианты
  if (s.pending) {
    label.textContent = 'Набор определяется по сканированию';
    const rows = s.pending.scanned.map(it =>
      `<div class="check-row done"><span class="check-mark">✓</span><span class="check-name">${escapeHtml(it.item_name)}</span>` +
      `<span class="check-tag">${it.marked ? 'КМ' : 'ШК'}</span><span class="check-count"></span></div>`);
    const cands = s.pending.candidates.map(c => escapeHtml(c.kit_name)).join('<br>');
    rows.push(`<div class="check-empty">Эта позиция входит в несколько наборов. Отсканируйте следующую — набор определится сам.<br><br>Возможные наборы:<br>${cands}</div>`);
    el.innerHTML = rows.join('');
    return;
  }

  if (!sel) {
    label.textContent = 'Что нужно отсканировать';
    el.innerHTML = '<div class="check-empty">Отсканируйте КМ любого товара — набор определится автоматически. ' +
      'Либо выберите набор вручную в списке выше.</div>';
    return;
  }
  label.textContent = 'Что нужно отсканировать · ' + sel.kit_name;
  const rows = (s.checklist || []).map(it => {
    const done = it.scanned >= it.required;
    const partial = !done && it.scanned > 0;
    return `<div class="check-row${done ? ' done' : ''}${partial ? ' partial' : ''}">` +
      `<span class="check-mark">${done ? '✓' : ''}</span>` +
      `<span class="check-name">${escapeHtml(it.item_name)}</span>` +
      `<span class="check-tag" title="${it.marked ? 'Товар с кодом маркировки' : 'Упаковка без КМ — штрихкод или артикул'}">${it.marked ? 'КМ' : 'ШК'}</span>` +
      `<span class="check-count">${it.scanned}/${it.required}</span>` +
      `</div>`;
  });
  rows.push(
    `<div class="check-row agg${sel.agg_ready ? ' ready' : ''}">` +
    `<span class="check-mark"></span>` +
    `<span class="check-name">Агрегационный код набора</span>` +
    `<span class="check-tag">КМ</span><span class="check-count"></span></div>`
  );
  if (s.requireBox) {
    rows.push(
      `<div class="check-row agg${s.awaitingBox ? ' ready' : ''}">` +
      `<span class="check-mark"></span>` +
      `<span class="check-name">Код короба</span>` +
      `<span class="check-tag">DTV</span><span class="check-count"></span></div>`
    );
  }
  el.innerHTML = rows.join('');
}

function renderConsole(opts = {}) {
  const { lastEvent, focusInput } = opts;
  const s = stationState;
  if (!s) return;
  ensureConsoleSkeleton();

  const chk = document.getElementById('chk-require-box');
  if (chk) chk.checked = !!s.requireBox;

  const k = s.currentKit;
  const sel = s.selectedKit;
  const left = (s.checklist || []).reduce((n, it) => n + Math.max(0, it.required - it.scanned), 0);
  let hint;
  let hintClass = '';
  if (s.pending) { hint = 'Набор определится по следующей позиции — отсканируйте КМ следующего товара'; }
  else if (s.awaitingBox) { hint = 'Набор закрыт — отсканируйте код короба (DTV…), затем можно начинать следующий'; hintClass = 'ok'; }
  else if (!k) {
    hint = sel ? 'Отсканируйте любую позицию набора' : 'Отсканируйте КМ любого товара — набор определится автоматически';
  }
  else if (k.items_count < k.items_required) { hint = `Осталось отсканировать позиций: ${left}`; }
  else { hint = 'Состав полон — отсканируйте агрегационный код набора'; hintClass = 'ok'; }
  if (lastEvent && lastEvent.result === 'error') { hint = lastEvent.message; hintClass = 'err'; }
  else if (lastEvent && lastEvent.result === 'ok' && lastEvent.code_type === 'kit_agg') { hint = lastEvent.message; hintClass = 'ok'; }

  document.getElementById('badge-collected').textContent = `наборов собрано: ${s.kitsAssembled}`;

  const hintEl = document.getElementById('hint-station');
  hintEl.textContent = hint;
  hintEl.className = `scan-hint ${hintClass}`;

  renderKitPicker();
  renderChecklist();

  document.getElementById('boxcount-station').textContent = `${s.kitsInBox} наборов в пуле`;
  document.getElementById('palletcount-station').textContent = `${s.boxesOnPallet} коробов в пуле`;

  renderChips();

  const feedEl = document.getElementById('feed-station');
  feedEl.innerHTML = (s.feed || []).map(f => `
    <div class="feed-line ${f.result}">
      <span class="feed-time">${fmtTime(f.t)}</span><span class="feed-msg">${escapeHtml(f.message)}</span>
    </div>`).join('') || '<div class="feed-line info"><span class="feed-msg">Событий пока нет</span></div>';

  // Фокус переставляем ТОЛЬКО по осознанному действию (см. submitScan/selectKit/init),
  // а не на каждом фоновом опросе — иначе он бы дёргался у оператора из-под курсора.
  if (focusInput) {
    const input = document.getElementById('input-station');
    if (input) input.focus({ preventScroll: true });
  }
}

function renderChips() {
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

  const s = stationState;
  const sel = s.selectedKit;
  const readyTpls = templates.filter(t => t.ready);

  // Из каких наборов предлагать тестовые коды: выбранный/определённый → он один;
  // набор определяется → варианты; иначе (авто, пусто) → все наборы справочника.
  let pool = readyTpls;
  if (s.pending) {
    const codes = s.pending.candidates.map(c => c.kit_code);
    pool = readyTpls.filter(t => codes.includes(t.kit_code));
  } else if (sel) {
    pool = readyTpls.filter(t => t.kit_code === sel.kit_code);
  }

  const scannedBySku = {};
  ((s.currentKit && s.currentKit.items) || []).forEach(it => { scannedBySku[it.item_sku] = (scannedBySku[it.item_sku] || 0) + 1; });

  if (s.awaitingBox) {
    // набор закрыт, ждём код короба — тестовых кнопок товаров не нужно
  } else if (sel && sel.agg_ready) {
    const tpl = pool[0];
    if (tpl) addChip(`Агрегат набора «${tpl.kit_name}»`, `01${tpl.kit_sku || FAKE_AGG_GTIN}21${rndSerial()}`);
  } else {
    const seen = new Set();
    pool.forEach(tpl => {
      tpl.items.forEach(it => {
        if (seen.has(it.item_sku)) return;       // один и тот же товар в нескольких наборах — одна кнопка
        seen.add(it.item_sku);
        if ((scannedBySku[it.item_sku] || 0) >= it.qty_required) return;
        if (it.marked) addChip(`КМ: ${it.item_name}`, `01${it.item_sku}21${rndSerial()}`);
        else addChip(`Упаковка: ${it.item_name}`, it.item_sku.replace(/^0/, ''));
      });
    });
  }
  addChip('Код короба', rndBoxCode());
  addChip('ШК паллеты', `PLT-${Date.now().toString(36).toUpperCase()}${rndSerial(3)}`);

  const dupChip = document.createElement('div');
  dupChip.className = 'chip danger' + (lastAcceptedCode ? '' : ' disabled');
  dupChip.textContent = '⚠ повторить последний ПРИНЯТЫЙ КМ (дубль)';
  if (lastAcceptedCode) {
    dupChip.title = lastAcceptedCode;
    dupChip.onclick = () => submitScan(lastAcceptedCode);
  } else {
    dupChip.title = 'Пока нечего повторять — не было ни одного успешного скана КМ';
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
  body.innerHTML = data.rows.map(r =>
    `<tr><td>${escapeHtml(r.kit_agg_code)}</td><td>${r.kit_no == null ? '—' : escapeHtml(r.kit_no)}</td>` +
    `<td>${escapeHtml(r.item_name)}</td><td>${escapeHtml(r.km_code)}</td></tr>`).join('');
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