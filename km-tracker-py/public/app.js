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

   ЭТАП 1 (production): удалены тестовые кнопки (чипы) и связанный с ними
   код; все запросы к серверу идут строго по одному (очередь) — нет
   повторного запуска операции, пока предыдущая не завершилась, и при этом
   ни один скан не теряется; ошибки сервера показываются понятным текстом.
   ============================================================ */
const API = '';
const STATION_KEY = 'km_station_id';
const GENERIC_ERROR = 'Ошибка на сервере. Повторите скан. Если ошибка повторяется — позовите администратора.';
const NO_CONNECTION = 'Нет связи с сервером. Скан НЕ принят — отсканируйте этот код ещё раз, когда связь восстановится.';

let STATION_ID = null;
let templates = [];        // [{kit_code, kit_sku, kit_name, ready, missing, items:[...]}]
let stationState = null;   // последний известный state с сервера

/* ---------- утилиты ---------- */
function shortName(name) { return (name || '').split(':')[0]; }
function fmtTime(t) { return (t || '').split(' ')[1] || t || ''; }
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

/* Запрос к серверу. Сетевой сбой — исключение (его ловит вызывающий код).
   Ответ сервера с ошибкой (500 и т.п.), не являющийся JSON, превращается
   в аккуратный объект-ошибку: пользователь не увидит технических деталей. */
async function api(path, opts) {
  const res = await fetch(API + path, opts);
  setConn(true);
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (res.status >= 500) {
    setConn(false);
    console.error('Ошибка сервера', res.status, path);
    return { result: 'error', message: GENERIC_ERROR, code_type: 'unknown' };
  }
  if (!res.ok) {
    console.error('Запрос отклонён', res.status, path, data);
    const msg = data && typeof data.detail === 'string' ? data.detail : GENERIC_ERROR;
    return { result: 'error', message: msg, code_type: 'unknown' };
  }
  return data;
}
function setConn(ok) {
  const dot = document.getElementById('conn-dot');
  const txt = document.getElementById('conn-text');
  dot.classList.toggle('down', !ok);
  txt.textContent = ok ? 'сервер на связи' : 'нет связи с сервером';
}

/* Очередь операций: действия выполняются СТРОГО ПО ОДНОМУ.
   Если сканер выдал два кода подряд, второй дождётся окончания первого —
   ничего не теряется и не перемешивается. */
let opQueue = Promise.resolve();
function enqueue(task) {
  const run = opQueue.then(task, task);
  opQueue = run.catch(() => {});
  return run;
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
    try {
      stationState = await api(`/api/stations/${encodeURIComponent(STATION_ID)}/state`);
      renderConsole({ focusInput: true });
    } catch (e) { setConn(false); }
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
    if (!Array.isArray(templates) || !stationState || stationState.result === 'error') throw new Error('bad response');
  } catch (e) {
    setConn(false);
    document.getElementById('consoles-root').innerHTML =
      '<div class="empty">Не удалось подключиться к серверу. Проверьте, что сервер запущен и станция в той же сети, обновите страницу.</div>';
    return;
  }

  document.getElementById('consoles-root').innerHTML = '<div class="console" id="console-root"></div>';
  renderConsole({ focusInput: true });

  setInterval(pollState, 4000);
}

async function pollState() {
  try {
    const s = await api(`/api/stations/${encodeURIComponent(STATION_ID)}/state`);
    if (s && s.result !== 'error') { stationState = s; renderConsole(); }
  } catch (e) { setConn(false); }
}

/* ============================================================
   ВЫБОР НАБОРА, РЕЖИМ «КОД КОРОБА» И СКАНИРОВАНИЕ
   ============================================================ */
function selectKit(kitCode) {
  return enqueue(async () => {
    let resp;
    try {
      resp = await api(`/api/stations/${encodeURIComponent(STATION_ID)}/select-kit`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kit_code: kitCode })
      });
    } catch (e) {
      setConn(false);
      renderConsole({ lastEvent: { result: 'error', message: NO_CONNECTION }, focusInput: true });
      return;
    }
    if (resp.state) stationState = resp.state;
    renderConsole({
      lastEvent: resp.result === 'error' ? { result: 'error', message: resp.message } : undefined,
      focusInput: true
    });
  });
}

function setRequireBox(on) {
  return enqueue(async () => {
    let resp;
    try {
      resp = await api(`/api/stations/${encodeURIComponent(STATION_ID)}/settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ require_box: !!on })
      });
    } catch (e) {
      setConn(false);
      renderConsole({ lastEvent: { result: 'error', message: NO_CONNECTION }, focusInput: true });
      return;
    }
    if (resp.state) stationState = resp.state;
    renderConsole({ focusInput: true });
  });
}

function submitScan(code, opts = {}) {
  code = (code || '').trim();
  if (!code) return Promise.resolve();
  return enqueue(async () => {
    let resp;
    try {
      resp = await api(`/api/stations/${encodeURIComponent(STATION_ID)}/scan`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code })
      });
    } catch (e) {
      setConn(false);
      renderConsole({ lastEvent: { result: 'error', message: NO_CONNECTION }, focusInput: true });
      beepError();
      return;
    }
    if (resp.state) stationState = resp.state;
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
  });
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
    <div class="console-body two-col">
      <div class="col-left">
        <div class="section-label">1. Набор для сборки</div>
        <div class="kit-picker" id="kit-picker"></div>

        <div class="section-label">2. Сканируйте код</div>
        <div class="scan-input-row">
          <input class="scan-input" id="input-station" placeholder="Скан → сюда (отправится автоматически)" autocomplete="off">
        </div>
        <div class="scan-hint" id="hint-station"></div>

        <label id="box-toggle" class="box-toggle"
               title="Включено: в конце набора сканируется код короба DTV… Выключено: набор закрывается автоматически">
          <input type="checkbox" id="chk-require-box">
          <span>Сканировать код короба (DTV…) в конце набора</span>
        </label>
      </div>

      <div class="col-right">
        <div class="section-label" id="checklist-label">Что нужно отсканировать</div>
        <div class="checklist" id="checklist"></div>

        <div class="progress-row">
          <div class="progress-block">
            <div class="progress-label">Закрытых наборов ждут короб</div>
            <div class="count-badge" id="boxcount-station"></div>
          </div>
          <div class="progress-block">
            <div class="progress-label">Коробов ждут паллету</div>
            <div class="count-badge" id="palletcount-station"></div>
          </div>
        </div>

        <div class="feed" id="feed-station"></div>
      </div>
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

init();
