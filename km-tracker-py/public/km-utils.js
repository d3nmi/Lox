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

  const api = { GS, normalizeScan, visibleKm };
  root.KmUtils = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
