#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
КАБТЕХ — логистический центр (v2).

Однофайловый сервер + интерфейс на чистом Python 3 (только стандартная библиотека).
База — SQLite (kabteh.db рядом со скриптом). При первом запуске объекты
автоматически мигрируются из db.json, если он рядом.

Запуск:  py kabteh.py [--port 8060]   ->  http://localhost:8060

Вкладки: Карта (объекты, маршруты, настройки линий), Грузы, Платежи,
Календарь (платёжный календарь + CSV), Настройки.
Экспорт объектов в CSV совместим с исходной таблицей v1.
Резервная копия — просто скопировать файл kabteh.db.
"""

import argparse
import json
import math
import os
import re
import sqlite3
import threading
import urllib.parse
import urllib.request
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "kabteh.db")
JSON_PATH = os.path.join(BASE, "db.json")
PORT_DEFAULT = 8060

TYPES = {"supplier": "Поставщик", "client": "Заказчик", "hub": "Хаб"}
REV_TYPES = {v: k for k, v in TYPES.items()}
OBJ_FIELDS = ["type", "name", "city", "inn", "kpp", "legal_addr", "wh_addr",
              "entry", "hours", "contact", "phone", "email", "notes"]
FIELD_LABELS = {
    "type": "Тип", "name": "Название", "search": "Название (поиск)", "city": "Город",
    "inn": "ИНН", "kpp": "КПП", "legal_addr": "Юр. адрес", "wh_addr": "Адрес склада",
    "entry": "Заезд", "hours": "Режим работы", "contact": "Контактное лицо",
    "phone": "Телефон", "email": "Email", "notes": "Примечания",
}
CARGO_STATUSES = ["Ожидает", "Готов", "В пути", "Доставлен"]
PAY_KINDS = ["аванс", "оплата", "возврат"]

# Базовые координаты городов (переносятся из db.json -> city_coords при миграции)
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

DB_LOCK = threading.Lock()


# ---------------------------------------------------------------- БД -------

def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS objects(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL DEFAULT 'supplier',
  name TEXT NOT NULL DEFAULT '',
  city TEXT DEFAULT '', address TEXT DEFAULT '', contact TEXT DEFAULT '',
  inn TEXT DEFAULT '', kpp TEXT DEFAULT '', wh_addr TEXT DEFAULT '',
  entry TEXT DEFAULT '', hours TEXT DEFAULT '', phone TEXT DEFAULT '',
  email TEXT DEFAULT '', note TEXT DEFAULT '',
  lat REAL, lng REAL, color TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS routes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  from_id INTEGER NOT NULL REFERENCES objects(id) ON DELETE CASCADE,
  to_ids TEXT NOT NULL DEFAULT '',
  label TEXT DEFAULT '', color TEXT DEFAULT '#2b7de9', visible INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY, value TEXT
);
CREATE TABLE IF NOT EXISTS cargos(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  description TEXT DEFAULT '',
  from_id INTEGER REFERENCES objects(id) ON DELETE SET NULL,
  to_id INTEGER REFERENCES objects(id) ON DELETE SET NULL,
  places INTEGER DEFAULT 1, weight REAL DEFAULT 0, volume REAL DEFAULT 0,
  ready_date TEXT DEFAULT '', ship_date TEXT DEFAULT '',
  status TEXT DEFAULT 'Ожидает', cost REAL DEFAULT 0, note TEXT DEFAULT '',
  created TEXT DEFAULT (date('now'))
);
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cargo_id INTEGER REFERENCES cargos(id) ON DELETE CASCADE,
  kind TEXT DEFAULT 'оплата', amount REAL DEFAULT 0, due_date TEXT DEFAULT '',
  paid_date TEXT, counterparty TEXT DEFAULT '', invoice TEXT DEFAULT '',
  upd TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def init_db():
    with DB_LOCK, connect() as con:
        con.executescript(SCHEMA_SQL)
        for k, v in [("show_lines", "1"), ("show_distances", "1"),
                     ("line_style", "straight"), ("font_size", "2")]:
            con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        n_obj = con.execute("SELECT COUNT(*) c FROM objects").fetchone()["c"]
        if n_obj == 0 and os.path.exists(JSON_PATH):
            migrate_json(con)
        # город -> координаты для автогеокода новых объектов
        n_city = con.execute("SELECT COUNT(*) c FROM meta WHERE key='city'").fetchone()["c"]
        if n_city == 0:
            for city, xy in CITY_COORDS.items():
                con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                            ("city:" + city, json.dumps(xy)))


def migrate_json(con):
    try:
        data = json.load(open(JSON_PATH, encoding="utf-8"))
    except Exception:
        return
    for node in data.get("nodes", []):
        con.execute(
            """INSERT INTO objects(id,type,name,city,address,contact,inn,kpp,wh_addr,
               entry,hours,phone,email,note,lat,lng)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (node.get("id"), node.get("type", "supplier"), node.get("name", ""),
             node.get("city", ""), node.get("legal_addr", ""), node.get("contact", ""),
             node.get("inn", ""), node.get("kpp", ""), node.get("wh_addr", ""),
             node.get("entry", ""), node.get("hours", ""), node.get("phone", ""),
             node.get("email", ""), node.get("notes", ""),
             node.get("lat"), node.get("lon")))
    seq = data.get("next_id", 100)
    con.execute("DELETE FROM sqlite_sequence WHERE name='objects'")
    con.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('objects',?)", (max(seq - 1, 0),))
    for city, xy in data.get("city_coords", {}).items():
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                    ("city:" + city, json.dumps(xy)))


def get_settings(con):
    return {r["key"]: r["value"] for r in con.execute("SELECT key,value FROM settings")}


def find_city_coord(con, city):
    """Локальный справочник + геокодер Nominatim (если есть интернет)."""
    if not city:
        return None
    norm = normalize_city(city)
    coords = {normalize_city(k): v for k, v in load_city_map(con).items()}
    for ck, cv in coords.items():
        if ck and (ck in norm or norm in ck):
            return cv
    xy = nominatim(city)
    if xy:
        con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                    ("city:" + city.strip(), json.dumps(list(xy))))
        return xy
    return None


def load_city_map(con):
    out = {}
    for r in con.execute("SELECT key,value FROM meta WHERE key LIKE 'city:%'"):
        out[r["key"][5:]] = json.loads(r["value"])
    return out


def normalize_city(s):
    s = (s or "").lower().strip()
    s = re.sub(r"\bгород\b|\bг\.\s*|\bпос(?:ёлок|елок)?\b|\bр-н\b.*|\bобл.*", " ", s)
    return re.sub(r"[^a-zёа-я0-9 -]", "", s).replace("  ", " ").strip()


def nominatim(query):
    try:
        url = ("https://nominatim.openstreetmap.org/search?format=json&limit=1&q="
               + urllib.parse.quote(query + ", Россия"))
        req = urllib.request.Request(url, headers={"User-Agent": "kabteh-dashboard/2.0"})
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode())
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception:
        pass
    return None


def haversine(a, b):
    R = 6371.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return round(2 * R * math.asin(math.sqrt(h)))


# ------------------------------------------------------------- API ---------

def api_list_objects(con):
    rows = [dict(r) for r in con.execute("SELECT * FROM objects ORDER BY id")]
    return rows


def api_save_object(con, body):
    fields = {f: str(body.get(f, "")).strip() for f in OBJ_FIELDS}
    lat, lng = body.get("lat"), body.get("lng")
    if lat is None and fields["city"]:
        xy = find_city_coord(con, fields["city"])
        if xy:
            lat, lng = float(xy[0]), float(xy[1])
    pid = body.get("id")
    if pid:
        con.execute(
            """UPDATE objects SET type=?,name=?,city=?,address=?,contact=?,inn=?,kpp=?,
               wh_addr=?,entry=?,hours=?,phone=?,email=?,note=?,lat=?,lng=? WHERE id=?""",
            (fields["type"], fields["name"], fields["city"], fields["legal_addr"],
             fields["contact"], fields["inn"], fields["kpp"], fields["wh_addr"],
             fields["entry"], fields["hours"], fields["phone"], fields["email"],
             fields["notes"], lat, lng, pid))
        return {"id": int(pid)}
    cur = con.execute(
        """INSERT INTO objects(type,name,city,address,contact,inn,kpp,wh_addr,entry,hours,
           phone,email,note,lat,lng) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (fields["type"], fields["name"], fields["city"], fields["legal_addr"],
         fields["contact"], fields["inn"], fields["kpp"], fields["wh_addr"],
         fields["entry"], fields["hours"], fields["phone"], fields["email"],
         fields["notes"], lat, lng))
    return {"id": cur.lastrowid}


def api_routes(con):
    out = []
    for r in con.execute("SELECT * FROM routes ORDER BY id"):
        d = dict(r)
        d["to_ids"] = [int(x) for x in str(d["to_ids"]).split(",") if x.strip()]
        pts = []
        o = con.execute("SELECT lat,lng,name FROM objects WHERE id=?", (d["from_id"],)).fetchone()
        if o:
            pts.append([o["lat"], o["lng"], o["name"]])
        for tid in d["to_ids"]:
            t = con.execute("SELECT lat,lng,name FROM objects WHERE id=?", (tid,)).fetchone()
            if t:
                pts.append([t["lat"], t["lng"], t["name"]])
        d["points"] = pts
        d["dist_km"] = sum(haversine(pts[i], pts[i + 1]) for i in range(len(pts) - 1)) if len(pts) > 1 else 0
        out.append(d)
    return out


def api_save_route(con, body):
    rid = body.get("id")
    if rid and "from_id" not in body:  # частичное обновление (visible/label/color)
        cur_r = con.execute("SELECT * FROM routes WHERE id=?", (rid,)).fetchone()
        if not cur_r:
            raise ValueError("route not found")
        vals = (cur_r["from_id"], cur_r["to_ids"],
                str(body["label"]) if "label" in body else cur_r["label"],
                str(body["color"]) if "color" in body else cur_r["color"],
                (1 if body.get("visible") else 0) if "visible" in body else cur_r["visible"])
        con.execute("UPDATE routes SET from_id=?,to_ids=?,label=?,color=?,visible=? WHERE id=?",
                    vals + (rid,))
        return {"id": int(rid)}
    to_ids = ",".join(str(int(t)) for t in body.get("to_ids", []))
    vals = (int(body["from_id"]), to_ids, str(body.get("label", "")),
            str(body.get("color", "#2b7de9")), 1 if body.get("visible", 1) else 0)
    if rid:
        con.execute("UPDATE routes SET from_id=?,to_ids=?,label=?,color=?,visible=? WHERE id=?",
                    vals + (rid,))
        return {"id": int(rid)}
    cur = con.execute("INSERT INTO routes(from_id,to_ids,label,color,visible) VALUES(?,?,?,?,?)", vals)
    return {"id": cur.lastrowid}


def api_cargos(con):
    q = """SELECT c.*, o1.name AS from_name, o2.name AS to_name FROM cargos c
           LEFT JOIN objects o1 ON o1.id=c.from_id LEFT JOIN objects o2 ON o2.id=c.to_id
           ORDER BY c.id DESC"""
    rows = [dict(r) for r in con.execute(q)]
    pay_sum = {}
    for r in con.execute("SELECT cargo_id, SUM(amount) s FROM payments GROUP BY cargo_id"):
        pay_sum[r["cargo_id"]] = r["s"]
    for c in rows:
        c["paid_sum"] = pay_sum.get(c["id"], 0)
    return rows


def api_save_cargo(con, body):
    def num(f, d=0):
        try:
            return float(body.get(f) or d)
        except (TypeError, ValueError):
            return d
    vals = dict(description=str(body.get("description", "")),
                from_id=body.get("from_id") or None, to_id=body.get("to_id") or None,
                places=int(num("places", 1)), weight=num("weight"), volume=num("volume"),
                ready_date=str(body.get("ready_date", "")), ship_date=str(body.get("ship_date", "")),
                status=body.get("status") if body.get("status") in CARGO_STATUSES else "Ожидает",
                cost=num("cost"), note=str(body.get("note", "")))
    cid = body.get("id")
    if cid:
        con.execute("""UPDATE cargos SET description=?,from_id=?,to_id=?,places=?,weight=?,
                       volume=?,ready_date=?,ship_date=?,status=?,cost=?,note=? WHERE id=?""",
                    tuple(vals.values()) + (cid,))
        return {"id": int(cid)}
    cur = con.execute("""INSERT INTO cargos(description,from_id,to_id,places,weight,volume,
                         ready_date,ship_date,status,cost,note) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                      tuple(vals.values()))
    return {"id": cur.lastrowid}


def api_payments(con, cargo_id=None):
    q = """SELECT p.*, c.description FROM payments p
           LEFT JOIN cargos c ON c.id=p.cargo_id"""
    args = ()
    if cargo_id:
        q += " WHERE p.cargo_id=?"
        args = (cargo_id,)
    q += " ORDER BY p.due_date, p.id"
    return [dict(r) for r in con.execute(q, args)]


def api_save_payment(con, body):
    pid = body.get("id")
    cur_p = None
    if pid:
        cur_p = con.execute("SELECT * FROM payments WHERE id=?", (pid,)).fetchone()
        if not cur_p:
            raise ValueError("payment not found")

    def pick(field, default=None):
        if field in body:
            return body[field]
        return cur_p[field] if cur_p is not None else default

    cargo_id = pick("cargo_id")
    kind = pick("kind", "оплата")
    if kind not in PAY_KINDS:
        kind = "оплата"
    try:
        amount = float(pick("amount", 0) or 0)
    except (TypeError, ValueError):
        amount = 0.0
    due_date = str(pick("due_date", "") or "")
    paid_date = pick("paid_date") or None
    cp = str(pick("counterparty", "") or "")
    inv = str(pick("invoice", "") or "")
    upd = str(pick("upd", "") or "")
    vals = (int(cargo_id) if cargo_id else None, kind, amount, due_date, paid_date, cp, inv, upd)
    if pid:
        con.execute("""UPDATE payments SET cargo_id=?,kind=?,amount=?,due_date=?,paid_date=?,
                       counterparty=?,invoice=?,upd=? WHERE id=?""", vals + (pid,))
        return {"id": int(pid)}
    cur = con.execute("""INSERT INTO payments(cargo_id,kind,amount,due_date,paid_date,
                         counterparty,invoice,upd) VALUES(?,?,?,?,?,?,?,?)""", vals)
    return {"id": cur.lastrowid}


def api_calendar(con):
    today = date.today().isoformat()
    months = {}
    for r in con.execute("SELECT * FROM payments ORDER BY due_date"):
        m = (r["due_date"] or "")[:7]
        if not m:
            m = "без срока"
        b = months.setdefault(m, {"expect": 0.0, "overdue": 0.0, "paid": 0.0})
        if r["paid_date"]:
            b["paid"] += r["amount"]
        elif r["due_date"] and r["due_date"] < today:
            b["overdue"] += r["amount"]
        else:
            b["expect"] += r["amount"]
    return {"today": today, "months": months}


# -------------------------------------------------------------- HTML -------

HTML = r"""<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>КАБТЕХ — логистический центр</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
:root{--fs:var(--f2)}
html{font-size:15px}
.fs1{font-size:13px}.fs2{font-size:15px}.fs3{font-size:17px}.fs4{font-size:19px}
*{box-sizing:border-box}
body{margin:0;font-family:'Segoe UI',system-ui,sans-serif;background:#eef1f5;color:#1c2430}
header{background:#12315e;color:#fff;padding:10px 18px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
header h1{font-size:1.15em;margin:0}
nav{display:flex;gap:6px;flex-wrap:wrap}
nav button{background:#1d4a8a;border:0;color:#dbe8ff;padding:7px 14px;border-radius:8px;cursor:pointer;font-size:.95em}
nav button.on{background:#fff;color:#12315e;font-weight:600}
main{padding:14px;max-width:1500px;margin:0 auto}
.card{background:#fff;border-radius:12px;box-shadow:0 1px 4px rgba(16,32,64,.12);padding:14px;margin-bottom:14px}
.grid{display:grid;grid-template-columns:380px 1fr;gap:14px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
#map{height:560px;border-radius:12px}
input,select,textarea{width:100%;padding:7px 9px;border:1px solid #c8d2e0;border-radius:8px;font-size:.95em;margin:3px 0;background:#fff;color:inherit}
label{font-size:.8em;color:#5a6b85;margin-top:6px;display:block}
button.btn{background:#1d4a8a;color:#fff;border:0;border-radius:8px;padding:8px 14px;cursor:pointer;font-size:.95em}
button.btn.red{background:#c0392b}button.btn.green{background:#1e8f4c}button.btn.gray{background:#7a8aa0}
button.mini{border:0;background:#eef2f8;border-radius:6px;padding:3px 7px;cursor:pointer}
table{width:100%;border-collapse:collapse;font-size:.9em}
th,td{padding:6px 8px;border-bottom:1px solid #e3e9f1;text-align:left;vertical-align:top}
th{color:#5a6b85;font-weight:600;font-size:.85em;text-transform:uppercase}
tr:hover td{background:#f4f8fd}
.tag{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.8em;background:#e3ecf9;color:#1d4a8a}
.st{padding:2px 8px;border-radius:10px;font-size:.8em;font-weight:600}
.s0{background:#fdf1d8;color:#a06a00}.s1{background:#dcf5e3;color:#1e8f4c}
.s2{background:#ddeafc;color:#1d4a8a}.s3{background:#eee;color:#666}
.hidden{display:none}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.sp{flex:1}
.objlist{max-height:420px;overflow:auto}
.objitem{padding:8px 10px;border-bottom:1px solid #edf1f7;cursor:pointer;border-radius:8px}
.objitem:hover{background:#f0f5fc}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:7px}
.rt{display:flex;align-items:center;gap:6px;padding:5px 8px;border-bottom:1px dashed #e3e9f1;font-size:.9em}
.cal{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px}
.month{border:1px solid #e3e9f1;border-radius:10px;padding:10px}
.badge{background:#c0392b;color:#fff;border-radius:10px;padding:1px 8px;font-size:.8em}
h2{font-size:1.05em;margin:0 0 10px}
.hint{font-size:.82em;color:#7a8aa0}
dialog{border:0;border-radius:14px;padding:0;max-width:640px;width:94%}
dialog::backdrop{background:rgba(10,20,40,.45)}
.dlgbody{padding:18px;max-height:75vh;overflow:auto}
</style></head><body>
<header>
 <h1>🚚 КАБТЕХ — логистический центр</h1>
 <nav id="nav"></nav>
 <div class="sp"></div>
 <span id="hint" class="hint" style="color:#bcd0ee"></span>
</header>
<main>

<section id="tab-map">
 <div class="grid">
  <div>
   <div class="card">
    <div class="row"><h2>Объекты</h2><div class="sp"></div>
     <button class="btn" onclick="openObj()">＋ Объект</button>
     <button class="btn green" onclick="openRoute()">＋ Маршрут</button></div>
    <input id="qobj" placeholder="Поиск по названию/городу…" oninput="renderObjs()">
    <div class="objlist" id="objlist"></div>
   </div>
   <div class="card"><h2>Маршруты</h2><div id="rtlist"></div>
    <div class="hint">Линии: прямые или ломаная через ближайший хаб — во «Настройках».</div></div>
  </div>
  <div class="card">
   <div class="row"><h2>Карта</h2><div class="sp"></div>
    <span class="hint" id="pickhint"></span></div>
   <div id="map"></div>
   <div class="hint">Клик по карте при открытом окне объекта/маршрута подставляет координаты / выбирает склады.</div>
  </div>
 </div>
</section>

<section id="tab-cargo" class="hidden">
 <div class="card">
  <div class="row"><h2>Грузы</h2><div class="sp"></div>
   <select id="fst" onchange="renderCargos()" style="width:auto">
    <option value="">Все статусы</option></select>
   <input id="fq" placeholder="Поиск…" oninput="renderCargos()" style="width:220px">
   <button class="btn" onclick="openCargo()">＋ Груз</button></div>
  <div style="overflow:auto"><table id="ctab"><thead><tr>
   <th>Описание</th><th>Откуда</th><th>Куда</th><th>Места</th><th>Вес, кг</th><th>Объём, м³</th>
   <th>Готов</th><th>Отгрузка</th><th>Статус</th><th>Стоимость</th><th></th></tr></thead><tbody></tbody></table></div>
  <div class="row" style="margin-top:8px"><b id="csum"></b><div class="sp"></div>
   <span class="hint">Итого стоимость непросроченных грузов</span></div>
 </div>
</section>

<section id="tab-pay" class="hidden">
 <div class="card">
  <div class="row"><h2>Платежи</h2><div class="sp"></div>
   <span id="paycnt" class="badge"></span>
   <button class="btn" onclick="openPay()">＋ Платёж</button></div>
  <div style="overflow:auto"><table id="ptab"><thead><tr>
   <th>Груз</th><th>Вид</th><th>Сумма</th><th>Срок</th><th>Оплачен</th><th></th>
   <th>Контрагент</th><th>Счёт</th><th>УПД</th><th></th></tr></thead><tbody></tbody></table></div>
 </div>
</section>

<section id="tab-cal" class="hidden">
 <div class="card">
  <div class="row"><h2>Платёжный календарь</h2><div class="sp"></div>
   <button class="btn gray" onclick="exportCalCSV()">Экспорт CSV</button></div>
  <div class="cal" id="calbox"></div>
 </div>
</section>

<section id="tab-set" class="hidden">
 <div class="card" style="max-width:560px">
  <h2>Настройки</h2>
  <label><input type="checkbox" id="s_lines" onchange="saveSet()"> Показывать линии маршрутов</label>
  <label><input type="checkbox" id="s_dist" onchange="saveSet()"> Подписи расстояний (км)</label>
  <label>Стиль линий
   <select id="s_style" onchange="saveSet()">
    <option value="straight">Прямые</option>
    <option value="via_hub">Ломаная через ближайший хаб</option></select></label>
  <label>Размер шрифта интерфейса
   <select id="s_font" onchange="saveSet()">
    <option value="1">Мелкий</option><option value="2">Обычный</option>
    <option value="3">Крупный</option><option value="4">Очень крупный</option></select></label>
  <div class="hint" style="margin-top:10px">Резервная копия: скопируйте файл kabteh.db.</div>
 </div>
</section>

</main>

<dialog id="dlgObj"><div class="dlgbody">
 <h2 id="objTitle">Объект</h2>
 <input type="hidden" id="o_id">
 <div class="grid2" style="display:grid;grid-template-columns:1fr 1fr;gap:0 10px">
 <div><label>Тип</label><select id="o_type"></select></div>
 <div><label>Название *</label><input id="o_name"></div>
 <div><label>Город</label><div class="row"><input id="o_city" class="sp"><button class="mini" title="Геокод по городу" onclick="geoCity()">🌐</button></div></div>
 <div><label>Координаты (клик по карте или вручную)</label>
   <div class="row"><input id="o_lat" placeholder="lat"><input id="o_lng" placeholder="lng"></div></div>
 <div><label>ИНН</label><input id="o_inn"></div>
 <div><label>КПП</label><input id="o_kpp"></div>
 </div>
 <label>Юр. адрес</label><input id="o_legal">
 <label>Адрес склада</label><input id="o_wh">
 <div class="row"><div class="sp"><label>Заезд</label><input id="o_entry"></div>
 <div class="sp"><label>Режим работы</label><input id="o_hours"></div></div>
 <label>Контактное лицо</label><input id="o_contact">
 <div class="row"><div class="sp"><label>Телефон</label><input id="o_phone"></div>
 <div class="sp"><label>Email</label><input id="o_email"></div></div>
 <label>Примечания</label><textarea id="o_notes" rows="3"></textarea>
 <div class="row" style="margin-top:10px">
  <button class="btn green" onclick="saveObj()">💾 Сохранить</button>
  <button class="btn gray" onclick="closeDlg('dlgObj')">Отмена</button>
  <div class="sp"></div>
  <button class="btn red hidden" id="o_del" onclick="delObj()">🗑 Удалить</button></div>
</div></dialog>

<dialog id="dlgRoute"><div class="dlgbody">
 <h2>Маршрут</h2>
 <label>Объект (откуда)</label><select id="r_from"></select>
 <label>Склады/точки (кликайте по списку или прямо по карте)</label>
 <div id="r_picks" class="objlist" style="max-height:180px;border:1px solid #e3e9f1;border-radius:8px;padding:6px"></div>
 <div class="row"><div class="sp"><label>Подпись</label><input id="r_label" placeholder="2 рейса/нед"></div>
 <div><label>Цвет</label><input type="color" id="r_color" value="#2b7de9" style="height:34px;padding:2px"></div></div>
 <div class="row" style="margin-top:10px">
  <button class="btn green" onclick="saveRoute()">💾 Сохранить</button>
  <button class="btn gray" onclick="closeDlg('dlgRoute')">Отмена</button></div>
</div></dialog>

<dialog id="dlgCargo"><div class="dlgbody">
 <h2 id="cgTitle">Груз</h2><input type="hidden" id="cg_id">
 <label>Описание *</label><input id="cg_desc">
 <div class="row">
  <div class="sp"><label>Откуда</label><select id="cg_from"></select></div>
  <div class="sp"><label>Куда</label><select id="cg_to"></select></div></div>
 <div class="row">
  <div><label>Места</label><input id="cg_places" type="number" value="1" min="0"></div>
  <div><label>Вес, кг</label><input id="cg_weight" type="number" step="0.01" value="0"></div>
  <div><label>Объём, м³</label><input id="cg_volume" type="number" step="0.01" value="0"></div></div>
 <div class="row">
  <div class="sp"><label>Дата готовности</label><input id="cg_ready" type="date"></div>
  <div class="sp"><label>План отгрузки</label><input id="cg_ship" type="date"></div>
  <div class="sp"><label>Статус</label><select id="cg_status"></select></div></div>
 <div class="row">
  <div class="sp"><label>Стоимость, ₽</label><input id="cg_cost" type="number" step="0.01" value="0"></div></div>
 <label>Примечания</label><textarea id="cg_note" rows="2"></textarea>
 <div class="row" style="margin-top:10px">
  <button class="btn green" onclick="saveCargo()">💾 Сохранить</button>
  <button class="btn gray" onclick="closeDlg('dlgCargo')">Отмена</button>
  <div class="sp"></div>
  <button class="btn red hidden" id="cg_del" onclick="delCargo()">🗑 Удалить</button></div>
</div></dialog>

<dialog id="dlgPay"><div class="dlgbody">
 <h2 id="pyTitle">Платёж</h2><input type="hidden" id="py_id">
 <label>Груз</label><select id="py_cargo"></select>
 <div class="row">
  <div class="sp"><label>Вид</label><select id="py_kind"></select></div>
  <div class="sp"><label>Сумма, ₽</label><input id="py_amount" type="number" step="0.01"></div></div>
 <div class="row">
  <div class="sp"><label>Срок</label><input id="py_due" type="date"></div>
  <div class="sp"><label>Факт оплаты</label><input id="py_paid" type="date"></div></div>
 <label>Контрагент</label><input id="py_cp">
 <div class="row">
  <div class="sp"><label>№ счёта</label><input id="py_inv"></div>
  <div class="sp"><label>№ УПД</label><input id="py_upd"></div></div>
 <div class="row" style="margin-top:10px">
  <button class="btn green" onclick="savePay()">💾 Сохранить</button>
  <button class="btn gray" onclick="closeDlg('dlgPay')">Отмена</button>
  <div class="sp"></div>
  <button class="btn red hidden" id="py_del" onclick="delPay()">🗑 Удалить</button></div>
</div></dialog>

<script>
const $=id=>document.getElementById(id);
let OBJECTS=[],ROUTES=[],CARGOS=[],PAYS=[],SET={},pickMode=null,routePicks=[];
const TYPECOLOR={supplier:'#e67e22',client:'#8e44ad',hub:'#1d4a8a'};
const TYPENAMES=['Поставщик','Заказчик','Хаб'];
const TABS=[['map','🗺 Карта'],['cargo','📦 Грузы'],['pay','💰 Платежи'],['cal','📅 Календарь'],['set','⚙ Настройки']];

async function api(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});return r.json();}
async function getJSON(path){const r=await fetch(path);return r.json();}

// ---------- загрузка ----------
async function reload(){
  [OBJECTS,ROUTES]=await Promise.all([getJSON('/api/objects'),getJSON('/api/routes')]);
  renderObjs();drawMap();renderRoutes();
}
async function reloadCargos(){CARGOS=await getJSON('/api/cargos');renderCargos();}
async function reloadPays(){PAYS=await getJSON('/api/payments');renderPays();renderCal();}

// ---------- навигация ----------
const nav=$('nav');
TABS.forEach(([id,lb])=>{const b=document.createElement('button');b.textContent=lb;b.dataset.t=id;
 b.onclick=()=>showTab(id);nav.appendChild(b);});
function showTab(t){
 document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on',b.dataset.t==t));
 ['map','cargo','pay','cal','set'].forEach(x=>$('tab-'+x).classList.toggle('hidden',x!=t));
 if(t=='cargo')reloadCargos(); if(t=='pay'||t=='cal')reloadPays();
 if(t=='map')setTimeout(()=>map.invalidateSize(),50);
}
showTab('map');

// ---------- карта ----------
const map=L.map('map').setView([55.3,42],5);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'© OpenStreetMap'}).addTo(map);
let lineLayer=L.layerGroup().addTo(map);
map.on('click',e=>{
  if(pickMode=='obj'){$('o_lat').value=e.latlng.lat.toFixed(5);$('o_lng').value=e.latlng.lng.toFixed(5);}
  if(pickMode=='route'){const near=nearestObject(e.latlng);if(near&&near.type!='supplier'){togglePick(near.id);}else if(near){togglePick(near.id)}}
});
function nearestObject(ll){let best=null,bd=1e9;for(const o of OBJECTS){if(o.lat==null)continue;
  const d=map.distance(ll,[o.lat,o.lng]);if(d<bd){bd=d;best=o}}return bd<50000?best:null;}
const markers=L.layerGroup().addTo(map);
function drawMap(){
 markers.clearLayers();lineLayer.clearLayers();
 for(const o of OBJECTS){if(o.lat==null)continue;
  L.circleMarker([o.lat,o.lng],{radius:8,color:TYPECOLOR[o.type]||'#888',fillOpacity:.9})
   .addTo(markers).bindPopup(popupHtml(o));}
 if(SET.show_lines=='1'){
  for(const r of ROUTES){if(!r.visible)continue;
   let pts=r.points.filter(p=>p[0]!=null).map(p=>[p[0],p[1]]);
   if(SET.line_style=='via_hub'&&pts.length>=2)pts=viaHubs(r);
   L.polyline(pts,{color:r.color,weight:3,opacity:.85}).addTo(lineLayer);
   if(SET.show_distances=='1'&&pts.length>1){
    const mid=pts[Math.floor(pts.length/2)];
    L.marker(mid,{icon:L.divIcon({className:'',html:`<div style="background:#fff;border:1px solid ${r.color};border-radius:8px;padding:1px 6px;font-size:11px;white-space:nowrap">${r.dist_km} км</div>`})}).addTo(lineLayer);}
  }
 }
}
function viaHubs(r){ // ломаная: объект -> ближайший хаб -> склад
 const hubs=OBJECTS.filter(o=>o.type=='hub'&&o.lat!=null);
 const pts=r.points.filter(p=>p[0]!=null).map(p=>[p[0],p[1]]);
 if(!hubs.length||pts.length<2)return pts;
 const out=[pts[0]];
 for(let i=1;i<pts.length;i++){
  let bh=null,bd=1e9;for(const h of hubs){const d=Math.hypot((h.lat-pts[i][0])*111,(h.lng-pts[i][1])*60);if(d<bd){bd=d;bh=h}}
  if(bh)out.push([bh.lat,bh.lng]);out.push(pts[i]);}
 return out;}
function popupHtml(o){const esc=s=>String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;');
 return `<b>${esc(o.name)}</b><br><small>${TYPENAMES[TYPENAMES.indexOf(labelType(o.type))]||esc(o.type)}</small><br>`+
  [o.city&&`📍 ${esc(o.city)}`,o.phone&&`📞 ${esc(o.phone)}`,o.contact&&`👤 ${esc(o.contact)}`,
   o.note&&`📝 ${esc(o.note)}`].filter(Boolean).join('<br>')+
  `<br><a href="#" onclick="openObj(${o.id});return false">✏️ Открыть карточку</a>`;}
function labelType(t){return {supplier:'Поставщик',client:'Заказчик',hub:'Хаб'}[t]||t;}

// ---------- список объектов ----------
function renderObjs(){
 const q=($('qobj').value||'').toLowerCase();
 const box=$('objlist');box.innerHTML='';
 for(const o of OBJECTS){
  if(q&&!((o.name+' '+o.city).toLowerCase().includes(q)))continue;
  const d=document.createElement('div');d.className='objitem';
  d.innerHTML=`<span class="dot" style="background:${TYPECOLOR[o.type]}"></span><b>${esc(o.name)}</b>
   <div class="hint">${esc(o.city||'')}</div>`;
  d.onclick=()=>{if(o.lat!=null)map.setView([o.lat,o.lng],9);openObj(o.id);};
  box.appendChild(d);}
}
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;')}

// ---------- карточка объекта ----------
function fillSel(el,arr,cur){el.innerHTML='<option value="">—</option>'+arr.map(v=>`<option ${v==cur?'selected':''}>${v}</option>`).join('');}
function openObj(id){
 pickMode='obj';$('pickhint').textContent='🖱 клик по карте = координаты';
 const o=OBJECTS.find(x=>x.id==id)||{};
 $('objTitle').textContent=id?'Объект #'+id:'Новый объект';
 $('o_id').value=o.id||'';$('o_del').classList.toggle('hidden',!id);
 fillSel($('o_type'),TYPENAMES,{supplier:'Поставщик',client:'Заказчик',hub:'Хаб'}[o.type]||'Поставщик');
 $('o_name').value=o.name||'';$('o_city').value=o.city||'';
 $('o_lat').value=o.lat??'';$('o_lng').value=o.lng??'';
 $('o_inn').value=o.inn||'';$('o_kpp').value=o.kpp||'';$('o_legal').value=o.address||'';
 $('o_wh').value=o.wh_addr||'';$('o_entry').value=o.entry||'';$('o_hours').value=o.hours||'';
 $('o_contact').value=o.contact||'';$('o_phone').value=o.phone||'';$('o_email').value=o.email||'';
 $('o_notes').value=o.note||'';
 $('dlgObj').showModal();
}
async function saveObj(){
 const t2={'Поставщик':'supplier','Заказчик':'client','Хаб':'hub'}[$('o_type').value];
 const body={id:$('o_id').value||undefined,type:t2,name:$('o_name').value.trim(),
  city:$('o_city').value.trim(),inn:$('o_inn').value,kpp:$('o_kpp').value,
  legal_addr:$('o_legal').value,wh_addr:$('o_wh').value,entry:$('o_entry').value,
  hours:$('o_hours').value,contact:$('o_contact').value,phone:$('o_phone').value,
  email:$('o_email').value,notes:$('o_notes').value,
  lat:$('o_lat').value?parseFloat($('o_lat').value):null,
  lng:$('o_lng').value?parseFloat($('o_lng').value):null};
 if(!body.name){alert('Заполните название');return;}
 await api('/api/object',body);pickMode=null;$('pickhint').textContent='';
 closeDlg('dlgObj');await reload();
}
async function delObj(){if(!confirm('Удалить объект и его маршруты?'))return;
 await api('/api/delete',{kind:'object',id:+$('o_id').value});
 closeDlg('dlgObj');await reload();}
async function geoCity(){
 const r=await api('/api/geocode',{city:$('o_city').value});
 if(r.ok){$('o_lat').value=r.lat;$('o_lng').value=r.lng;$('hint').textContent='🌐 '+r.matched;}
 else $('hint').textContent='Город не найден (нет интернета или неизвестен)';
 setTimeout(()=>$('hint').textContent='',3000);
}
function closeDlg(id){$(id).close();pickMode=null;$('pickhint').textContent='';}

// ---------- маршруты ----------
function openRoute(){
 pickMode='route';routePicks=[];$('pickhint').textContent='🖱 кликайте по карте — точки маршрута';
 const sel=$('r_from');sel.innerHTML=OBJECTS.map(o=>`<option value="${o.id}">${esc(o.name)}</option>`).join('');
 renderPicks();$('dlgRoute').showModal();
}
function togglePick(id){const i=routePicks.indexOf(id);if(i>=0)routePicks.splice(i,1);else routePicks.push(id);renderPicks();}
function renderPicks(){
 $('r_picks').innerHTML=OBJECTS.map(o=>`<div class="rt"><label style="margin:0"><input type="checkbox" style="width:auto"
  ${routePicks.includes(o.id)?'checked':''} onchange="togglePick(${o.id})">
  <span class="dot" style="background:${TYPECOLOR[o.type]}"></span>${esc(o.name)}</label></div>`).join('');
}
async function saveRoute(){
 if(!routePicks.length){alert('Выберите хотя бы один склад/точку');return;}
 await api('/api/route',{from_id:+$('r_from').value,to_ids:routePicks,
  label:$('r_label').value,color:$('r_color').value,visible:1});
 closeDlg('dlgRoute');await reload();
}
function renderRoutes(){
 const box=$('rtlist');
 box.innerHTML=ROUTES.map(r=>{
  const f=OBJECTS.find(o=>o.id==r.from_id),ts=r.to_ids.map(id=>(OBJECTS.find(o=>o.id==id)||{}).name||'?');
  return `<div class="rt"><span class="dot" style="background:${r.color}"></span>
   <span class="sp">${esc(f?f.name:'?')} → ${ts.map(esc).join(', ')}${r.label?' · '+esc(r.label):''} · <b>${r.dist_km} км</b></span>
   <button class="mini" title="${r.visible?'Скрыть':'Показать'}" onclick="toggleRoute(${r.id},${1-r.visible})">${r.visible?'👁':'🙈'}</button>
   <button class="mini" title="Удалить" onclick="delRoute(${r.id})">✖</button></div>`;}).join('')||'<div class="hint">Нет маршрутов</div>';
}
async function toggleRoute(id,v){await api('/api/route',{id,visible:v});await reload();}
async function delRoute(id){if(!confirm('Удалить маршрут?'))return;await api('/api/delete',{kind:'route',id});await reload();}

// ---------- грузы ----------
function stClass(s){return {'Ожидает':'s0','Готов':'s1','В пути':'s2','Доставлен':'s3'}[s]||'s3';}
function renderCargos(){
 const fst=$('fst');if(fst.options.length<2)CARGO_ST.forEach(s=>fst.add(new Option(s,s)));
 const q=($('fq').value||'').toLowerCase(),st=fst.value;
 const tb=$('ctab').querySelector('tbody');tb.innerHTML='';let sum=0;
 for(const c of CARGOS){
  if(st&&c.status!=st)continue;
  if(q&&!((c.description+' '+(c.from_name||'')+' '+(c.to_name||'')).toLowerCase().includes(q)))continue;
  if(c.status!='Доставлен')sum+=c.cost;
  tb.insertAdjacentHTML('beforeend',`<tr><td>${esc(c.description)}<div class="hint">${esc(c.note||'')}</div></td>
   <td>${esc(c.from_name||'—')}</td><td>${esc(c.to_name||'—')}</td><td>${c.places}</td>
   <td>${num(c.weight)}</td><td>${num(c.volume)}</td><td>${c.ready_date||'—'}</td><td>${c.ship_date||'—'}</td>
   <td><span class="st ${stClass(c.status)}">${c.status}</span></td><td>${num(c.cost)} ₽</td>
   <td><button class="mini" onclick="openCargo(${c.id})">✏️</button></td></tr>`);}
 $('csum').textContent='Итого: '+fmt(sum)+' ₽';
}
const CARGO_ST=['Ожидает','Готов','В пути','Доставлен'];
function num(v){return (Math.round((v||0)*100)/100).toLocaleString('ru-RU');}
function fmt(v){return Math.round(v).toLocaleString('ru-RU');}
function objOptions(sel,cur){sel.innerHTML='<option value="">—</option>'+OBJECTS.map(o=>`<option value="${o.id}" ${o.id==cur?'selected':''}>${esc(o.name)}</option>`).join('');}
function openCargo(id){
 const c=CARGOS.find(x=>x.id==id)||{};
 $('cgTitle').textContent=id?'Груз #'+id:'Новый груз';$('cg_id').value=c.id||'';$('cg_del').classList.toggle('hidden',!id);
 $('cg_desc').value=c.description||'';objOptions($('cg_from'),c.from_id);objOptions($('cg_to'),c.to_id);
 $('cg_places').value=c.places??1;$('cg_weight').value=c.weight||0;$('cg_volume').value=c.volume||0;
 $('cg_ready').value=c.ready_date||'';$('cg_ship').value=c.ship_date||'';
 fillSel($('cg_status'),CARGO_ST,c.status||'Ожидает');
 $('cg_cost').value=c.cost||0;$('cg_note').value=c.note||'';
 $('dlgCargo').showModal();
}
async function saveCargo(){
 const body={id:$('cg_id').value||undefined,description:$('cg_desc').value.trim(),
  from_id:$('cg_from').value||null,to_id:$('cg_to').value||null,
  places:+$('cg_places').value||0,weight:+$('cg_weight').value||0,volume:+$('cg_volume').value||0,
  ready_date:$('cg_ready').value,ship_date:$('cg_ship').value,status:$('cg_status').value,
  cost:+$('cg_cost').value||0,note:$('cg_note').value};
 if(!body.description){alert('Заполните описание');return;}
 await api('/api/cargo',body);closeDlg('dlgCargo');await reloadCargos();
}
async function delCargo(){if(!confirm('Удалить груз и его платежи?'))return;
 await api('/api/delete',{kind:'cargo',id:+$('cg_id').value});closeDlg('dlgCargo');await reloadCargos();}

// ---------- платежи ----------
async function renderPays(){
 const cnt=PAYS.filter(p=>!p.paid_date).length;
 $('paycnt').textContent=cnt?`к оплате: ${cnt}`:'всё оплачено ✓';
 const tb=$('ptab').querySelector('tbody');tb.innerHTML='';
 const today=new Date().toISOString().slice(0,10);
 for(const p of PAYS){
  const over=!p.paid_date&&p.due_date&&p.due_date<today;
  tb.insertAdjacentHTML('beforeend',`<tr>
   <td>${esc(p.description||'—')}</td><td>${p.kind}</td><td>${fmt(p.amount)} ₽</td>
   <td>${p.due_date||'—'}${over?' <span class="badge">просрочен</span>':''}</td>
   <td>${p.paid_date||''}</td>
   <td>${p.paid_date?'':`<button class="mini" title="Отметить оплаченным" onclick="markPaid(${p.id})">✓</button>`}</td>
   <td>${esc(p.counterparty)}</td><td>${esc(p.invoice)}</td><td>${esc(p.upd)}</td>
   <td><button class="mini" onclick="openPay(${p.id})">✏️</button>
       <button class="mini" onclick="delPayId(${p.id})">✖</button></td></tr>`);}
}
function openPay(id){
 const p=PAYS.find(x=>x.id==id)||{};
 $('pyTitle').textContent=id?'Платёж #'+id:'Новый платёж';$('py_id').value=p.id||'';$('py_del').classList.toggle('hidden',!id);
 $('py_cargo').innerHTML='<option value="">— без груза —</option>'+CARGOS.map(c=>`<option value="${c.id}" ${c.id==p.cargo_id?'selected':''}>#${c.id} ${esc(c.description)}</option>`).join('');
 fillSel($('py_kind'),['аванс','оплата','возврат'],p.kind||'оплата');
 $('py_amount').value=p.amount||'';$('py_due').value=p.due_date||'';$('py_paid').value=p.paid_date||'';
 $('py_cp').value=p.counterparty||'';$('py_inv').value=p.invoice||'';$('py_upd').value=p.upd||'';
 $('dlgPay').showModal();
}
async function savePay(){
 await api('/api/payment',{id:$('py_id').value||undefined,cargo_id:$('py_cargo').value||null,
  kind:$('py_kind').value,amount:+$('py_amount').value||0,due_date:$('py_due').value,
  paid_date:$('py_paid').value||null,counterparty:$('py_cp').value,invoice:$('py_inv').value,upd:$('py_upd').value});
 closeDlg('dlgPay');await reloadPays();
}
async function markPaid(id){await api('/api/payment',{id,paid_date:new Date().toISOString().slice(0,10)});await reloadPays();}
async function delPay(){await api('/api/delete',{kind:'payment',id:+$('py_id').value});closeDlg('dlgPay');await reloadPays();}
async function delPayId(id){if(!confirm('Удалить платёж?'))return;await api('/api/delete',{kind:'payment',id});await reloadPays();}

// ---------- календарь ----------
let CAL={};
async function renderCal(){
 CAL=await getJSON('/api/calendar');
 const box=$('calbox');box.innerHTML='';
 const names=['янв','фев','мар','апр','май','июн','июл','авг','сен','окт','ноя','дек'];
 const keys=Object.keys(CAL.months).sort();
 if(!keys.length){box.innerHTML='<div class="hint">Нет платежей</div>';return;}
 for(const m of keys){
  const b=CAL.months[m];
  const lbl=m.length==7?`${names[+m.slice(5)-1]} ${m.slice(0,4)}`:m;
  box.insertAdjacentHTML('beforeend',`<div class="month"><b>${lbl}</b>
   <table><tr><td class="hint">Ожидает</td><td>${fmt(b.expect)} ₽</td></tr>
   <tr><td class="hint" style="color:#c0392b">Просрочено</td><td style="color:#c0392b">${fmt(b.overdue)} ₽</td></tr>
   <tr><td class="hint" style="color:#1e8f4c">Оплачено</td><td style="color:#1e8f4c">${fmt(b.paid)} ₽</td></tr></table></div>`);}
}
function exportCalCSV(){
 let csv='\uFEFFМесяц;Ожидает;Просрочено;Оплачено\n';
 for(const m of Object.keys(CAL.months).sort()){const b=CAL.months[m];
  csv+=`${m};${b.expect};${b.overdue};${b.paid}\n`;}
 dl(csv,'calendar.csv');
}
function dl(text,name){const a=document.createElement('a');
 a.href=URL.createObjectURL(new Blob([text],{type:'text/csv;charset=utf-8'}));a.download=name;a.click();}

// ---------- настройки ----------
async function loadSet(){SET=await getJSON('/api/settings');
 $('s_lines').checked=SET.show_lines=='1';$('s_dist').checked=SET.show_distances=='1';
 $('s_style').value=SET.line_style||'straight';$('s_font').value=SET.font_size||'2';applyFont();}
function applyFont(){document.body.className='fs'+(SET.font_size||'2');setTimeout(()=>map.invalidateSize(),10);}
async function saveSet(){
 const body={show_lines:$('s_lines').checked?1:0,show_distances:$('s_dist').checked?1:0,
  line_style:$('s_style').value,font_size:$('s_font').value};
 await api('/api/settings',body);SET=body;applyFont();drawMap();
}

// старт
(async()=>{await loadSet();await reload();})();
</script></body></html>"""


# ------------------------------------------------------------ SERVER ---------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, data, ctype="application/json; charset=utf-8"):
        body = data.encode("utf-8") if isinstance(data, str) else data
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            return {}

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._send(200, HTML, "text/html; charset=utf-8")
        with DB_LOCK, connect() as con:
            if path == "/api/objects":
                return self._send(200, json.dumps(api_list_objects(con), ensure_ascii=False))
            if path == "/api/routes":
                return self._send(200, json.dumps(api_routes(con), ensure_ascii=False))
            if path == "/api/cargos":
                return self._send(200, json.dumps(api_cargos(con), ensure_ascii=False))
            if path == "/api/payments":
                return self._send(200, json.dumps(api_payments(con), ensure_ascii=False))
            if path == "/api/calendar":
                return self._send(200, json.dumps(api_calendar(con), ensure_ascii=False))
            if path == "/api/settings":
                return self._send(200, json.dumps(get_settings(con), ensure_ascii=False))
            if path == "/export/objects.csv":
                return self._send(200, objects_csv(con), "text/csv; charset=utf-8")
        self._send(404, '{"error":"not found"}')

    def do_POST(self):
        body = self._read_body()
        path = self.path.split("?")[0]
        with DB_LOCK, connect() as con:
            try:
                if path == "/api/object":
                    res = api_save_object(con, body)
                elif path == "/api/route":
                    res = api_save_route(con, body)
                elif path == "/api/cargo":
                    res = api_save_cargo(con, body)
                elif path == "/api/payment":
                    res = api_save_payment(con, body)
                elif path == "/api/settings":
                    for k in ("show_lines", "show_distances", "line_style", "font_size"):
                        if k in body:
                            con.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                        (k, str(body[k])))
                    res = {"ok": True}
                elif path == "/api/geocode":
                    xy = find_city_coord(con, body.get("city", ""))
                    res = {"ok": bool(xy)}
                    if xy:
                        res.update(lat=xy[0], lng=xy[1], matched=body.get("city"))
                elif path == "/api/delete":
                    kind, table = body.get("kind"), {
                        "object": "objects", "route": "routes",
                        "cargo": "cargos", "payment": "payments"}.get(body.get("kind"))
                    if not table:
                        return self._send(400, '{"error":"kind?"}')
                    con.execute(f"DELETE FROM {table} WHERE id=?", (body.get("id"),))
                    res = {"ok": True}
                else:
                    return self._send(404, '{"error":"not found"}')
                con.commit()
                return self._send(200, json.dumps(res, ensure_ascii=False))
            except Exception as e:  # noqa: BLE001
                return self._send(500, json.dumps({"error": str(e)}))


def objects_csv(con):
    labels = ["№", "Тип", "Название (поиск)", "Название", "Город", "ИНН", "КПП",
              "Юр. адрес", "Адрес склада", "Заезд", "Режим работы", "Контактное лицо",
              "Телефон", "Email", "Примечания", "lat", "lon"]
    lines = ["\uFEFF" + ";".join(labels)]
    for o in api_list_objects(con):
        row = [str(o["id"]), TYPES.get(o["type"], o["type"]), o["name"], o["name"], o["city"],
               o["inn"], o["kpp"], o["address"], o["wh_addr"], o["entry"], o["hours"],
               o["contact"], o["phone"], o["email"], o["note"],
               "" if o["lat"] is None else o["lat"], "" if o["lng"] is None else o["lng"]]
        lines.append(";".join('"{}"'.format(str(c).replace('"', '""')) for c in row))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT_DEFAULT)
    args = ap.parse_args()
    init_db()
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"КАБТЕХ v2: http://localhost:{args.port}  (база: {DB_PATH}, остановка — Ctrl+C)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
