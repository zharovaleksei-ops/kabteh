# SCHEMA — база данных КАБТЕХ v2 (SQLite)

Файл базы: `kabteh.db` (рядом с `kabteh.py`). Резервная копия = скопировать файл.
Миграция: при первом запуске объекты переносятся из `db.json` (если он рядом).

## Таблицы

### objects — объекты карты (27 шт. перенесено из v1)
| поле | тип | описание |
|---|---|---|
| id | INTEGER PK | |
| name | TEXT | название (город/компания) |
| type | TEXT | Поставщик / Заказчик / Хаб |
| city | TEXT | город |
| address | TEXT | адрес |
| contact | TEXT | контакты |
| lat, lng | REAL | координаты |
| color | TEXT | цвет маркера |
| note | TEXT | примечания |

### routes — ручные маршруты (объект → склад(ы))
| поле | тип | описание |
|---|---|---|
| id | INTEGER PK | |
| from_id | INTEGER → objects.id | объект |
| to_ids | TEXT | CSV-список id складов |
| label | TEXT | подпись («2 рейса/нед») |
| color | TEXT | цвет линии |
| visible | INTEGER | 1/0 (🙈/👁) |

### settings — настройки карты (ключ-значение)
| ключ | значение |
|---|---|
| show_lines | 1/0 |
| show_distances | 1/0 |
| line_style | `straight` / `via_hub` |
| font_size | 1..4 |

### cargos — грузы
| поле | тип | описание |
|---|---|---|
| id | INTEGER PK | |
| description | TEXT | |
| from_id, to_id | INTEGER → objects | откуда/куда |
| places | INTEGER | места |
| weight, volume | REAL | вес, объём |
| ready_date | TEXT | дата готовности |
| ship_date | TEXT | план отгрузки |
| status | TEXT | Ожидает / Готов / В пути / Доставлен |
| cost | REAL | стоимость |
| note | TEXT | |

### payments — платежи (привязаны к грузу)
| поле | тип | описание |
|---|---|---|
| id | INTEGER PK | |
| cargo_id | INTEGER → cargos.id | |
| kind | TEXT | аванс / оплата / возврат |
| amount | REAL | сумма |
| due_date | TEXT | срок |
| paid_date | TEXT | факт оплаты (NULL = не оплачен) |
| counterparty | TEXT | контрагент |
| invoice | TEXT | № счёта |
| upd | TEXT | № УПД |

## Платёжный календарь
Вычисляется по `payments`: по месяцам — ожидает / просрочен / оплачен, суммы. Экспорт в CSV.

## Дальнейшее развитие (backlog)
- Сквозные цепочки поставщик→хаб→заказчик с раскраской направления
- PDF-карточка груза
- Напоминания о готовности грузов
- Несколько баз под проекты
