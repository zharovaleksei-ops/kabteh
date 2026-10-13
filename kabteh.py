#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
КАБТЕХ — логистический центр (v3).

Однофайловый сервер + интерфейс на чистом Python 3 (только стандартная библиотека).
База — SQLite (kabteh.db рядом со скриптом). При первом запуске объекты
автоматически мигрируются из db.json, если он рядом.

Запуск:  py kabteh.py [--port 8060]   ->  http://localhost:8060

v3 по ТЗ: правые панели (Платежи сверху, Грузы снизу) вместо вкладок;
карточки грузов/платежей; двусторонняя связь маршрут<->груз; контакты и
транспорт; темная/светлая тема; дата/время в шапке; экспорт календаря в шапке;
уведомления (сегодня платеж / завтра отгрузка / за 3 дня); фильтры карты;
пакетный ввод платежей (предоплата/доплата/постоплата, суммы или %);
маршрут создаётся и редактируется через карточку; линии — тонкий пунктир.
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
from datetime import date, datetime, timedelta
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
CREATE TABLE IF NOT EXISTS contacts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  obj_id INTEGER REFERENCES objects(id) ON DELETE SET NULL,
  name TEXT DEFAULT '', position TEXT DEFAULT '', phone TEXT DEFAULT '',
  email TEXT DEFAULT '', note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS carriers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL DEFAULT '', phone TEXT DEFAULT '', email TEXT DEFAULT '',
  site TEXT DEFAULT '', modes TEXT DEFAULT '', note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def _add_col(con, table, col, decl):
    cols = [r["name"] for r in con.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db():
    with DB_LOCK, connect() as con:
        con.executescript(SCHEMA_SQL)
        # v3: связи маршрут <-> груз
        _add_col(con, "cargos", "route_id", "INTEGER REFERENCES routes(id) ON DELETE SET NULL")
        _add_col(con, "routes", "auto_cargo", "INTEGER DEFAULT 1")
        for k, v in [("show_lines", "1"), ("show_distances", "1"),
                     ("line_style", "straight"), ("font_size", "2"),
                     ("theme", "light"), ("notify", "1"),
                     ("filter_types", "supplier,client,hub"),
                     ("filter_colors", "")]:
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
    auto_cargo = 1 if body.get("auto_cargo", 1) else 0
    vals = (int(body["from_id"]), to_ids, str(body.get("label", "")),
            str(body.get("color", "#2b7de9")), 1 if body.get("visible", 1) else 0, auto_cargo)
    if rid:
        con.execute("UPDATE routes SET from_id=?,to_ids=?,label=?,color=?,visible=?,auto_cargo=? WHERE id=?",
                    vals + (rid,))
        res = {"id": int(rid)}
        # при редактировании маршрута обновляем привязанные грузы
        con.execute("""UPDATE cargos SET from_id=?, to_id=? WHERE route_id=?""",
                    (int(body["from_id"]),
                     int(body["to_ids"][0]) if body.get("to_ids") else None, rid))
        return res
    cur = con.execute("INSERT INTO routes(from_id,to_ids,label,color,visible,auto_cargo) VALUES(?,?,?,?,?,?)", vals)
    rid = cur.lastrowid
    res = {"id": rid}
    # v3: создание маршрута автоматически рождает груз
    if auto_cargo:
        f = con.execute("SELECT name,city FROM objects WHERE id=?", (vals[0],)).fetchone()
        first_to = int(body["to_ids"][0]) if body.get("to_ids") else None
        t = con.execute("SELECT name,city FROM objects WHERE id=?", (first_to,)).fetchone() if first_to else None
        desc = "Груз: %s → %s" % ((f["name"] if f else "?"), (t["name"] if t else "?"))
        c2 = con.execute("""INSERT INTO cargos(description,from_id,to_id,places,status,note,route_id)
                            VALUES(?,?,?,?,?,?,?)""",
                         (desc, vals[0], first_to, 1, "Ожидает",
                          "Создан автоматически из маршрута #%d" % rid, rid))
        res["cargo_id"] = c2.lastrowid
    return res


def api_cargos(con):
    q = """SELECT c.*, o1.name AS from_name, o1.city AS from_city,
           o2.name AS to_name, o2.city AS to_city, r.label AS route_label, r.color AS route_color
           FROM cargos c
           LEFT JOIN objects o1 ON o1.id=c.from_id LEFT JOIN objects o2 ON o2.id=c.to_id
           LEFT JOIN routes r ON r.id=c.route_id ORDER BY c.id DESC"""
    rows = [dict(r) for r in con.execute(q)]
    pay_sum = {}
    for r in con.execute("SELECT cargo_id, SUM(amount) s FROM payments WHERE paid_date IS NOT NULL GROUP BY cargo_id"):
        pay_sum[r["cargo_id"]] = r["s"]
    for c in rows:
        c["paid_sum"] = pay_sum.get(c["id"], 0) or 0
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
    route_id = body.get("route_id") or None
    if cid:
        con.execute("""UPDATE cargos SET description=?,from_id=?,to_id=?,places=?,weight=?,
                       volume=?,ready_date=?,ship_date=?,status=?,cost=?,note=?,route_id=? WHERE id=?""",
                    tuple(vals.values()) + (route_id, cid))
        return {"id": int(cid)}
    cur = con.execute("""INSERT INTO cargos(description,from_id,to_id,places,weight,volume,
                         ready_date,ship_date,status,cost,note,route_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                      tuple(vals.values()) + (route_id,))
    res = {"id": cur.lastrowid}
    # v3: груз с from/to может родить маршрут (если include_route и есть обе точки)
    if body.get("include_route") and vals["from_id"] and vals["to_id"]:
        exists = con.execute("SELECT id FROM routes WHERE from_id=? AND to_ids LIKE ?",
                             (vals["from_id"], "%s%%" % vals["to_id"])).fetchone()
        if not exists:
            c2 = con.execute("INSERT INTO routes(from_id,to_ids,label,color,visible,auto_cargo) "
                             "VALUES(?,?,?,?,?,0)",
                             (vals["from_id"], str(vals["to_id"]),
                              "Из груза #%d" % res["id"], "#2b7de9", 1))
            rid = c2.lastrowid
            con.execute("UPDATE cargos SET route_id=? WHERE id=?", (rid, res["id"]))
            res["route_id"] = rid
    return res


def api_payments(con, cargo_id=None):
    q = """SELECT p.*, c.description, c.cost AS cargo_cost,
           o1.name AS from_name, o2.name AS to_name, o2.city AS to_city,
           r.color AS route_color, r.label AS route_label
           FROM payments p
           LEFT JOIN cargos c ON c.id=p.cargo_id
           LEFT JOIN objects o1 ON o1.id=c.from_id
           LEFT JOIN objects o2 ON o2.id=c.to_id
           LEFT JOIN routes r ON r.id=c.route_id"""
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


KIND_BY_STAGE = {"pre": "аванс", "mid": "оплата", "post": "возврат"}


def api_save_pay_batch(con, body):
    """v3: пакет — предоплата/доплата/постоплата. Режимы: sums или percent от cost."""
    cargo_id = body.get("cargo_id")
    if not cargo_id:
        raise ValueError("cargo_id required")
    mode = body.get("mode", "sums")
    total = None
    if mode == "percent":
        c = con.execute("SELECT cost FROM cargos WHERE id=?", (cargo_id,)).fetchone()
        if not c:
            raise ValueError("cargo not found")
        total = float(c["cost"] or 0)
        pcts = [float(body.get(k, 0) or 0) for k in ("pre_pct", "mid_pct", "post_pct")]
        if abs(sum(pcts) - 100) > 0.01:
            raise ValueError("Сумма процентов должна быть 100 (сейчас %g)" % sum(pcts))
        amounts = [round(total * p / 100.0, 2) for p in pcts]
    else:
        amounts = [float(body.get(k, 0) or 0) for k in ("pre_sum", "mid_sum", "post_sum")]
    dates = [str(body.get(k, "") or "") for k in ("pre_date", "mid_date", "post_date")]
    created = []
    for stage, amount, due in zip(("pre", "mid", "post"), amounts, dates):
        if amount <= 0:
            continue
        cur = con.execute("""INSERT INTO payments(cargo_id,kind,amount,due_date,paid_date,
                             counterparty,invoice,upd) VALUES(?,?,?,?,?,?,?,?)""",
                          (int(cargo_id), KIND_BY_STAGE[stage], amount, due, None, "", "", ""))
        created.append(cur.lastrowid)
    return {"ids": created, "count": len(created)}


def api_contacts(con):
    rows = [dict(r) for r in con.execute(
        """SELECT ct.*, o.name AS obj_name FROM contacts ct
           LEFT JOIN objects o ON o.id=ct.obj_id ORDER BY ct.id""")]
    return rows


def api_save_contact(con, body):
    vals = dict(obj_id=body.get("obj_id") or None, name=str(body.get("name", "")),
                position=str(body.get("position", "")), phone=str(body.get("phone", "")),
                email=str(body.get("email", "")), note=str(body.get("note", "")))
    cid = body.get("id")
    if cid:
        con.execute("""UPDATE contacts SET obj_id=?,name=?,position=?,phone=?,email=?,note=?
                       WHERE id=?""", tuple(vals.values()) + (cid,))
        return {"id": int(cid)}
    cur = con.execute("INSERT INTO contacts(obj_id,name,position,phone,email,note) VALUES(?,?,?,?,?,?)",
                      tuple(vals.values()))
    return {"id": cur.lastrowid}


def api_carriers(con):
    return [dict(r) for r in con.execute("SELECT * FROM carriers ORDER BY name")]


def api_save_carrier(con, body):
    vals = dict(name=str(body.get("name", "")), phone=str(body.get("phone", "")),
                email=str(body.get("email", "")), site=str(body.get("site", "")),
                modes=str(body.get("modes", "")), note=str(body.get("note", "")))
    cid = body.get("id")
    if cid:
        con.execute("""UPDATE carriers SET name=?,phone=?,email=?,site=?,modes=?,note=?
                       WHERE id=?""", tuple(vals.values()) + (cid,))
        return {"id": int(cid)}
    cur = con.execute("INSERT INTO carriers(name,phone,email,site,modes,note) VALUES(?,?,?,?,?,?)",
                      tuple(vals.values()))
    return {"id": cur.lastrowid}


def api_events(con):
    """v3: уведомления — платежи сегодня/просроченные, отгрузки завтра, события за N дней."""
    today = date.today()
    t = today.isoformat()
    d1 = (today + timedelta(days=1)).isoformat()
    d3 = (today + timedelta(days=3)).isoformat()
    events = []
    for p in con.execute("""SELECT p.*, c.description, o.name AS cp_name FROM payments p
                            LEFT JOIN cargos c ON c.id=p.cargo_id
                            LEFT JOIN objects o ON o.id=c.from_id WHERE p.paid_date IS NULL"""):
        due = p["due_date"] or ""
        if not due:
            continue
        if due < t:
            events.append({"kind": "overdue", "date": due, "text":
                           "Просрочен платёж: %s (%s ₽) — %s" % (p["description"] or "без груза",
                                                                  fmt_num(p["amount"]), p["kind"])})
        elif due == t:
            events.append({"kind": "pay_today", "date": due, "text":
                           "Сегодня платёж (%s): %s — %s ₽" % (p["kind"], p["description"] or "—",
                                                                fmt_num(p["amount"]))})
        elif due <= d3:
            events.append({"kind": "pay_soon", "date": due, "text":
                           "Платёж %s: %s — %s ₽ (через %d дн.)" % (
                               due, p["description"] or "—", fmt_num(p["amount"]),
                               days_between(t, due))})
    for c in con.execute("""SELECT c.*, o1.name AS fn, o2.name AS tn FROM cargos c
                            LEFT JOIN objects o1 ON o1.id=c.from_id
                            LEFT JOIN objects o2 ON o2.id=c.to_id
                            WHERE c.status IN ('Ожидает','Готов')"""):
        sd = c["ship_date"] or ""
        rd = c["ready_date"] or ""
        if sd and sd == t:
            events.append({"kind": "ship_today", "date": sd, "text":
                           "Сегодня отгрузка: %s (%s → %s)" % (c["description"], c["fn"] or "?", c["tn"] or "?")})
        elif sd and sd == d1:
            events.append({"kind": "ship_tomorrow", "date": sd, "text":
                           "Завтра отгрузка: %s (%s → %s)" % (c["description"], c["fn"] or "?", c["tn"] or "?")})
        elif sd and sd <= d3:
            events.append({"kind": "ship_soon", "date": sd, "text":
                           "Отгрузка %s: %s (через %d дн.)" % (sd, c["description"], days_between(t, sd))})
        if rd and rd == t and c["status"] == "Ожидает":
            events.append({"kind": "ready_today", "date": rd, "text":
                           "Сегодня готов груз: %s" % c["description"]})
    events.sort(key=lambda e: e["date"])
    return {"today": t, "events": events}


def days_between(a, b):
    da = datetime.strptime(a, "%Y-%m-%d").date()
    db = datetime.strptime(b, "%Y-%m-%d").date()
    return (db - da).days


def fmt_num(v):
    try:
        return "{:,.0f}".format(float(v or 0)).replace(",", " ")
    except (TypeError, ValueError):
        return str(v)


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
html{font-size:15px}
.fs1{font-size:13px}.fs2{font-size:15px}.fs3{font-size:17px}.fs4{font-size:19px}
*{box-sizing:border-box}
:root{--bg:#eef1f5;--card:#fff;--ink:#1c2430;--mut:#5a6b85;--line:#e3e9f1;--hover:#f4f8fd;
--hdr:#12315e;--nav:#1d4a8a;--navink:#dbe8ff;--inp:#fff;--inpline:#c8d2e0;--mini:#eef2f8}
body.dark{--bg:#12161d;--card:#1b222c;--ink:#dbe4f0;--mut:#8fa1b8;--line:#2a3441;--hover:#232d3a;
--hdr:#0b1220;--nav:#1d4a8a;--navink:#dbe8ff;--inp:#232d3a;--inpline:#3a4656;--mini:#2a3441}
body{margin:0;font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--ink)}
header{background:var(--hdr);color:#fff;padding:8px 18px;display:flex;align-items:center;gap:14px;flex-wrap:wrap;position:sticky;top:0;z-index:1200}
header h1{font-size:1.1em;margin:0;white-space:nowrap}
#clock{font-size:.95em;color:#bcd0ee;font-variant-numeric:tabular-nums;white-space:nowrap}
nav{display:flex;gap:6px;flex-wrap:wrap}
nav button{background:var(--nav);border:0;color:var(--navink);padding:6px 13px;border-radius:8px;cursor:pointer;font-size:.92em}
nav button.on{background:#fff;color:#12315e;font-weight:600}
main{padding:12px;max-width:1700px;margin:0 auto}
.card{background:var(--card);border-radius:12px;box-shadow:0 1px 4px rgba(16,32,64,.12);padding:12px;margin-bottom:12px}
.grid{display:grid;grid-template-columns:360px minmax(420px,1fr) 340px;gap:12px;align-items:start}
@media(max-width:1250px){.grid{grid-template-columns:320px 1fr}.sidecol{grid-column:1/-1;display:grid;grid-template-columns:1fr 1fr;gap:12px}}
@media(max-width:900px){.grid{grid-template-columns:1fr}.sidecol{grid-template-columns:1fr}}
#map{height:560px;border-radius:12px}
input,select,textarea{width:100%;padding:6px 8px;border:1px solid var(--inpline);border-radius:8px;font-size:.93em;margin:3px 0;background:var(--inp);color:var(--ink)}
label{font-size:.78em;color:var(--mut);margin-top:5px;display:block}
button.btn{background:var(--nav);color:#fff;border:0;border-radius:8px;padding:7px 13px;cursor:pointer;font-size:.92em}
button.btn.red{background:#c0392b}button.btn.green{background:#1e8f4c}button.btn.gray{background:#7a8aa0}
button.mini{border:0;background:var(--mini);color:inherit;border-radius:6px;padding:3px 7px;cursor:pointer}
table{width:100%;border-collapse:collapse;font-size:.88em}
th,td{padding:5px 7px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:.82em;text-transform:uppercase}
tr:hover td{background:var(--hover)}
.st{padding:2px 7px;border-radius:10px;font-size:.78em;font-weight:600;white-space:nowrap}
.s0{background:#fdf1d8;color:#a06a00}.s1{background:#dcf5e3;color:#1e8f4c}
.s2{background:#ddeafc;color:#1d4a8a}.s3{background:#eee;color:#666}
.hidden{display:none}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.sp{flex:1}
.objlist{max-height:300px;overflow:auto}
.pillist{max-height:250px;overflow:auto}
.objitem{padding:5px 8px;border-bottom:1px solid var(--line);cursor:pointer;border-radius:6px;font-size:.9em}
.objitem:hover{background:var(--hover)}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;border:2px solid #fff;box-shadow:0 0 0 1px rgba(0,0,0,.15),0 0 6px 1px currentColor}
.rt{display:flex;align-items:center;gap:6px;padding:4px 6px;border-bottom:1px dashed var(--line);font-size:.86em}
.badge{background:#c0392b;color:#fff;border-radius:10px;padding:1px 8px;font-size:.8em}
h2{font-size:1em;margin:0 0 8px}
.hint{font-size:.78em;color:var(--mut)}
dialog{border:0;border-radius:14px;padding:0;max-width:600px;width:94%;background:var(--card);color:var(--ink)}
dialog::backdrop{background:rgba(10,20,40,.45)}
.dlgbody{padding:16px;max-height:78vh;overflow:auto}
/* маркеры-лампочки */
.bulb{position:relative;width:20px;height:20px}
.bulb i{position:absolute;left:2px;top:2px;width:16px;height:16px;border-radius:50%;background:currentColor;border:2.5px solid #fff;box-shadow:0 0 0 1px rgba(0,0,0,.25),0 0 10px 2px currentColor;animation:pulse 2.4s ease-in-out infinite}
@keyframes pulse{0%,100%{filter:brightness(.85)}50%{filter:brightness(1.5)}}
/* панель фильтров на карте */
#mapfilters{position:absolute;top:10px;right:10px;z-index:800;background:var(--card);opacity:.95;border-radius:10px;padding:8px 10px;box-shadow:0 2px 8px rgba(0,0,0,.25);font-size:.82em;max-width:230px}
#mapfilters label{display:inline-flex;align-items:center;gap:4px;margin:2px 8px 2px 0;font-size:1em;color:var(--ink)}
#mapfilters .dot{width:9px;height:9px;margin:0}
.payrow,.carow{padding:6px 8px;border-bottom:1px solid var(--line);cursor:pointer;border-radius:6px;font-size:.86em;line-height:1.35}
.payrow:hover,.carow:hover{background:var(--hover)}
.days{float:right;font-size:.85em;padding:1px 7px;border-radius:9px;background:var(--mini);color:var(--mut);white-space:nowrap}
.days.hot{background:#fdecea;color:#c0392b;font-weight:700}
.days.ok{background:#dcf5e3;color:#1e8f4c}
.paidline{text-decoration:line-through;opacity:.55}
#notifbox{position:fixed;right:16px;bottom:16px;z-index:1500;width:340px;max-height:60vh;overflow:auto;background:var(--card);border-radius:12px;box-shadow:0 4px 18px rgba(0,0,0,.3);padding:12px}
#notifbox h3{margin:0 0 8px;font-size:.95em}
.ev{padding:6px 8px;border-left:4px solid #999;border-radius:6px;margin-bottom:6px;font-size:.85em;background:var(--hover)}
.ev.overdue,.ev.pay_today{border-color:#c0392b}
.ev.pay_soon{border-color:#e67e22}
.ev.ship_today,.ev.ship_tomorrow{border-color:#1d4a8a}
.ev.ship_soon,.ev.ready_today{border-color:#1e8f4c}
</style></head><body>
<header>
 <h1>🚚 КАБТЕХ</h1>
 <span id="clock"></span>
 <nav id="nav"></nav>
 <div class="sp"></div>
 <button class="btn gray" onclick="exportCalCSV()" title="Экспорт платёжного календаря в CSV">📅 Календарь CSV</button>
 <button class="btn gray" onclick="toggleTheme()" id="themeBtn" title="Тёмная/светлая тема">🌙</button>
 <button class="btn gray" onclick="showEvents()" title="Уведомления">🔔<span id="evcnt"></span></button>
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
   <div class="card"><h2>Маршруты</h2><div class="pillist" id="rtlist"></div>
    <div class="hint">Клик по маршруту — редактирование карточки.</div></div>
  </div>
  <div class="card" style="position:relative;padding:10px">
   <div class="row"><h2>Карта</h2><div class="sp"></div>
    <span class="hint" id="pickhint"></span></div>
   <div id="map" style="position:relative">
    <div id="mapfilters"></div>
   </div>
   <div class="hint">Клик по карте при открытом окне объекта/маршрута подставляет координаты / выбирает точки.</div>
  </div>
  <div class="sidecol">
   <div class="card">
    <div class="row"><h2>💰 Платежи</h2><div class="sp"></div>
     <button class="btn" title="Пакет: предоплата/доплата/постоплата" onclick="openBatch()">＋ пакет</button>
     <span id="paycnt" class="badge"></span></div>
    <div class="pillist" id="paylist"></div>
   </div>
   <div class="card">
    <div class="row"><h2>📦 Грузы</h2><div class="sp"></div>
     <button class="btn" onclick="openCargo()">＋</button></div>
    <div class="pillist" id="carolist"></div>
   </div>
  </div>
 </div>
</section>

<section id="tab-contacts" class="hidden">
 <div class="card">
  <div class="row"><h2>Контакты (менеджеры)</h2><div class="sp"></div>
   <input id="qcon" placeholder="Поиск…" oninput="renderCons()" style="width:220px">
   <button class="btn" onclick="openCon()">＋ Контакт</button></div>
  <div style="overflow:auto"><table id="contab"><thead><tr>
   <th>Имя</th><th>Должность</th><th>Организация</th><th>Телефон</th><th>Email</th><th>Примечание</th><th></th></tr></thead><tbody></tbody></table></div>
 </div>
</section>

<section id="tab-carriers" class="hidden">
 <div class="card">
  <div class="row"><h2>Транспортные компании</h2><div class="sp"></div>
   <input id="qcar" placeholder="Поиск…" oninput="renderCars()" style="width:220px">
   <button class="btn" onclick="openCar()">＋ Перевозчик</button></div>
  <div style="overflow:auto"><table id="cartab"><thead><tr>
   <th>Название</th><th>Телефон</th><th>Email</th><th>Сайт</th><th>Модальные окна</th><th>Примечание</th><th></th></tr></thead><tbody></tbody></table></div>
 </div>
</section>

<section id="tab-set" class="hidden">
 <div class="card" style="max-width:600px">
  <h2>Настройки</h2>
  <label><input type="checkbox" id="s_lines" onchange="saveSet()"> Показывать линии маршрутов (тонкий пунктир)</label>
  <label><input type="checkbox" id="s_dist" onchange="saveSet()"> Подписи расстояний (км)</label>
  <label><input type="checkbox" id="s_notify" onchange="saveSet()"> Уведомления о платежах и отгрузках</label>
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

<div id="notifbox" class="hidden">
 <div class="row"><h3>🔔 Уведомления</h3><div class="sp"></div>
 <button class="mini" onclick="$('notifbox').classList.add('hidden')">✖</button></div>
 <div id="evlist"></div>
</div>

<dialog id="dlgObj"><div class="dlgbody">
 <h2 id="objTitle">Объект</h2>
 <input type="hidden" id="o_id">
 <div style="display:grid;grid-template-columns:1fr 1fr;gap:0 10px">
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
 <label>Примечания</label><textarea id="o_notes" rows="2"></textarea>
 <div class="row" style="margin-top:8px">
  <button class="btn green" onclick="saveObj()">💾 Сохранить</button>
  <button class="btn gray" onclick="closeDlg('dlgObj')">Отмена</button>
  <div class="sp"></div>
  <button class="btn red hidden" id="o_del" onclick="delObj()">🗑 Удалить</button></div>
</div></dialog>

<dialog id="dlgRoute"><div class="dlgbody">
 <h2 id="rtTitle">Маршрут</h2><input type="hidden" id="r_id">
 <label>Объект (откуда)</label><select id="r_from"></select>
 <label>Склады/точки (кликайте по списку или прямо по карте)</label>
 <div id="r_picks" class="pillist" style="max-height:160px;border:1px solid var(--line);border-radius:8px;padding:5px"></div>
 <div class="row"><div class="sp"><label>Подпись</label><input id="r_label" placeholder="2 рейса/нед"></div>
 <div><label>Цвет</label><input type="color" id="r_color" value="#2b7de9" style="height:32px;padding:2px"></div></div>
 <label><input type="checkbox" id="r_autocargo" style="width:auto" checked> создать груз из маршрута</label>
 <div class="row" style="margin-top:8px">
  <button class="btn green" onclick="saveRoute()">💾 Сохранить</button>
  <button class="btn gray" onclick="closeDlg('dlgRoute')">Отмена</button>
  <div class="sp"></div>
  <button class="btn red hidden" id="r_del" onclick="delRouteBtn()">🗑 Удалить</button></div>
</div></dialog>

<dialog id="dlgCargo"><div class="dlgbody">
 <h2 id="cgTitle">Груз</h2><input type="hidden" id="cg_id">
 <label>Описание *</label><input id="cg_desc">
 <div class="row">
  <div class="sp"><label>Откуда (поставщик)</label><select id="cg_from"></select></div>
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
  <div class="sp"><label>Стоимость, ₽</label><input id="cg_cost" type="number" step="0.01" value="0"></div>
  <div class="sp"><label>Маршрут</label><select id="cg_route"></select></div></div>
 <label><input type="checkbox" id="cg_mkroute" style="width:auto"> создать маршрут по этим точкам</label>
 <label>Примечания</label><textarea id="cg_note" rows="2"></textarea>
 <div class="row" style="margin-top:8px">
  <button class="btn green" onclick="saveCargo()">💾 Сохранить</button>
  <button class="btn gray" onclick="closeDlg('dlgCargo')">Отмена</button>
  <div class="sp"></div>
  <button class="btn red hidden" id="cg_del" onclick="delCargo()">🗑 Удалить</button>
  <button class="btn" onclick="closeDlg('dlgCargo');openBatch($('cg_id').value)">💰 Пакет платежей</button></div>
</div></dialog>

<dialog id="dlgPay"><div class="dlgbody">
 <h2 id="pyTitle">Платёж</h2><input type="hidden" id="py_id">
 <label>Груз</label><select id="py_cargo" onchange="payCargoHint()"></select>
 <div id="py_cargohint" class="hint" style="min-height:1.2em"></div>
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
 <div class="row" style="margin-top:8px">
  <button class="btn green" onclick="savePay()">💾 Сохранить</button>
  <button class="btn gray" onclick="closeDlg('dlgPay')">Отмена</button>
  <div class="sp"></div>
  <button class="btn red hidden" id="py_del" onclick="delPay()">🗑 Удалить</button></div>
</div></dialog>

<dialog id="dlgBatch"><div class="dlgbody">
 <h2>Пакет платежей по грузу</h2>
 <label>Груз</label><select id="b_cargo" onchange="batchCalc()"></select>
 <div id="b_cargohint" class="hint" style="min-height:1.2em"></div>
 <div class="row" style="margin-top:6px">
  <label style="margin:0"><input type="radio" name="bmode" value="sums" checked onchange="batchMode()"> Суммы, ₽</label>
  <label style="margin:0"><input type="radio" name="bmode" value="percent" onchange="batchMode()"> Проценты от стоимости</label>
  <div class="sp"></div><b id="b_total"></b></div>
 <table style="margin-top:6px"><thead><tr><th>Этап</th><th>Сумма</th><th>%</th><th>Дата</th></tr></thead><tbody>
 <tr><td>Предоплата (аванс)</td><td><input id="b_pre_sum" type="number" step="0.01" oninput="batchCalc()"></td>
     <td><input id="b_pre_pct" type="number" step="1" oninput="batchCalc()"></td>
     <td><input id="b_pre_date" type="date"></td></tr>
 <tr><td>Доплата (оплата)</td><td><input id="b_mid_sum" type="number" step="0.01" oninput="batchCalc()"></td>
     <td><input id="b_mid_pct" type="number" step="1" oninput="batchCalc()"></td>
     <td><input id="b_mid_date" type="date"></td></tr>
 <tr><td>Постоплата (возврат)</td><td><input id="b_post_sum" type="number" step="0.01" oninput="batchCalc()"></td>
     <td><input id="b_post_pct" type="number" step="1" oninput="batchCalc()"></td>
     <td><input id="b_post_date" type="date"></td></tr>
 </tbody></table>
 <div class="hint" id="b_err"></div>
 <div class="row" style="margin-top:8px">
  <button class="btn green" onclick="saveBatch()">➕ Добавить платежи</button>
  <button class="btn gray" onclick="closeDlg('dlgBatch')">Отмена</button></div>
</div></dialog>

<dialog id="dlgCon"><div class="dlgbody">
 <h2 id="cnTitle">Контакт</h2><input type="hidden" id="cn_id">
 <label>Имя *</label><input id="cn_name">
 <label>Должность</label><input id="cn_pos" placeholder="менеджер, логист…">
 <label>Организация (объект из базы)</label><select id="cn_obj"></select>
 <div class="row"><div class="sp"><label>Телефон</label><input id="cn_phone"></div>
 <div class="sp"><label>Email</label><input id="cn_email"></div></div>
 <label>Примечание</label><input id="cn_note">
 <div class="row" style="margin-top:8px">
  <button class="btn green" onclick="saveCon()">💾 Сохранить</button>
  <button class="btn gray" onclick="closeDlg('dlgCon')">Отмена</button>
  <div class="sp"></div>
  <button class="btn red hidden" id="cn_del" onclick="delCon()">🗑 Удалить</button></div>
</div></dialog>

<dialog id="dlgCar"><div class="dlgbody">
 <h2 id="crTitle">Перевозчик</h2><input type="hidden" id="cr_id">
 <label>Название *</label><input id="cr_name">
 <div class="row"><div class="sp"><label>Телефон</label><input id="cr_phone"></div>
 <div class="sp"><label>Email</label><input id="cr_email"></div></div>
 <div class="row"><div class="sp"><label>Сайт</label><input id="cr_site"></div>
 <div class="sp"><label>Модальные окна</label><input id="cr_modes" placeholder="FTL, LTL, сборные…"></div></div>
 <label>Примечание</label><textarea id="cr_note" rows="2"></textarea>
 <div class="row" style="margin-top:8px">
  <button class="btn green" onclick="saveCar()">💾 Сохранить</button>
  <button class="btn gray" onclick="closeDlg('dlgCar')">Отмена</button>
  <div class="sp"></div>
  <button class="btn red hidden" id="cr_del" onclick="delCar()">🗑 Удалить</button></div>
</div></dialog>
<script>
const $=id=>document.getElementById(id);
let OBJECTS=[],ROUTES=[],CARGOS=[],PAYS=[],CONS=[],CARS=[],SET={},pickMode=null,routePicks=[];
const TYPECOLOR={supplier:'#e67e22',client:'#8e44ad',hub:'#1d4a8a'};
const TYPENAMES=['Поставщик','Заказчик','Хаб'];
const TABS=[['map','🗺 Карта'],['contacts','👤 Контакты'],['carriers','🚛 Транспорт'],['set','⚙ Настройки']];
let FILTER={types:{supplier:true,client:true,hub:true},colors:null};

async function api(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});return r.json();}
async function getJSON(path){const r=await fetch(path);return r.json();}
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;')}
function num(v){return (Math.round((v||0)*100)/100).toLocaleString('ru-RU');}
function fmt(v){return Math.round(v||0).toLocaleString('ru-RU');}
function todayStr(){return new Date().toISOString().slice(0,10);}
function daysTo(d){if(!d)return null;const t=new Date(todayStr());const x=new Date(d);return Math.round((x-t)/864e5);}
function dayLabel(n){if(n==null)return'';if(n<0)return 'просрочен '+(-n)+' дн.';if(n===0)return 'сегодня';if(n===1)return 'завтра';return 'через '+n+' дн.';}

// ---------- загрузка ----------
async function reload(){
  [OBJECTS,ROUTES,CARGOS,PAYS]=await Promise.all([getJSON('/api/objects'),getJSON('/api/routes'),getJSON('/api/cargos'),getJSON('/api/payments')]);
  renderObjs();drawMap();renderRoutes();renderPayList();renderCaoList();
}

// ---------- навигация ----------
const nav=$('nav');
TABS.forEach(([id,lb])=>{const b=document.createElement('button');b.textContent=lb;b.dataset.t=id;
 b.onclick=()=>showTab(id);nav.appendChild(b);});
function showTab(t){
 document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('on',b.dataset.t==t));
 ['map','contacts','carriers','set'].forEach(x=>$('tab-'+x).classList.toggle('hidden',x!=t));
 if(t=='contacts')loadCons(); if(t=='carriers')loadCars();
 if(t=='map')setTimeout(()=>map.invalidateSize(),50);
}
showTab('map');

// ---------- часы ----------
const WD=['вс','пн','вт','ср','чт','пт','сб'];
const MN=['янв','фев','мар','апр','мая','июн','июл','авг','сен','окт','ноя','дек'];
function tickClock(){const d=new Date();
 $('clock').textContent=`${WD[d.getDay()]}, ${d.getDate()} ${MN[d.getMonth()]} ${d.getFullYear()} · ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`;}
tickClock();setInterval(tickClock,15000);

// ---------- тема ----------
function applyTheme(){document.body.classList.toggle('dark',SET.theme=='dark');$('themeBtn').textContent=SET.theme=='dark'?'☀️':'🌙';setTimeout(()=>map.invalidateSize(),10);}
async function toggleTheme(){SET.theme=SET.theme=='dark'?'light':'dark';applyTheme();await api('/api/settings',{theme:SET.theme});}

// ---------- карта ----------
const map=L.map('map').setView([55.3,42],5);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{attribution:'© OpenStreetMap'}).addTo(map);
let lineLayer=L.layerGroup().addTo(map);
map.on('click',e=>{
  if(pickMode=='obj'){$('o_lat').value=e.latlng.lat.toFixed(5);$('o_lng').value=e.latlng.lng.toFixed(5);}
  if(pickMode=='route'){const near=nearestObject(e.latlng);if(near){togglePick(near.id);}}
});
function nearestObject(ll){let best=null,bd=1e9;for(const o of OBJECTS){if(o.lat==null||!objVisible(o))continue;
  const d=map.distance(ll,[o.lat,o.lng]);if(d<bd){bd=d;best=o}}return bd<50000?best:null;}
function objVisible(o){if(!FILTER.types[o.type])return false;
  if(FILTER.colors){return ROUTES.some(r=>r.color==FILTER.colors&&visibleRoute(r)&&(r.from_id==o.id||r.to_ids.includes(o.id)));}
  return true;}
function visibleRoute(r){if(!r.visible)return false;if(FILTER.colors)return r.color==FILTER.colors;return true;}
const markers=L.layerGroup().addTo(map);
function bulbIcon(color){return L.divIcon({className:'',html:`<div class="bulb" style="color:${color}"><i></i></div>`,iconSize:[20,20],iconAnchor:[10,10]});}
function drawMap(){
 markers.clearLayers();lineLayer.clearLayers();
 for(const o of OBJECTS){if(o.lat==null||!objVisible(o))continue;
  L.marker([o.lat,o.lng],{icon:bulbIcon(TYPECOLOR[o.type]||'#888')})
   .addTo(markers).bindPopup(popupHtml(o));}
 if(SET.show_lines=='1'){
  for(const r of ROUTES){if(!visibleRoute(r))continue;
   let pts=r.points.filter(p=>p[0]!=null).map(p=>[p[0],p[1]]);
   if(SET.line_style=='via_hub'&&pts.length>=2)pts=viaHubs(r);
   L.polyline(pts,{color:r.color,weight:2,opacity:.8,dashArray:'6 6',lineCap:'round'}).addTo(lineLayer);
   if(SET.show_distances=='1'&&pts.length>1){
    const mid=pts[Math.floor(pts.length/2)];
    L.marker(mid,{icon:L.divIcon({className:'',html:`<div style="background:var(--card);border:1px solid ${r.color};border-radius:8px;padding:0 5px;font-size:10px;white-space:nowrap">${r.dist_km} км</div>`})}).addTo(lineLayer);}
  }
 }
}
function viaHubs(r){
 const hubs=OBJECTS.filter(o=>o.type=='hub'&&o.lat!=null);
 const pts=r.points.filter(p=>p[0]!=null).map(p=>[p[0],p[1]]);
 if(!hubs.length||pts.length<2)return pts;
 const out=[pts[0]];
 for(let i=1;i<pts.length;i++){
  let bh=null,bd=1e9;for(const h of hubs){const d=Math.hypot((h.lat-pts[i][0])*111,(h.lng-pts[i][1])*60);if(d<bd){bd=d;bh=h}}
  if(bh)out.push([bh.lat,bh.lng]);out.push(pts[i]);}
 return out;}
function popupHtml(o){
 return `<b>${esc(o.name)}</b><br><small>${esc(labelType(o.type))}</small><br>`+
  [o.city&&`📍 ${esc(o.city)}`,o.phone&&`📞 ${esc(o.phone)}`,o.contact&&`👤 ${esc(o.contact)}`,
   o.note&&`📝 ${esc(o.note)}`].filter(Boolean).join('<br>')+
  `<br><a href="#" onclick="openObj(${o.id});return false">✏️ Открыть карточку</a>`;}
function labelType(t){return {supplier:'Поставщик',client:'Заказчик',hub:'Хаб'}[t]||t;}

// ---------- фильтры карты ----------
function renderFilters(){
 const colors=[...new Set(ROUTES.map(r=>r.color))];
 let h='<b style="font-size:.9em">Фильтры</b><br>';
 for(const tp of ['supplier','client','hub']){
  h+=`<label><input type="checkbox" style="width:auto" ${FILTER.types[tp]?'checked':''} onchange="togType('${tp}',this.checked)"><span class="dot" style="background:${TYPECOLOR[tp]};color:${TYPECOLOR[tp]}"></span>${labelType(tp)}</label>`;}
 h+=`<br><label><input type="checkbox" style="width:auto" ${FILTER.colors?'checked':''} onchange="togColorFilter(this.checked)"> только цвет маршрутов:</label>`;
 h+=colors.map(c=>`<button class="mini" title="только ${c}" style="color:#fff;background:${c};border-radius:50%;width:18px;height:18px;padding:0" onclick="setColorFilter('${c}')"></button>`).join(' ');
 $('mapfilters').innerHTML=h;}
function togType(tp,v){FILTER.types[tp]=v;saveFilters();drawMap();renderObjs();renderRoutes();}
function togColorFilter(v){if(!v)FILTER.colors=null;else{const cs=[...new Set(ROUTES.map(r=>r.color))];FILTER.colors=cs[0]||null;}saveFilters();drawMap();renderRoutes();renderFilters();}
function setColorFilter(c){FILTER.colors=(FILTER.colors==c)?null:c;saveFilters();drawMap();renderRoutes();renderFilters();}
function saveFilters(){api('/api/settings',{filter_types:Object.keys(FILTER.types).filter(k=>FILTER.types[k]).join(','),filter_colors:FILTER.colors||''});}

// ---------- список объектов ----------
function renderObjs(){
 const q=($('qobj').value||'').toLowerCase();
 const box=$('objlist');box.innerHTML='';
 for(const o of OBJECTS){
  if(!objVisible(o))continue;
  if(q&&!((o.name+' '+o.city).toLowerCase().includes(q)))continue;
  const d=document.createElement('div');d.className='objitem';
  d.innerHTML=`<span class="dot" style="background:${TYPECOLOR[o.type]};color:${TYPECOLOR[o.type]}"></span><b>${esc(o.name)}</b> <span class="hint">${esc(o.city||'')}</span>`;
  d.onclick=()=>{if(o.lat!=null)map.setView([o.lat,o.lng],9);openObj(o.id);};
  box.appendChild(d);}
}

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

// ---------- маршруты: карточка создания/редактирования ----------
function openRoute(id){
 pickMode='route';routePicks=[];
 const r=id?ROUTES.find(x=>x.id==id):null;
 $('rtTitle').textContent=id?'Маршрут #'+id:'Новый маршрут';
 $('r_id').value=id||'';$('r_del').classList.toggle('hidden',!id);
 $('r_label').value=r?(r.label||''):'';$('r_color').value=r?r.color:'#2b7de9';
 $('r_autocargo').checked=r?!!r.auto_cargo:true;
 const sel=$('r_from');sel.innerHTML=OBJECTS.map(o=>`<option value="${o.id}" ${r&&r.from_id==o.id?'selected':''}>${esc(o.name)}</option>`).join('');
 if(r)routePicks=r.to_ids.slice();
 $('pickhint').textContent='🖱 кликайте по карте — точки маршрута';
 renderPicks();$('dlgRoute').showModal();
}
function togglePick(id){const i=routePicks.indexOf(id);if(i>=0)routePicks.splice(i,1);else routePicks.push(id);renderPicks();}
function renderPicks(){
 $('r_picks').innerHTML=OBJECTS.map(o=>`<div class="rt"><label style="margin:0"><input type="checkbox" style="width:auto"
  ${routePicks.includes(o.id)?'checked':''} onchange="togglePick(${o.id})">
  <span class="dot" style="background:${TYPECOLOR[o.type]};color:${TYPECOLOR[o.type]}"></span>${esc(o.name)}</label></div>`).join('');
}
async function saveRoute(){
 if(!routePicks.length){alert('Выберите хотя бы одну точку');return;}
 const rid=$('r_id').value||undefined;
 const res=await api('/api/route',{id:rid,from_id:+$('r_from').value,to_ids:routePicks,
  label:$('r_label').value,color:$('r_color').value,visible:1,auto_cargo:$('r_autocargo').checked?1:0});
 closeDlg('dlgRoute');await reload();
 if(res&&res.cargo_id)toast('Из маршрута создан груз #'+res.cargo_id);
}
function renderRoutes(){
 const box=$('rtlist');
 box.innerHTML=ROUTES.filter(visibleRoute).map(r=>{
  const f=OBJECTS.find(o=>o.id==r.from_id),ts=r.to_ids.map(id=>(OBJECTS.find(o=>o.id==id)||{}).name||'?');
  return `<div class="rt"><span class="dot" style="background:${r.color};color:${r.color}"></span>
   <span class="sp" style="cursor:pointer" title="Редактировать" onclick="openRoute(${r.id})">${esc(f?f.name:'?')} → ${ts.map(esc).join(', ')}${r.label?' · '+esc(r.label):''} · <b>${r.dist_km} км</b></span>
   <button class="mini" title="${r.visible?'Скрыть':'Показать'}" onclick="toggleRoute(${r.id},${1-r.visible})">${r.visible?'👁':'🙈'}</button>
   <button class="mini" title="Редактировать" onclick="openRoute(${r.id})">✏️</button>
   <button class="mini" title="Удалить" onclick="delRoute(${r.id})">✖</button></div>`;}).join('')||'<div class="hint">Нет маршрутов</div>';
 renderFilters();
}
async function toggleRoute(id,v){await api('/api/route',{id,visible:v});await reload();}
async function delRoute(id){if(!confirm('Удалить маршрут?'))return;await api('/api/delete',{kind:'route',id});await reload();}
async function delRouteBtn(){const id=+$('r_id').value;closeDlg('dlgRoute');await delRoute(id);}

// ---------- правая панель: платежи (емко) ----------
function payInfoLine(p){
 const c=CARGOS.find(x=>x.id==p.cargo_id);
 if(!c)return '<b>без груза</b>';
 return `#${c.id} ${esc(c.description)} · <span class="hint">${esc(c.from_name||'?')} → ${esc(c.to_name||'?')} · ${fmt(c.cost)} ₽</span>`;}
function renderPayList(){
 const cnt=PAYS.filter(p=>!p.paid_date).length;
 $('paycnt').textContent=cnt?`к оплате: ${cnt}`:'всё оплачено ✓';
 const box=$('paylist');box.innerHTML='';
 const sorted=PAYS.slice().sort((a,b)=>(a.due_date||'9999')<(b.due_date||'9999')?-1:1);
 for(const p of sorted){
  const n=daysTo(p.due_date);
  const cls=p.paid_date?'ok':(n!=null&&n<=1?'hot':'');
  box.insertAdjacentHTML('beforeend',`<div class="payrow${p.paid_date?' paidline':''}" onclick="openPay(${p.id})">
   <span class="days ${cls}">${p.paid_date?'✓ оплачен':dayLabel(n)}</span>
   <b>${fmt(p.amount)} ₽</b> · ${esc(p.kind)}<br>
   <span class="hint">${p.due_date||'без срока'} · ${payInfoLine(p)}</span></div>`);}
 if(!sorted.length)box.innerHTML='<div class="hint">Нет платежей. «＋ пакет» — предоплата/доплата/постоплата.</div>';
}

// ---------- правая панель: грузы (емко) ----------
function renderCaoList(){
 const box=$('carolist');box.innerHTML='';
 for(const c of CARGOS){
  const n=daysTo(c.ship_date);
  const cls=(c.status=='Доставлен'||c.status=='В пути')?'ok':(n!=null&&n<=1?'hot':'');
  box.insertAdjacentHTML('beforeend',`<div class="carow" onclick="openCargo(${c.id})">
   <span class="days ${cls}">${n==null?'':dayLabel(n)}</span>
   <span class="st ${stClass(c.status)}">${c.status}</span> <b>#${c.id}</b> ${esc(c.description)}<br>
   <span class="hint">${esc(c.from_name||'?')} → ${esc(c.to_name||'?')} · ${c.places} м · ${num(c.weight)} кг · ${fmt(c.cost)} ₽${c.route_color?` · <span class="dot" style="background:${c.route_color};color:${c.route_color}"></span>${esc(c.route_label||'марш.')}`:''}</span></div>`);}
 if(!CARGOS.length)box.innerHTML='<div class="hint">Нет грузов</div>';
}
function stClass(s){return {'Ожидает':'s0','Готов':'s1','В пути':'s2','Доставлен':'s3'}[s]||'s3';}

// ---------- карточка груза ----------
function objOptions(sel,cur){sel.innerHTML='<option value="">—</option>'+OBJECTS.map(o=>`<option value="${o.id}" ${o.id==cur?'selected':''}>${esc(o.name)} (${esc(o.city||'')})</option>`).join('');}
function routeOptions(sel,cur){sel.innerHTML='<option value="">— без маршрута —</option>'+ROUTES.map(r=>{
 const f=OBJECTS.find(o=>o.id==r.from_id);return `<option value="${r.id}" ${r.id==cur?'selected':''}>#${r.id} ${esc(f?f.name:'?')} → ${r.to_ids.map(id=>(OBJECTS.find(o=>o.id==id)||{}).name||'?').join(', ')}</option>`;}).join('');}
const CARGO_ST=['Ожидает','Готов','В пути','Доставлен'];
function openCargo(id){
 const c=CARGOS.find(x=>x.id==id)||{};
 $('cgTitle').textContent=id?'Груз #'+id:'Новый груз';$('cg_id').value=c.id||'';$('cg_del').classList.toggle('hidden',!id);
 $('cg_desc').value=c.description||'';objOptions($('cg_from'),c.from_id);objOptions($('cg_to'),c.to_id);
 routeOptions($('cg_route'),c.route_id);$('cg_mkroute').checked=false;
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
  cost:+$('cg_cost').value||0,note:$('cg_note').value,
  route_id:$('cg_route').value||null,include_route:$('cg_mkroute').checked?1:0};
 if(!body.description){alert('Заполните описание');return;}
 const res=await api('/api/cargo',body);closeDlg('dlgCargo');await reload();
 if(res&&res.route_id)toast('Создан маршрут #'+res.route_id);
}
async function delCargo(){if(!confirm('Удалить груз и его платежи?'))return;
 await api('/api/delete',{kind:'cargo',id:+$('cg_id').value});closeDlg('dlgCargo');await reload();}

// ---------- карточка платежа (груз — информативно) ----------
function cargoOptionText(c){return `#${c.id} ${esc(c.description)} · ${esc(c.from_name||'?')} → ${esc(c.to_name||'?')} · ${fmt(c.cost)} ₽`;}
function payCargoHint(){const c=CARGOS.find(x=>String(x.id)==String($('py_cargo').value));
 $('py_cargohint').innerHTML=c?`Откуда: <b>${esc(c.from_name||'—')}</b> → Куда: <b>${esc(c.to_name||'—')}</b> · стоимость ${fmt(c.cost)} ₽`:'—';}
function openPay(id){
 const p=PAYS.find(x=>x.id==id)||{};
 $('pyTitle').textContent=id?'Платёж #'+id:'Новый платёж';$('py_id').value=p.id||'';$('py_del').classList.toggle('hidden',!id);
 $('py_cargo').innerHTML='<option value="">— без груза —</option>'+CARGOS.map(c=>`<option value="${c.id}" ${c.id==p.cargo_id?'selected':''}>${cargoOptionText(c)}</option>`).join('');
 payCargoHint();
 fillSel($('py_kind'),['аванс','оплата','возврат'],p.kind||'оплата');
 $('py_amount').value=p.amount||'';$('py_due').value=p.due_date||'';$('py_paid').value=p.paid_date||'';
 $('py_cp').value=p.counterparty||'';$('py_inv').value=p.invoice||'';$('py_upd').value=p.upd||'';
 $('dlgPay').showModal();
}
async function savePay(){
 await api('/api/payment',{id:$('py_id').value||undefined,cargo_id:$('py_cargo').value||null,
  kind:$('py_kind').value,amount:+$('py_amount').value||0,due_date:$('py_due').value,
  paid_date:$('py_paid').value||null,counterparty:$('py_cp').value,invoice:$('py_inv').value,upd:$('py_upd').value});
 closeDlg('dlgPay');await reload();
}
async function delPay(){await api('/api/delete',{kind:'payment',id:+$('py_id').value});closeDlg('dlgPay');await reload();}

// ---------- пакет платежей ----------
function openBatch(cargoId){
 $('b_cargo').innerHTML=CARGOS.map(c=>`<option value="${c.id}" ${String(c.id)==String(cargoId)?'selected':''}>${cargoOptionText(c)}</option>`).join('');
 ['pre','mid','post'].forEach(s=>{$('b_'+s+'_sum').value='';$('b_'+s+'_pct').value='';$('b_'+s+'_date').value='';});
 $('b_err').textContent='';batchMode();$('dlgBatch').showModal();
}
function batchMode(){const pct=document.querySelector('input[name=bmode]:checked').value=='percent';
 ['pre','mid','post'].forEach(s=>{$('b_'+s+'_sum').disabled=pct;$('b_'+s+'_pct').disabled=!pct;});batchCalc();}
function batchCalc(){const c=CARGOS.find(x=>String(x.id)==String($('b_cargo').value));
 $('b_cargohint').innerHTML=c?`Откуда: <b>${esc(c.from_name||'—')}</b> → Куда: <b>${esc(c.to_name||'—')}</b> · стоимость ${fmt(c.cost)} ₽`:'—';
 $('b_total').textContent=c?('стоимость: '+fmt(c.cost)+' ₽'):'';
 const pct=document.querySelector('input[name=bmode]:checked').value=='percent';
 if(pct&&c){const rest=100-(+$('b_pre_pct').value||0)-(+$('b_mid_pct').value||0);
  $('b_post_pct').value=Math.max(rest,0);}
 ['pre','mid','post'].forEach(s=>{if(pct&&c)$('b_'+s+'_sum').value=Math.round((c.cost||0)*(+$('b_'+s+'_pct').value||0))/100;});
 let err='';
 if(pct&&c){const sum=(+$('b_pre_pct').value||0)+(+$('b_mid_pct').value||0)+(+$('b_post_pct').value||0);
  if(Math.abs(sum-100)>0.01)err='Σ процентов = '+sum+', нужно 100';}
 $('b_err').textContent=err;}
async function saveBatch(){
 const pct=document.querySelector('input[name=bmode]:checked').value=='percent';
 const body={cargo_id:$('b_cargo').value,mode:pct?'percent':'sums'};
 ['pre','mid','post'].forEach(s=>{body[s+(pct?'_pct':'_sum')]=$('b_'+s+(pct?'_pct':'_sum')).value;body[s+'_date']=$('b_'+s+'_date').value;});
 if(!body.cargo_id){alert('Выберите груз');return;}
 const r=await api('/api/payment_batch',body);
 if(r.error){$('b_err').textContent=r.error;return;}
 closeDlg('dlgBatch');await reload();toast('Добавлено платежей: '+(r.count||0));
}

// ---------- контакты ----------
async function loadCons(){CONS=await getJSON('/api/contacts');renderCons();}
function renderCons(){
 const q=($('qcon').value||'').toLowerCase();
 const tb=$('contab').querySelector('tbody');tb.innerHTML='';
 for(const ct of CONS){
  if(q&&!((ct.name+' '+(ct.obj_name||'')+' '+ct.phone).toLowerCase().includes(q)))continue;
  tb.insertAdjacentHTML('beforeend',`<tr><td><b>${esc(ct.name)}</b></td><td>${esc(ct.position)}</td>
   <td>${esc(ct.obj_name||'—')}</td><td>${esc(ct.phone)}</td><td>${esc(ct.email)}</td><td>${esc(ct.note)}</td>
   <td><button class="mini" onclick="openCon(${ct.id})">✏️</button></td></tr>`);}
 if(!CONS.length)tb.innerHTML='<tr><td colspan="7" class="hint">Пусто — добавьте контакт</td></tr>';
}
function openCon(id){
 const ct=CONS.find(x=>x.id==id)||{};
 $('cnTitle').textContent=id?'Контакт #'+id:'Новый контакт';$('cn_id').value=ct.id||'';$('cn_del').classList.toggle('hidden',!id);
 $('cn_name').value=ct.name||'';$('cn_pos').value=ct.position||'';
 $('cn_obj').innerHTML='<option value="">—</option>'+OBJECTS.map(o=>`<option value="${o.id}" ${o.id==ct.obj_id?'selected':''}>${esc(o.name)} (${esc(o.city||'')})</option>`).join('');
 $('cn_phone').value=ct.phone||'';$('cn_email').value=ct.email||'';$('cn_note').value=ct.note||'';
 $('dlgCon').showModal();
}
async function saveCon(){
 if(!$('cn_name').value.trim()){alert('Заполните имя');return;}
 await api('/api/contact',{id:$('cn_id').value||undefined,name:$('cn_name').value.trim(),position:$('cn_pos').value,obj_id:$('cn_obj').value||null,phone:$('cn_phone').value,email:$('cn_email').value,note:$('cn_note').value});
 closeDlg('dlgCon');await loadCons();}
async function delCon(){if(!confirm('Удалить контакт?'))return;await api('/api/delete',{kind:'contact',id:+$('cn_id').value});closeDlg('dlgCon');await loadCons();}

// ---------- перевозчики ----------
async function loadCars(){CARS=await getJSON('/api/carriers');renderCars();}
function renderCars(){
 const q=($('qcar').value||'').toLowerCase();
 const tb=$('cartab').querySelector('tbody');tb.innerHTML='';
 for(const cr of CARS){
  if(q&&!((cr.name+cr.phone+cr.modes).toLowerCase().includes(q)))continue;
  tb.insertAdjacentHTML('beforeend',`<tr><td><b>${esc(cr.name)}</b></td><td>${esc(cr.phone)}</td><td>${esc(cr.email)}</td>
   <td>${esc(cr.site)}</td><td>${esc(cr.modes)}</td><td>${esc(cr.note)}</td>
   <td><button class="mini" onclick="openCar(${cr.id})">✏️</button></td></tr>`);}
 if(!CARS.length)tb.innerHTML='<tr><td colspan="7" class="hint">Пусто — добавьте перевозчика</td></tr>';
}
function openCar(id){
 const cr=CARS.find(x=>x.id==id)||{};
 $('crTitle').textContent=id?'Перевозчик #'+id:'Новый перевозчик';$('cr_id').value=cr.id||'';$('cr_del').classList.toggle('hidden',!id);
 $('cr_name').value=cr.name||'';$('cr_phone').value=cr.phone||'';$('cr_email').value=cr.email||'';
 $('cr_site').value=cr.site||'';$('cr_modes').value=cr.modes||'';$('cr_note').value=cr.note||'';
 $('dlgCar').showModal();
}
async function saveCar(){
 if(!$('cr_name').value.trim()){alert('Заполните название');return;}
 await api('/api/carrier',{id:$('cr_id').value||undefined,name:$('cr_name').value.trim(),phone:$('cr_phone').value,email:$('cr_email').value,site:$('cr_site').value,modes:$('cr_modes').value,note:$('cr_note').value});
 closeDlg('dlgCar');await loadCars();}
async function delCar(){if(!confirm('Удалить перевозчика?'))return;await api('/api/delete',{kind:'carrier',id:+$('cr_id').value});closeDlg('dlgCar');await loadCars();}

// ---------- уведомления ----------
let EVENTS=[];
async function loadEvents(){
 if(SET.notify=='0'){$('evcnt').textContent='';return;}
 const r=await getJSON('/api/events');EVENTS=r.events||[];
 $('evcnt').textContent=EVENTS.length?` ${EVENTS.length}`:'';
 if(EVENTS.length&&!sessionStorage.getItem('notif_shown')){showEvents();sessionStorage.setItem('notif_shown','1');}
}
function showEvents(){
 const box=$('evlist');
 box.innerHTML=EVENTS.length?EVENTS.map(e=>`<div class="ev ${e.kind}">${esc(e.text)}</div>`).join(''):
  '<div class="hint">Событий нет — всё спокойно ✓</div>';
 $('notifbox').classList.remove('hidden');
}

// ---------- экспорт календаря ----------
async function exportCalCSV(){
 const cal=await getJSON('/api/calendar');
 const names={1:'янв',2:'фев',3:'мар',4:'апр',5:'май',6:'июн',7:'июл',8:'авг',9:'сен',10:'окт',11:'ноя',12:'дек'};
 let csv='\uFEFFМесяц;Ожидает;Просрочено;Оплачено\n';
 for(const m of Object.keys(cal.months).sort()){const b=cal.months[m];
  const lbl=m.length==7?`${names[+m.slice(5)]} ${m.slice(0,4)}`:m;
  csv+=`${lbl};${Math.round(b.expect)};${Math.round(b.overdue)};${Math.round(b.paid)}\n`;}
 dl(csv,'calendar.csv');toast('Календарь выгружен');
}
function dl(text,name){const a=document.createElement('a');
 a.href=URL.createObjectURL(new Blob([text],{type:'text/csv;charset=utf-8'}));a.download=name;a.click();}
function toast(msg){$('hint').textContent=msg;setTimeout(()=>$('hint').textContent='',3500);}

// ---------- настройки ----------
async function loadSet(){SET=await getJSON('/api/settings');
 $('s_lines').checked=SET.show_lines=='1';$('s_dist').checked=SET.show_distances=='1';
 $('s_notify').checked=SET.notify!='0';
 $('s_style').value=SET.line_style||'straight';$('s_font').value=SET.font_size||'2';
 document.body.className='fs'+(SET.font_size||'2');
 FILTER.types={supplier:true,client:true,hub:true};
 if(SET.filter_types){const on=SET.filter_types.split(',');
  for(const k of Object.keys(FILTER.types))FILTER.types[k]=on.includes(k);}
 FILTER.colors=SET.filter_colors||null;
 applyTheme();}
async function saveSet(){
 const body={show_lines:$('s_lines').checked?1:0,show_distances:$('s_dist').checked?1:0,
  notify:$('s_notify').checked?1:0,line_style:$('s_style').value,font_size:$('s_font').value};
 await api('/api/settings',body);SET={...SET,...body};
 document.body.className='fs'+(SET.font_size||'2');drawMap();loadEvents();
}

// старт
(async()=>{await loadSet();await reload();await loadEvents();})();
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
            if path == "/api/contacts":
                return self._send(200, json.dumps(api_contacts(con), ensure_ascii=False))
            if path == "/api/carriers":
                return self._send(200, json.dumps(api_carriers(con), ensure_ascii=False))
            if path == "/api/events":
                return self._send(200, json.dumps(api_events(con), ensure_ascii=False))
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
                elif path == "/api/payment_batch":
                    res = api_save_pay_batch(con, body)
                elif path == "/api/contact":
                    res = api_save_contact(con, body)
                elif path == "/api/carrier":
                    res = api_save_carrier(con, body)
                elif path == "/api/settings":
                    for k in ("show_lines", "show_distances", "line_style", "font_size",
                              "theme", "notify", "filter_types", "filter_colors"):
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
                    table = {
                        "object": "objects", "route": "routes",
                        "cargo": "cargos", "payment": "payments",
                        "contact": "contacts", "carrier": "carriers"}.get(body.get("kind"))
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
    print(f"КАБТЕХ v3: http://localhost:{args.port}  (база: {DB_PATH}, остановка — Ctrl+C)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
