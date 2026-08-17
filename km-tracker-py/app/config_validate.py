# ============================================================
# Валидация app/config/kit-templates.json при старте сервера.
#
# Без этой проверки опечатка в конфиге (дубль GTIN, не тот формат кода
# и т.п.) роняет сервер необработанным исключением SQLite прямо на
# старте — голый стектрейс вместо понятной ошибки.
#
# ВАЖНО про GTIN и КМ — это разные вещи, и конфиг требует именно GTIN:
#   - kit_sku / item_sku в этом файле — GTIN-14 товара/набора КАК ТИПА
#     продукции (одинаковый для всех физических экземпляров одного SKU).
#     По нему сервер понимает, ЧТО отсканировали.
#   - Настоящий код маркировки (КМ), уникальный для каждой физической
#     единицы, сюда не вписывается — он приходит только при сканировании
#     и целиком сохраняется в km_code/km_agg_code/km_box_code/pallet_code.
# Самая частая ошибка при ручном редактировании — случайно вставить сюда
# полный отсканированный КМ (с серийным номером и криптохвостом) вместо
# чистого 14-значного GTIN. Проверка ниже ловит это явно.
# ============================================================
import re

GTIN14_RE = re.compile(r'^\d{14}$')


def validate_kit_templates(templates):
    errors = []

    if not isinstance(templates, list) or len(templates) == 0:
        return False, ['Файл kit-templates.json должен содержать непустой массив наборов.']

    seen_kit_sku = {}          # kit_sku -> индекс шаблона
    seen_any_sku_global = {}   # любой GTIN (kit_sku ИЛИ item_sku) -> где встречался

    def register_global_sku(sku, label):
        prev = seen_any_sku_global.get(sku)
        if prev is not None and prev != label:
            errors.append(
                f'GTIN "{sku}" используется одновременно как {prev} и как {label} — '
                f'один и тот же GTIN не может быть и набором, и товаром.'
            )
        seen_any_sku_global[sku] = label

    for t_idx, t in enumerate(templates):
        where = f'Набор №{t_idx + 1}'
        if isinstance(t, dict) and t.get('kit_name'):
            where += f' («{t["kit_name"]}»)'

        if not isinstance(t, dict):
            errors.append(f'{where}: элемент массива должен быть объектом.')
            continue

        kit_name = t.get('kit_name')
        if not isinstance(kit_name, str) or not kit_name.strip():
            errors.append(f'{where}: не заполнено поле "kit_name" (название набора).')

        kit_sku = t.get('kit_sku')
        if not isinstance(kit_sku, str) or not GTIN14_RE.match(kit_sku):
            errors.append(
                f'{where}: "kit_sku" должен быть чистым 14-значным GTIN (например "08056860397516"), '
                f'а не полным отсканированным КМ. Сейчас: {kit_sku!r}. '
                f'Если у вас 13-значный EAN-13 — допишите один ноль слева.'
            )
        else:
            if kit_sku in seen_kit_sku:
                errors.append(
                    f'{where}: kit_sku "{kit_sku}" уже используется в наборе №{seen_kit_sku[kit_sku] + 1} — '
                    f'GTIN набора должен быть уникален.'
                )
            seen_kit_sku[kit_sku] = t_idx
            register_global_sku(kit_sku, f'kit_sku набора «{kit_name or "?"}»')

        items = t.get('items')
        if not isinstance(items, list) or len(items) == 0:
            errors.append(f'{where}: поле "items" должно быть непустым массивом товаров.')
            continue

        seen_item_sku_in_this_kit = {}
        for i_idx, it in enumerate(items):
            item_where = f'{where}, товар №{i_idx + 1}'
            if not isinstance(it, dict):
                errors.append(f'{item_where}: элемент должен быть объектом.')
                continue

            item_name = it.get('item_name')
            if not isinstance(item_name, str) or not item_name.strip():
                errors.append(f'{item_where}: не заполнено поле "item_name".')

            item_sku = it.get('item_sku')
            if not isinstance(item_sku, str) or not GTIN14_RE.match(item_sku):
                errors.append(
                    f'{item_where}: "item_sku" должен быть чистым 14-значным GTIN, а не полным КМ. '
                    f'Сейчас: {item_sku!r}.'
                )
            else:
                if item_sku in seen_item_sku_in_this_kit:
                    errors.append(
                        f'{item_where}: item_sku "{item_sku}" уже указан как товар '
                        f'№{seen_item_sku_in_this_kit[item_sku] + 1} в этом же наборе. '
                        f'Если нужно несколько штук — не дублируйте строку, а увеличьте "qty_required".'
                    )
                seen_item_sku_in_this_kit[item_sku] = i_idx
                register_global_sku(item_sku, f'item_sku товара «{item_name or "?"}»')

            qty = it.get('qty_required')
            if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
                errors.append(f'{item_where}: "qty_required" должен быть целым числом ≥ 1. Сейчас: {qty!r}.')

    return len(errors) == 0, errors
