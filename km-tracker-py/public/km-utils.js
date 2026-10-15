/* ============================================================
   Общие функции работы с кодами маркировки (КМ).
   ГЛАВНОЕ ПРАВИЛО: код хранится и передаётся БЕЗ ИЗМЕНЕНИЙ, включая
   управляющий символ GS (ASCII 29, 0x1D). «[GS]» — только способ ПОКАЗАТЬ
   его человеку на экране; в данные это обозначение никогда не попадает.
   ============================================================ */
(function (root) {
  const GS = String.fromCharCode(29);

  /* Убираем только то, что добавляет сам сканер/ввод по краям: пробелы, табуляцию,
     переводы строки. GS и любые другие символы КМ не трогаем (в отличие от
     String.trim() в других языках, например str.strip() в Python, который
     удаляет GS на краях строки). */
  function normalizeScan(raw) {
    return String(raw == null ? '' : raw).replace(/^[ \t\r\n]+|[ \t\r\n]+$/g, '');
  }

  /* Только для показа на экране: настоящий GS -> «[GS]». */
  function visibleKm(s) {
    return String(s == null ? '' : s).split(GS).join('[GS]');
  }

  /* Некоторые сканеры передают управляющие символы как Alt-коды Windows: удерживают Alt и набирают на цифровой клавиатуре
     четыре цифры (GS = Alt+0029, перевод строки = Alt+0010). Поле ввода такие символы молча теряет, поэтому
     сами читаем нажатия. onControl(n) вызывается с числом 1–31 (29 = GS, 10/13 = конец ввода).
     Печатные символы (код 32 и выше) браузер вставляет сам — их не трогаем. */
  function altCodeDigit(e) {
    if (!e.altKey || e.ctrlKey) return null;
    const m = /^Numpad([0-9])$/.exec(e.code || '');
    if (m) return m[1];
    if (e.keyCode >= 96 && e.keyCode <= 105) return String(e.keyCode - 96);
    return null;
  }
  function createAltCodeReader(onControl) {
    let buf = '';
    function finish() {
      const s = buf; buf = '';
      if (s.length < 2 || s[0] !== '0') return false;       // с ведущего нуля начинаются коды «ANSI» (0029); без нуля — другая таблица
      const n = parseInt(s, 10);
      if (n >= 1 && n <= 31) { onControl(n); return true; }
      return false;
    }
    return {
      keydown(e) {              // true — это цифра Alt-кода (обработана)
        const d = altCodeDigit(e);
        if (d === null) return false;
        buf += d;
        if (buf.length >= 4) finish();
        return true;
      },
      keyup(e) { if (e.key === 'Alt' && buf) finish(); },
    };
  }

  const api = { GS, normalizeScan, visibleKm, createAltCodeReader };
  root.KmUtils = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
