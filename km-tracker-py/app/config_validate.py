# ============================================================
# Валидация app/config/kit-templates.json при старте сервера.
#
# Формат набора:
#   kit_code  — артикул набора (например "LRU270571"), обязателен, уникален.
#               По нему оператор выбирает набор на станции.
#   kit_sku   — GTIN-14 набора (для агрегационного кода). МОЖЕТ БЫТЬ null:
#               тогда в качестве агрегационного кода принимается любой
#               GS1-код, который не является товаром из состава набора.
#   kit_name  — название набора.
#   items[]   — состав:
#       item_code    — артикул товара, обязателен
#       item_sku     — GTIN-14 товара (13-значный EAN-13 дополняется нулём
#                      слева). МОЖЕТ БЫТЬ null — набор с такими позициями
#                      виден в списке, но выбрать его нельзя, пока GTIN
#                      не заполнены (иначе сервер не поймёт, что отсканировали).
#       item_name    — наименование
#       qty_required — сколько штук, целое >= 1
#       marked       — true: товар с КМ (DataMatrix, попадает в выгрузку);
#                      false: упаковка/товар без КМ — сканируется обычным
#                      штрихкодом (EAN) или артикулом, в выгрузку не входит.
#                      По умолчанию true.
#
# GTIN и КМ — разные вещи: в конфиг пишется чистый GTIN (тип продукции),
# а не полный отсканированный код маркировки.
# ============================================================
import re

GTIN14_RE = re.compile(r'^\d{14}$')


def validate_kit_templates(templates):
    errors = []

    if not isinstance(templates, list) or len(templates) == 0:
        return False, ['Файл kit-templates.json должен содержать непустой массив наборов.']

    seen_kit_code = {}
    sku_role = {}  # GTIN -> 'kit' | 'item'

    def register_role(sku, role, where):
        prev = sku_role.get(sku)
        if prev is not None and prev != role:
            errors.append(
                f'{where}: GTIN "{sku}" используется одновременно и как набор, и как товар — '
                f'один и тот же GTIN не может быть и тем и другим.'
            )
        sku_role.setdefault(sku, role)

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

        kit_code = t.get('kit_code')
        if not isinstance(kit_code, str) or not kit_code.strip():
            errors.append(f'{where}: не заполнено поле "kit_code" (артикул набора, например "LRU270571").')
        else:
            if kit_code in seen_kit_code:
                errors.append(f'{where}: kit_code "{kit_code}" уже используется в наборе №{seen_kit_code[kit_code] + 1}.')
            seen_kit_code[kit_code] = t_idx

        kit_sku = t.get('kit_sku')
        if kit_sku is not None:
            if not isinstance(kit_sku, str) or not GTIN14_RE.match(kit_sku):
                errors.append(
                    f'{where}: "kit_sku" должен быть чистым 14-значным GTIN (например "04630111270571") '
                    f'или null, а не полным отсканированным КМ. Сейчас: {kit_sku!r}. '
                    f'Если у вас 13-значный EAN-13 — допишите один ноль слева.'
                )
            else:
                register_role(kit_sku, 'kit', where)

        items = t.get('items')
        if not isinstance(items, list) or len(items) == 0:
            errors.append(f'{where}: поле "items" должно быть непустым массивом товаров.')
            continue

        seen_item_sku = {}
        seen_item_code = {}
        for i_idx, it in enumerate(items):
            item_where = f'{where}, товар №{i_idx + 1}'
            if not isinstance(it, dict):
                errors.append(f'{item_where}: элемент должен быть объектом.')
                continue

            item_name = it.get('item_name')
            if not isinstance(item_name, str) or not item_name.strip():
                errors.append(f'{item_where}: не заполнено поле "item_name".')

            item_code = it.get('item_code')
            if not isinstance(item_code, str) or not item_code.strip():
                errors.append(f'{item_where}: не заполнено поле "item_code" (артикул товара).')
            else:
                if item_code in seen_item_code:
                    errors.append(f'{item_where}: артикул "{item_code}" уже указан как товар №{seen_item_code[item_code] + 1} в этом наборе. '
                                  f'Если нужно несколько штук — увеличьте "qty_required".')
                seen_item_code[item_code] = i_idx

            item_sku = it.get('item_sku')
            if item_sku is not None:
                if not isinstance(item_sku, str) or not GTIN14_RE.match(item_sku):
                    errors.append(
                        f'{item_where}: "item_sku" должен быть чистым 14-значным GTIN или null, а не полным КМ. '
                        f'Сейчас: {item_sku!r}.'
                    )
                else:
                    if item_sku in seen_item_sku:
                        errors.append(
                            f'{item_where}: GTIN "{item_sku}" уже указан у товара №{seen_item_sku[item_sku] + 1} '
                            f'в этом же наборе. Если нужно несколько штук — увеличьте "qty_required".'
                        )
                    seen_item_sku[item_sku] = i_idx
                    register_role(item_sku, 'item', item_where)

            qty = it.get('qty_required')
            if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
                errors.append(f'{item_where}: "qty_required" должен быть целым числом ≥ 1. Сейчас: {qty!r}.')

            marked = it.get('marked', True)
            if not isinstance(marked, bool):
                errors.append(f'{item_where}: "marked" должен быть true или false. Сейчас: {marked!r}.')

    return len(errors) == 0, errors
