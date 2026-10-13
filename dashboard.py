#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
КАБТЕХ — логистический дашборд (живой, не генерация HTML).

Однофайловый сервер на чистом Python (только стандартная библиотека).
Данные хранятся в db.json рядом со скриптом. Открываете браузером
http://localhost:8050 — карта, поиск, карточки, добавление/редактирование/
удаление объектов, экспорт/импорт CSV, всё сохраняется автоматически.

Запуск:  python3 dashboard.py [--port 8050]
"""

import json
import csv
import io
import math
import os
import sys
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "db.json")

FIELDS = ["type", "name", "city", "inn", "kpp", "legal_addr", "wh_addr",
          "entry", "hours", "contact", "phone", "email", "notes"]

FIELD_LABELS = {
    "type": "Тип", "name": "Название", "city": "Город", "inn": "ИНН", "kpp": "КПП",
    "legal_addr": "Юр. адрес", "wh_addr": "Адрес склада", "entry": "Заезд",
    "hours": "Режим работы", "contact": "Контактное лицо", "phone": "Телефон",
    "email": "Email", "notes": "Примечания",
}

TYPES = {"supplier": "Поставщик", "client": "Заказчик", "hub": "Хаб КАБТЕХ"}

# Базовые координаты городов для автопривязки новых точек (можно править в db.json -> city_coords)
CITY_COORDS = {
    "Саранск": [54.1808, 45.1772], "Балахна": [56.2528, 43.5606],
    "Казань": [55.7963, 49.1088], "Ярославль": [57.6261, 39.8845],
    "Самара": [53.2000, 50.1500], "Домодедово": [55.4386, 37.7508],
    "Котельники": [55.6333, 37.8764], "Санкт-Петербург": [59.9389, 30.3157],
    "Набережные Челны": [55.7438, 52.3959], "Дзержинск": [56.2413, 43.4591],
    "Алфимово": [55.0500, 38.1300], "Апрелевка": [55.4266, 36.9644],
    "Климовск": [55.4288, 37.5644], "Балашиха": [55.7964, 37.9372],
    "Тула": [54.1961, 37.6182], "Воровского": [55.8600, 38.3700],
    "Подольск": [55.4258, 37.5103], "Москва": [55.7522, 37.6156],
    "Курск": [51.7304, 36.2085], "Белорецк": [53.9692, 58.4050],
    "Южноуральск": [54.4615, 61.9643], "Волгоград": [48.7080, 44.5133],
    "Спутник": [53.2700, 50.4200],
}

SEED_ROWS = [  # данные из таблицы пользователя; lat/lon проставляются при инициализации
    ("supplier", "Сарансккабель-Оптика", "Саранск", "1327153649", "132701001",
     "430016, Республика Мордовия, г. Саранск, ул. Строительная, д. 3Г, строение 1",
     "430016, Республика Мордовия, г. Саранск, ул. Строительная, д. 3Г, строение 1",
     "с ул. Титова", "уточняется", "Макарова Елена", "+7 (927) 193-91-03", "makarova@sarko.ru",
     "Обязательна боковая загрузка. Обязателен деревянный пол в авто. Самовывоз. Кабель готов 22 мая."),
    ("supplier", "Сармат", "Саранск", "1327013779", "132701001",
     "430016, Республика Мордовия, г. Саранск, ул. Строительная, д. 3В",
     "430016, Республика Мордовия, г. Саранск, ул. Строительная, д. 3В",
     "уточняется", "уточняется", "Шилов Пётр Иванович", "+7 (927) 171-36-83", "office@sarmatura.ru",
     "Срок изготовления: 2 недели. Готовность 22 мая. Доставку организует КАБТЕХ."),
    ("supplier", "БЭК", "Балахна", "5244009960", "524401001",
     "606408, Нижегородская обл., г. Балахна, ул. Филатова, д. 9",
     "606408, Нижегородская обл., г. Балахна, ул. Филатова, д. 9",
     "", "Пн–Пт: 09:00–16:00 (без обеда)", "Юкалов Игорь Викторович", "+7 (920) 253-35-51", "bec_nn@mail.ru",
     "Пропуск не требуется. Погрузка силами поставщика. Для отгрузки необходимо доверительное письмо."),
    ("supplier", "Оптик Ком", "Казань", "5259119147", "525701001",
     "603070, г. Нижний Новгород, б-р Мещерский, д. 3, к. 3, пом. П13, офис 1",
     "г. Казань, ул. Тополевая, д. 53А, стр. 1",
     "", "Пн–Пт: 08:00–17:00 (без обеда)", "Марат", "+7 (919) 694-08-15", "kzn.optik@mail.ru",
     "Самовывоз. Забирали монтажники на УАЗе."),
    ("supplier", "Оптик Ком", "Ярославль", "", "", "",
     "г. Ярославль, ул. Промышленная, д. 20, стр. 2",
     "", "уточняется", "Евгений (начальник склада)", "+7 (919) 534-20-07", "",
     "Отгрузка через Деловые Линии. ХП-550 (9 шт)."),
    ("supplier", "Элком Трейд", "Самара", "7802629205", "780201001",
     "194354, г. Санкт-Петербург, ул. Есенина, д. 1, корп. 1, литер А, пом. 55-Н",
     "г. Самара, Московское шоссе, 23 км, д. 20, корп. 45",
     "", "09:00–16:00", "Александр Максим", "+7 (927) 177-46-96, +7 (927) 750-25-15", "mmm@elcom-trade.ru",
     "Отгрузка через Деловые Линии."),
    ("supplier", "ССД (Связьстройдеталь)", "Домодедово", "7704588494", "772301001",
     "115088, г. Москва, ул. Южнопортовая, д. 7А",
     "142001, Московская обл., г. Домодедово, ул. Промышленная, д. 13",
     "", "Пн–Пт: 08:00–19:00", "Шитиков Алексей Николаевич", "+7 (495) 786-34-34", "mail@ssd.ru",
     "Основной склад ССД. Самовывоз. Отгрузка: крепёж, кроссы, трубка ССД ТУТ, комплект заземления."),
    ("supplier", "ЭТМ", "Котельники", "7842224734", "784201001",
     "191144, г. Санкт-Петербург, ул. 7-я Советская, д. 44, литера Б, пом. 6-Н",
     "140054, Московская обл., г. Котельники, мкр. Ковровый, д. 37, пом. 18М",
     "", "Пн–Пт: 09:00–17:00; Сб: 09:00–17:00; Вс: выходной", "Крючкова Александра", "+7 (495) 783-70-27", "kryuchkova_at@etm.ru",
     "Отгрузка через Деловые Линии. Доверенность: оригинал с печатью. Кодовые слова: «Мы в ЭТМ»."),
    ("supplier", "Пластком", "Санкт-Петербург", "7807396811", "780701001",
     "198218, г. Санкт-Петербург, Волхонское шоссе, д. 115, литер Ж",
     "198323, г. Санкт-Петербург, Волхонское шоссе, д. 115, литер Ж",
     "", "уточняется", "Бордо Сергей", "(812) 746-17-61", "plastcom@plastcom.spb.ru",
     "Труба ЗПТ НГ (800 м) + муфты (13 шт). Доставка СТС Логистикс."),
    ("supplier", "Энергоком", "Набережные Челны", "1650399507", "165001001",
     "423810, Республика Татарстан, г. Набережные Челны, пр. Раиса Беляева, д. 4, офис 24",
     "", "", "уточняется", "", "", "",
     "Кабель ОКПнг(А)-HF-0,22-16-П (300 м). УПД №66 от 23.06.2026."),
    ("supplier", "Элком Трейд", "Дзержинск", "7802629205", "780201001",
     "194354, г. Санкт-Петербург, ул. Есенина, д. 1, корп. 1, литер А, пом. 55-Н",
     "г. Дзержинск (Нижегородская обл.) — точный адрес уточняется",
     "", "уточняется", "", "", "",
     "Отгрузка СОЭ, КМП, ЛСО-40. Доставка Деловыми Линиями."),
    ("supplier", "Элком Трейд", "Балахна", "7802629205", "780201001",
     "194354, г. Санкт-Петербург, ул. Есенина, д. 1, корп. 1, литер А, пом. 55-Н",
     "606408, Нижегородская обл., г. Балахна, ул. Филатова, д. 9",
     "", "08:00–17:00, отгрузка до 16:15", "Майя Михайловна (Абрамова)", "+7 (920) 253-35-51", "",
     "ХП-550 (9 шт)."),
    ("hub", "КАБТЕХ (Алфимово)", "Алфимово (Ступинский р-н)", "7724438582", "503001001",
     "Московская обл., Наро-Фоминский р-н, г. Апрелевка, ул. Парковая, д. 1",
     "Московская обл., Ступинский р-н, д. Алфимово, ул. Шоссейная, вл. 9",
     "", "уточняется", "", "+7 (499) 289-80-03", "mail@cab-tech.ru",
     "Основной логистический хаб. Крупнотоннажное хранение, комплектация федеральных проектов."),
    ("hub", "КАБТЕХ (Апрелевка)", "Апрелевка", "7724438582", "503001001",
     "Московская обл., Наро-Фоминский р-н, г. Апрелевка, ул. Парковая, д. 1",
     "Московская обл., Наро-Фоминский р-н, г. Апрелевка, ул. Парковая, д. 1",
     "", "уточняется", "", "+7 (499) 289-80-03", "mail@cab-tech.ru",
     "Офисно-складской комплекс. Быстрая отгрузка срочных заказов."),
    ("hub", "КАБТЕХ (Климовск)", "Климовск", "7724438582", "503001001",
     "Московская обл., Наро-Фоминский р-н, г. Апрелевка, ул. Парковая, д. 1",
     "Московская обл., г. Климовск, ул. Товарная, д. 29",
     "", "уточняется", "", "+7 (499) 289-80-03", "mail@cab-tech.ru",
     "Временный склад, перегрузка."),
    ("supplier", "ЮАИЗ", "Балашиха", "6164235725", "742401001",
     "457040, Челябинская область, г. Южноуральск, ул. Заводская, д. 1Е, оф. 214",
     "Московская область, г. Балашиха, мкр. Новое Павлино, Косинское шоссе, вл. 7, склад 5",
     "", "8:00–16:00", "Стариков Олег, Надежда", "+7 (926) 056-57-22", "msk@gig-group.com",
     "Самовывоз по факту прихода денег, при наличии доверенности и паспорта. Груз: ящик 260 кг, паллета 350 кг."),
    ("supplier", "ТЭМЗ (Тульский Электромеханический завод)", "Тула", "7107088113", "710701001",
     "300057, г. Тула, ул. Железнодорожная, д. 48",
     "300045, г. Тула, ул. Железнодорожная, д. 48",
     "", "9:00–12:00 и 13:00–16:00", "Елена (склад), Шварц Ольга (склад)", "+7 (950) 910-79-64; +7 (930) 074-00-28", "sekretar@temz-tula.ru",
     "Перерыв с 12:00 до 13:00. Груз: 35 кг, ящик ~0.3×0.3×0.3 м."),
    ("supplier", "МКС-ЭнергоГрупп", "пос. Воровского (МО)", "7720400492", "772001001",
     "111141, г. Москва, ул. Плеханова, д. 7, помещ. 1/3",
     "Московская область, Ногинский район, пос. Воровского, ул. Воровского, д. 12",
     "", "Пн–Пт: 09:00–18:00; Сб–Вс: выходной", "Попович Наталия", "+7 (495) 664-99-14; +7 (495) 215-06-32", "mks-energogrupp@mail.ru",
     "Кабельно-проводниковая продукция."),
    ("supplier", "СНАБЭКСПОРТ", "Подольск", "1326236525", "",
     "г. Саранск, ул. Маринина, д. 20",
     "Московская область, г. Подольск, ул. Вишневая, д. 7",
     "", "06:00–18:00", "Бевняев Никита", "+7 (495) 246-50-50", "m12@ruzkabel.ru",
     "Связано с ООО «Рузкабель». Кабель и провод."),
    ("supplier", "ДКО ПОЛИПЛАСТИК", "Москва", "9729289612", "",
     "", "г. Москва, Очаковское шоссе, д. 18, стр. 3, помещ. 113А",
     "", "Пн–Чт: 09:00–18:00; Пт: 09:00–17:30", "Ольга Воробьева", "+7 (495) 737-04-28; +7 (925) 139-04-70", "olga.vorobeva@polyplastic.ru",
     "Пластиковые трубы (ГК «Полипластик»)."),
    ("supplier", "КТЗ (Климовский трубный завод)", "Климовск", "5021012366", "",
     "", "142182, Московская область, г. Подольск, мкр. Климовск, Бережковский проезд, д. 10",
     "", "уточнить у поставщика", "", "", "",
     "Грузоотправитель по УПД на трубу."),
    ("supplier", "ТД «Электросетьстройпроект»", "Курск", "7715877160", "771501001",
     "127566, г. Москва, Высоковольтный проезд, д. 1, стр. 36",
     "г. Курск, 2-ой Литовский переулок, дом 4-В",
     "", "уточняется", "Черных Ольга Андреевна", "+7 (495) 727-43-43 (доб. 1264)", "",
     "Спиральная арматура и гасители вибрации."),
    ("client", "Мордовэнерго (Филиал ПАО «Россети Волга»)", "Саранск", "6450925977", "132743001",
     "430030, Республика Мордовия, г. Саранск, ул. Васенко, д. 40В",
     "430000, Республика Мордовия, г. Саранск, проспект Ленина, дом 50",
     "", "Пн–Пт: 08:00–17:00, перерыв 12:00–13:00", "Годунов Алексей Николаевич", "+7 (927) 184-33-34", "",
     "Обязательно согласовать время приезда заранее."),
    ("supplier", "УралКанатСервис", "Белорецк", "7456025237", "745601001",
     "455026, Челябинская обл., г. Магнитогорск, ул. Гагарина, д. 33",
     "Республика Башкортостан, г. Белорецк, ул. Тюленина, д. 20",
     "", "Пн–Пт: 08:00–19:00; Сб–Вс: 08:00–17:00", "Рыбаков Сергей Петрович", "+7 (905) 006-55-20; +7 (495) 212-23-67", "",
     "Тросы, канаты (метизы)."),
    ("supplier", "ЮАИЗ", "Южноуральск", "6164235725", "742401001",
     "457040, Челябинская обл., г. Южноуральск, ул. Заводская, д. 1Е",
     "457040, Челябинская обл., г. Южноуральск, ул. Заводская, д. 1Е",
     "", "", "Афанасова Светлана", "+7 (962) 912-66-37", "afanasova@gig-group.com",
     "Склад там же, где офис."),
    ("supplier", "Энергосервис", "Волгоград", "7724504140", "772601001",
     "117638, г. Москва, Варшавское ш., д. 56, стр. 2",
     "г. Волгоград, ул. Бахтурова, д. 12",
     "", "уточняется", "уточняется", "уточняется", "",
     "По спецификации склад отгрузки — Волгоград."),
    ("supplier", "Стройком-А", "пос. Спутник (Самарская обл.)", "уточняется", "уточняется",
     "410012, г. Саратов, ул. Большая Казачья, д. 56, оф. 1",
     "Самарская обл., пос. Спутник, ул. Придорожная, д. 4, корп. 1",
     "", "уточняется", "Терников Георгий Сергеевич", "+7 (937) 975-68-05", "tgs@stroycom-a.ru",
     "Адрес склада уточнён."),
]


def find_city_coord(city, db=None):
    """Ищет координаты по названию города (подстрока)."""
    c = (city or "").strip().lower()
    if not c:
        return None
    coords = (db or {}).get("city_coords") or CITY_COORDS
    for name, xy in coords.items():
        if name.lower() in c or c in name.lower():
            return xy
    return None


def init_db():
    """Создаёт db.json из SEED_ROWS, если его ещё нет."""
    if os.path.exists(DB_PATH):
        with open(DB_PATH, encoding="utf-8") as f:
            return json.load(f)
    nodes = []
    nid = 1
    for r in SEED_ROWS:
        d = dict(zip(FIELDS, r))
        xy = find_city_coord(d["city"])
        d["id"] = nid; nid += 1
        d["lat"], d["lon"] = xy if xy else [None, None]
        nodes.append(d)
    db = {"next_id": nid, "nodes": nodes, "city_coords": CITY_COORDS}
    save_db(db)
    return db


def load_db():
    with open(DB_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_db(db):
    tmp = DB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DB_PATH)


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>КАБТЕХ · Логистический дашборд</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
:root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--txt:#e8ebf0;--dim:#9aa3b2;
--red:#e63946;--grn:#2a9d8f;--blu:#4ea1ff;--amber:#f4a261;}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--txt);height:100vh;display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:14px;padding:10px 16px;background:var(--panel);border-bottom:1px solid var(--line)}
header h1{font-size:16px;margin:0;white-space:nowrap}
header h1 b{color:var(--blu)}
#stats{font-size:12px;color:var(--dim)}
header .sp{flex:1}
button{background:#222835;color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:7px 12px;font-size:13px;cursor:pointer}
button:hover{border-color:var(--blu)}
button.primary{background:var(--blu);border-color:var(--blu);color:#04122b;font-weight:600}
button.danger{color:var(--red)}
main{flex:1;display:flex;min-height:0}
#list{width:360px;min-width:280px;background:var(--panel);border-right:1px solid var(--line);display:flex;flex-direction:column}
#tools{padding:10px;border-bottom:1px solid var(--line);display:flex;flex-direction:column;gap:8px}
#search{width:100%;background:#0f1115;border:1px solid var(--line);border-radius:8px;color:var(--txt);padding:8px 10px;font-size:13px}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{font-size:12px;padding:4px 10px;border-radius:14px;border:1px solid var(--line);cursor:pointer;color:var(--dim);user-select:none}
.chip.on{color:#fff;border-color:transparent}
.chip[data-t=supplier].on{background:var(--red)} .chip[data-t=client].on{background:var(--grn)} .chip[data-t=hub].on{background:var(--blu)}
#items{flex:1;overflow:auto}
.item{padding:9px 12px;border-bottom:1px solid var(--line);cursor:pointer}
.item:hover,.item.sel{background:#1d222c}
.item .t{font-size:13px;font-weight:600;display:flex;gap:6px;align-items:baseline}
.item .badge{font-size:10px;padding:1px 7px;border-radius:9px;color:#fff;flex:none}
.item .s{font-size:12px;color:var(--dim);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#map{flex:1}
#side{width:400px;min-width:320px;background:var(--panel);border-left:1px solid var(--line);overflow:auto;display:none;flex-direction:column}
#side.open{display:flex}
#sideHead{padding:12px 14px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:8px}
#sideHead .t{font-size:15px;font-weight:700;flex:1}
#formBox{padding:12px 14px;display:flex;flex-direction:column;gap:9px}
label.f{font-size:11px;color:var(--dim);display:flex;flex-direction:column;gap:3px}
input.i,select.i,textarea.i{background:#0f1115;border:1px solid var(--line);border-radius:7px;color:var(--txt);padding:7px 9px;font-size:13px;font-family:inherit;width:100%}
textarea.i{resize:vertical;min-height:54px}
.row2{display:grid;grid-template-columns:1fr 1fr;gap:9px}
.geoRow{display:flex;gap:6px;align-items:center}
#geoMsg{font-size:11px;color:var(--dim)}
.hint{font-size:11px;color:var(--dim)}
.kv{padding:12px 14px;display:flex;flex-direction:column;gap:7px;font-size:13px}
.kv .k{color:var(--dim);font-size:11px;margin-top:5px}
.kv a{color:var(--blu);text-decoration:none}
.actions{display:flex;gap:8px;padding:12px 14px;border-top:1px solid var(--line);margin-top:auto}
#toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:#222835;border:1px solid var(--line);padding:9px 16px;border-radius:9px;font-size:13px;opacity:0;transition:.25s;pointer-events:none;z-index:9999}
#toast.show{opacity:1}
.leaflet-popup-content{font-size:13px} .mk{border-radius:50%;border:2px solid #fff;box-shadow:0 0 6px rgba(0,0,0,.6)}
dialog{background:var(--panel);color:var(--txt);border:1px solid var(--line);border-radius:12px;padding:16px;max-width:560px;width:92%}
dialog::backdrop{background:rgba(0,0,0,.6)}
</style>
</head>
<body>
<header>
  <h1><b>КАБТЕХ</b> · Логистический дашборд</h1>
  <span id="stats"></span>
  <span class="sp"></span>
  <button id="btnAdd" class="primary">＋ Новый объект</button>
  <button id="btnExport">CSV ↓</button>
  <button id="btnImport">CSV ↑</button>
  <input type="file" id="fileImport" accept=".csv,text/csv" style="display:none">
</header>
<main>
  <div id="list">
    <div id="tools">
      <input id="search" placeholder="Поиск: название, город, ИНН…">
      <div class="chips">
        <span class="chip on" data-t="supplier">Поставщики</span>
        <span class="chip on" data-t="client">Заказчики</span>
        <span class="chip on" data-t="hub">Хабы</span>
      </div>
    </div>
    <div id="items"></div>
  </div>
  <div id="map"></div>
  <div id="side"></div>
</main>
<div id="toast"></div>
<dialog id="dlgCsv"><h3 style="margin:4px 0 10px">Импорт CSV</h3>
<div id="csvPrev" class="hint"></div>
<label class="f"><input type="checkbox" id="csvReplace" style="width:auto"> Заменить все данные (иначе — добавить новые)</label>
<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
<button id="csvCancel">Отмена</button><button id="csvOk" class="primary">Импортировать</button></div></dialog>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const FIELDS=["type","name","city","inn","kpp","legal_addr","wh_addr","entry","hours","contact","phone","email","notes"];
const LABELS={type:"Тип",name:"Название",city:"Город",inn:"ИНН",kpp:"КПП",legal_addr:"Юр. адрес",wh_addr:"Адрес склада",entry:"Заезд",hours:"Режим работы",contact:"Контактное лицо",phone:"Телефон",email:"Email",notes:"Примечания"};
const TYPES={supplier:"Поставщик",client:"Заказчик",hub:"Хаб КАБТЕХ"};
const COLORS={supplier:"#e63946",client:"#2a9d8f",hub:"#4ea1ff"};
let NODES=[],SEL=null,MODE="view"; // view | edit | new
const map=L.map('map',{zoomControl:true}).setView([55.3,44.0],5);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}).addTo(map);
const layerLines=L.layerGroup().addTo(map), layerNodes=L.layerGroup().addTo(map);

async function api(path,body){const o={headers:{'Content-Type':'application/json'}};if(body!==undefined){o.method='POST';o.body=JSON.stringify(body);}const r=await fetch(path,o);if(!r.ok)throw new Error(await r.text());return r.json();}
async function reload(){const d=await api('/api/data');NODES=d.nodes;drawAll();}
function esc(s){return (s??'').toString().replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),1800);}
function hav(a,b){const R=6371,r=Math.PI/180;const dLa=(b[0]-a[0])*r,dLo=(b[1]-a[1])*r;const h=Math.sin(dLa/2)**2+Math.cos(a[0]*r)*Math.cos(b[0]*r)*Math.sin(dLo/2)**2;return 2*R*Math.asin(Math.sqrt(h));}
function arc(a,b,n=40){ // дуга с подъёмом средней точки — эффект авиамаршрута
  const out=[];for(let i=0;i<=n;i++){const t=i/n;const lat=a[0]+(b[0]-a[0])*t,lon=a[1]+(b[1]-a[1])*t;const bump=Math.sin(Math.PI*t)*(hav(a,b)*0.006);out.push([lat+bump,lon]);}return out;}

function visible(n){if(!CH.has(n.type))return false;const q=Q.trim().toLowerCase();if(!q)return true;return (n.name+' '+n.city+' '+n.inn+' '+(n.contact||'')+' '+(n.notes||'')).toLowerCase().includes(q);}
let Q='',CH=new Set(['supplier','client','hub']);

function drawAll(){
  layerNodes.clearLayers();layerLines.clearLayers();
  const hubs=NODES.filter(n=>n.type==='hub'&&n.lat!=null);
  let totalKm=0,lines=0;
  for(const n of NODES){
    if(!visible(n))continue;
    if(n.type!=='hub'&&n.lat!=null&&hubs.length){
      let best=null,bd=1e9;for(const h of hubs){const d=hav([n.lat,n.lon],[h.lat,h.lon]);if(d<bd){bd=d;best=h;}}
      if(best){totalKm+=bd;lines++;
        L.polyline(arc([n.lat,n.lon],[best.lat,best.lon]),{color:COLORS[n.type],weight:1.6,opacity:.55,dashArray:'1,6'}).addTo(layerLines);
        const mp=[(n.lat+best.lat)/2,(n.lon+best.lon)/2];
        L.marker(mp,{interactive:false,icon:L.divIcon({className:'',html:`<span style="font-size:10px;color:#8b93a5;text-shadow:0 0 4px #000">${Math.round(bd)} км</span>`,iconSize:[40,12]})}).addTo(layerLines);}
    }
    if(n.lat==null)continue;
    const size=n.type==='hub'?18:12;
    const ic=L.divIcon({className:'',html:`<div class="mk" style="width:${size}px;height:${size}px;background:${COLORS[n.type]}"></div>`,iconSize:[size,size],iconAnchor:[size/2,size/2]});
    const m=L.marker([n.lat,n.lon],{icon:ic}).addTo(layerNodes);
    m.bindTooltip(`${esc(n.name)} · ${esc(n.city)}`,{direction:'top',offset:[0,-8]});
    m.on('click',()=>openNode(n.id));
  }
  document.getElementById('stats').textContent=`${NODES.filter(visible).length} узлов · ${lines} линий · ≈${Math.round(totalKm).toLocaleString('ru-RU')} км`;
  drawList();
}
function drawList(){
  const el=document.getElementById('items');el.innerHTML='';
  for(const n of NODES.filter(visible)){
    const d=document.createElement('div');d.className='item'+(SEL===n.id?' sel':'');
    d.innerHTML=`<div class="t"><span class="badge" style="background:${COLORS[n.type]}">${TYPES[n.type]}</span>${esc(n.name)}</div>
      <div class="s">${esc(n.city)||'— без города —'}${n.wh_addr?' · '+esc(n.wh_addr.split(',')[0]):''}${n.lat==null?' · ⚠ нет координат':''}</div>`;
    d.onclick=()=>openNode(n.id);el.appendChild(d);
  }
}
function kv(k,v){if(!v||v==='—')return '';return `<div><div class="k">${k}</div><div>${v}</div></div>`;}
function openNode(id){
  SEL=id;MODE='view';renderSide();drawList();
  const n=NODES.find(x=>x.id===id);if(n&&n.lat!=null)map.flyTo([n.lat,n.lon],7,{duration:.6});
}
function renderSide(){
  const side=document.getElementById('side');
  if(MODE==='view'){
    const n=NODES.find(x=>x.id===SEL);
    if(!n){side.classList.remove('open');return;}
    side.classList.add('open');
    const tel=(n.phone||'').split(/[,;]/).map(t=>t.trim()).filter(Boolean).map(t=>`<a href="tel:${t.replace(/[^\d+]/g,'')}">${esc(t)}</a>`).join(', ');
    const links=[kv(LABELS.phone,tel||'—'),kv(LABELS.email,n.email?`<a href="mailto:${esc(n.email)}">${esc(n.email)}</a>`:'—')].join('');
    side.innerHTML=`<div id="sideHead"><span class="badge" style="background:${COLORS[n.type]};padding:2px 9px;border-radius:10px;font-size:11px">${TYPES[n.type]}</span>
      <span class="t">${esc(n.name)}</span><button onclick="closeSide()">✕</button></div>
      <div class="kv">${kv(LABELS.city,esc(n.city)||'—')}${kv(LABELS.inn,esc(n.inn)+(n.kpp?' / '+esc(n.kpp):''))}
      ${kv(LABELS.legal_addr,esc(n.legal_addr))}${kv(LABELS.wh_addr,esc(n.wh_addr))}
      ${kv(LABELS.entry,esc(n.entry))}${kv(LABELS.hours,esc(n.hours))}${kv(LABELS.contact,esc(n.contact))}${links}
      ${kv(LABELS.notes,esc(n.notes))}
      ${n.lat!=null?kv('Координаты',`${n.lat.toFixed(4)}, ${n.lon.toFixed(4)} · <a href="https://www.openstreetmap.org/?mlat=${n.lat}&mlon=${n.lon}#map=16/${n.lat}/${n.lon}" target="_blank">открыть в OSM ↗</a>`):'<div style="color:var(--amber)">⚠ Координаты не заданы — нажмите «Изменить» и укажите город или точку на карте.</div>'}</div>
      <div class="actions"><button class="primary" onclick="editMode()">✎ Изменить</button><button class="danger" onclick="delNode()">🗑 Удалить</button></div>`;
  }else{
    const isNew=MODE==='new';
    const n=isNew?{type:'supplier',name:'',city:'',inn:'',kpp:'',legal_addr:'',wh_addr:'',entry:'',hours:'',contact:'',phone:'',email:'',notes:'',lat:null,lon:null}
                 :NODES.find(x=>x.id===SEL);
    side.classList.add('open');
    side.innerHTML=`<div id="sideHead"><span class="t">${isNew?'Новый объект':'Редактирование'}</span><button onclick="closeSide()">✕</button></div>
    <form id="frm"><div id="formBox">
      <label class="f">${LABELS.type}<select class="i" name="type">${Object.entries(TYPES).map(([k,v])=>`<option value="${k}" ${n.type===k?'selected':''}>${v}</option>`).join('')}</select></label>
      <label class="f">${LABELS.name}*<input class="i" name="name" required value="${esc(n.name)}"></label>
      <div class="row2"><label class="f">${LABELS.city}<input class="i" name="city" id="fCity" value="${esc(n.city)}"></label>
      <label class="f">Координаты<input class="i" readonly value="${n.lat!=null?n.lat.toFixed(4)+', '+n.lon.toFixed(4):'нет'}" id="fXY"></label></div>
      <div class="geoRow"><button type="button" id="btnGeo">🌍 Привязать по городу</button><button type="button" id="btnPick">📍 Выбрать на карте</button><span id="geoMsg"></span></div>
      <div class="row2"><label class="f">${LABELS.inn}<input class="i" name="inn" value="${esc(n.inn)}"></label><label class="f">${LABELS.kpp}<input class="i" name="kpp" value="${esc(n.kpp)}"></label></div>
      <label class="f">${LABELS.legal_addr}<textarea class="i" name="legal_addr">${esc(n.legal_addr)}</textarea></label>
      <label class="f">${LABELS.wh_addr}<textarea class="i" name="wh_addr">${esc(n.wh_addr)}</textarea></label>
      <div class="row2"><label class="f">${LABELS.entry}<input class="i" name="entry" value="${esc(n.entry)}"></label><label class="f">${LABELS.hours}<input class="i" name="hours" value="${esc(n.hours)}"></label></div>
      <label class="f">${LABELS.contact}<input class="i" name="contact" value="${esc(n.contact)}"></label>
      <div class="row2"><label class="f">${LABELS.phone}<input class="i" name="phone" value="${esc(n.phone)}"></label><label class="f">${LABELS.email}<input class="i" name="email" value="${esc(n.email)}"></label></div>
      <label class="f">${LABELS.notes}<textarea class="i" name="notes">${esc(n.notes)}</textarea></label>
      <div class="hint">* обязательные поля. Данные сохраняются в db.json на сервере.</div>
    </div>
    <div class="actions"><button type="submit" class="primary">💾 Сохранить</button><button type="button" onclick="closeSide()">Отмена</button></div></form>`;
    document.getElementById('frm').onsubmit=async e=>{
      e.preventDefault();
      const fd=Object.fromEntries(new FormData(e.target));
      fd.lat=pendingLat??n.lat; fd.lon=pendingLon??n.lon;
      if(isNew){const r=await api('/api/add',fd);toast('Объект добавлен');await reload();openNode(r.id);}
      else{await api('/api/update',{id:SEL,...fd});toast('Сохранено');await reload();openNode(SEL);}
    };
    let pendingLat=null,pendingLon=null;
    document.getElementById('btnGeo').onclick=async()=>{
      const city=document.getElementById('fCity').value.trim();
      const r=await api('/api/geocode',{city});
      if(r.ok){pendingLat=r.lat;pendingLon=r.lon;document.getElementById('fXY').value=r.lat.toFixed(4)+', '+r.lon.toFixed(4);
        document.getElementById('geoMsg').textContent='Город распознан: '+r.matched;map.flyTo([r.lat,r.lon],9);}
      else{document.getElementById('geoMsg').textContent='Неизвестный город — используйте «Выбрать на карте»';}
    };
    document.getElementById('btnPick').onclick=()=>{
      pickMode=true;document.getElementById('geoMsg').textContent='Кликните по карте…';map.getContainer().style.cursor='crosshair';
    };
  }
}
let pickMode=false;
map.on('click',e=>{if(!pickMode)return;pickMode=false;map.getContainer().style.cursor='';
  pendingLat=e.latlng.lat;pendingLon=e.latlng.lng;
  const xy=document.getElementById('fXY');if(xy)xy.value=e.latlng.lat.toFixed(4)+', '+e.latlng.lng.toFixed(4);
  L.circleMarker(e.latlng,{radius:6,color:'#f4a261',fillOpacity:1}).addTo(layerNodes);
  document.getElementById('geoMsg').textContent='Точка выбрана, не забудьте сохранить';});
function closeSide(){MODE='view';document.getElementById('side').classList.remove('open');drawList();}
function editMode(){MODE='edit';renderSide();}
async function delNode(){if(!confirm('Удалить объект?'))return;await api('/api/delete',{id:SEL});SEL=null;closeSide();await reload();toast('Удалено');}
document.getElementById('btnAdd').onclick=()=>{MODE='new';SEL=null;pendingLat=pendingLon=null;renderSide();};
document.getElementById('search').oninput=e=>{Q=e.target.value;drawAll();};
document.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{c.classList.toggle('on');CH=new Set([...document.querySelectorAll('.chip.on')].map(x=>x.dataset.t));drawAll();});
document.getElementById('btnExport').onclick=()=>location.href='/api/export.csv';
document.getElementById('btnImport').onclick=()=>document.getElementById('fileImport').click();
let pendingCsv=null;
document.getElementById('fileImport').onchange=async ev=>{
  const f=ev.target.files[0];if(!f)return;pendingCsv=await f.text();
  const rows=pendingCsv.split('\n').filter(l=>l.trim()).length;
  document.getElementById('csvPrev').textContent=`Файл: ${f.name}, строк: ~${rows}. Колонки должны совпадать с экспортируемыми (Тип, Название, Город, ...).`;
  document.getElementById('dlgCsv').showModal();ev.target.value='';
};
document.getElementById('csvCancel').onclick=()=>document.getElementById('dlgCsv').close();
document.getElementById('csvOk').onclick=async()=>{
  await api('/api/import',{csv:pendingCsv,replace:document.getElementById('csvReplace').checked});
  document.getElementById('dlgCsv').close();await reload();toast('Импортировано');
};
window.closeSide=closeSide;window.editMode=editMode;window.delNode=delNode;
reload();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, HTML_PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/data":
            db = load_db()
            self._send(200, json.dumps({"nodes": db["nodes"]}, ensure_ascii=False))
        elif self.path.startswith("/api/export.csv"):
            db = load_db()
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow([FIELD_LABELS[f] for f in FIELDS] + ["Широта", "Долгота"])
            for n in db["nodes"]:
                row = [TYPES.get(n["type"], n["type"])]
                row += [n.get(f, "") for f in FIELDS[1:]]
                row += [n.get("lat") or "", n.get("lon") or ""]
                w.writerow(row)
            csv_bytes = ("\ufeff" + buf.getvalue()).encode("utf-8")
            self._send(200, csv_bytes, "text/csv; charset=utf-8",
                       {"Content-Disposition": "attachment; filename=cabtech_export.csv"})
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def _read_json(self):
        ln = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(ln).decode("utf-8")) if ln else {}

    def do_POST(self):
        db = load_db()
        body = self._read_json()
        if self.path == "/api/add":
            node = {f: str(body.get(f, "")).strip() for f in FIELDS}
            if not node["name"]:
                return self._send(400, json.dumps({"error": "name required"}))
            node["id"] = db["next_id"]; db["next_id"] += 1
            node["lat"] = body.get("lat"); node["lon"] = body.get("lon")
            if node["lat"] is None and node["city"]:
                xy = find_city_coord(node["city"], db)
                if xy: node["lat"], node["lon"] = xy
            db["nodes"].append(node); save_db(db)
            return self._send(200, json.dumps({"id": node["id"]}))
        if self.path == "/api/update":
            for n in db["nodes"]:
                if n["id"] == body.get("id"):
                    for f in FIELDS:
                        if f in body: n[f] = str(body[f]).strip()
                    n["lat"] = body.get("lat"); n["lon"] = body.get("lon")
                    if n["lat"] is None and n["city"]:
                        xy = find_city_coord(n["city"], db)
                        if xy: n["lat"], n["lon"] = xy
            save_db(db)
            return self._send(200, json.dumps({"ok": True}))
        if self.path == "/api/delete":
            db["nodes"] = [n for n in db["nodes"] if n["id"] != body.get("id")]
            save_db(db)
            return self._send(200, json.dumps({"ok": True}))
        if self.path == "/api/geocode":
            xy = find_city_coord(body.get("city", ""), db)
            if xy:
                return self._send(200, json.dumps({"ok": True, "lat": xy[0], "lon": xy[1], "matched": body["city"]}))
            return self._send(200, json.dumps({"ok": False}))
        if self.path == "/api/import":
            rows = list(csv.reader(io.StringIO(body.get("csv", ""))))
            if not rows:
                return self._send(400, json.dumps({"error": "пустой файл"}))
            header = [h.strip().lstrip("\ufeff") for h in rows[0]]
            label2field = {v: k for k, v in FIELD_LABELS.items()}
            idx = {}
            for j, h in enumerate(header):
                hl = h.rstrip("*").strip()
                if hl in label2field: idx[label2field[hl]] = j
                elif h in ("lat", "Широта"): idx["lat"] = j
                elif h in ("lon", "Долгота"): idx["lon"] = j
            if "name" not in idx:
                return self._send(400, json.dumps({"error": "нет колонки 'Название'"}))
            rev_types = {v: k for k, v in TYPES.items()}
            new_nodes = []
            for r in rows[1:]:
                if not any(c.strip() for c in r): continue
                get = lambda f: (r[idx[f]].strip() if f in idx and idx[f] < len(r) else "")
                t = get("type"); t = rev_types.get(t, t if t in TYPES else "supplier")
                node = {f: get(f) for f in FIELDS}
                node["type"] = t
                try:
                    node["lat"] = float(get("lat")) if get("lat") else None
                    node["lon"] = float(get("lon")) if get("lon") else None
                except ValueError:
                    node["lat"] = node["lon"] = None
                if node["lat"] is None and node["city"]:
                    xy = find_city_coord(node["city"], db)
                    if xy: node["lat"], node["lon"] = xy
                new_nodes.append(node)
            if body.get("replace"):
                db["nodes"] = new_nodes
            else:
                for n in new_nodes:
                    n["id"] = db["next_id"]; db["next_id"] += 1
                    db["nodes"].append(n)
            if body.get("replace"):
                for n in db["nodes"]:
                    n["id"] = db["next_id"]; db["next_id"] += 1
            save_db(db)
            return self._send(200, json.dumps({"ok": True, "count": len(new_nodes)}))
        self._send(404, json.dumps({"error": "not found"}))


def find_city_coord_named(city, db=None):  # обёртка для вызова с db
    return find_city_coord(city)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8050)
    args = ap.parse_args()
    init_db()
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"КАБТЕХ дашборд: http://localhost:{args.port}  (данные: {DB_PATH}, Ctrl+C — остановка)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
