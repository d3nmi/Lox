// Проверка чтения Alt-кодов сканера (GS = Alt+0029, перевод строки = Alt+0010) без браузера.
const path = require('path');
const { createAltCodeReader } = require(path.join(__dirname, '..', 'public', 'km-utils.js'));
const got = [];
const r = createAltCodeReader(n => got.push(n));
const alt = (digits, viaKeyup) => {
  r.keydown({ key: 'Alt', code: 'AltLeft', keyCode: 18, altKey: true });
  for (const d of digits) r.keydown({ key: d, code: 'Numpad' + d, keyCode: 96 + Number(d), altKey: true });
  if (viaKeyup) r.keyup({ key: 'Alt' });
};
alt('0029'); alt('0029', true); alt('0010'); alt('0065'); alt('029', true); alt('29', true);
process.stdout.write(JSON.stringify(got));
