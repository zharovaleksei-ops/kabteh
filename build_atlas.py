#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Логистический атлас КАБТЕХ
==========================
Читает data.csv (поставщики/заказчики) и строит atlas.html — интерактивную
карту в стиле «самолётных линий»: узлы (склады/города) + маршруты через хаб.

Координаты городов заданы словарём (без обращения к геокодеру), поэтому
скрипт работает офлайн. Маршруты: каждый поставщик/заказчик соединяется
с ближайшим хабом КАБТЕХ (Алфимово / Апрелевка / Климовск, Москва).

Запуск:  python build_atlas.py          -> создаёт ./atlas.html
Опция:   python build_atlas.py --embed  -> встроить Leaflet+OSM-тайлы прямо
           в HTML (нужен интернет на момент запуска, файл становится ~2 МБ
           и полностью автономным).
"""

import csv
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).parent
DATA_CSV = BASE / "data.csv"
OUT_HTML = BASE / "atlas.html"

# ----------------------------- координаты -----------------------------------
COORDS = {
    "Саранск":            (54.1808, 45.1772),
    "Балахна":            (56.2528, 43.5606),
    "Казань":             (55.7963, 49.1088),
    "Ярославль":          (57.6261, 39.8845),
    "Самара":             (53.2000, 50.1500),
    "Домодедово":         (55.4386, 37.7508),
    "Котельники":         (55.6333, 37.8764),
    "Санкт-Петербург":    (59.9389, 30.3157),
    "Набережные Челны":   (55.7438, 52.3959),
    "Дзержинск":          (56.2413, 43.4591),
    "Алфимово (Ступинский р-н)": (55.0500, 38.1300),  # Ступинский р-н,approx
    "Апрелевка":          (55.4266, 36.9644),
    "Климовск":           (55.4288, 37.5644),
    "Балашиха":           (55.7964, 37.9372),
    "Тула":               (54.1961, 37.6182),
    "пос. Воровского (МО)": (55.8600, 38.3700),        # Ногинский р-н, approx
    "Подольск":           (55.4258, 37.5103),
    "Москва":             (55.7522, 37.6156),
    "Курск":              (51.7304, 36.2085),
    "Белорецк":           (53.9692, 58.4050),
    "Южноуральск":        (54.4615, 61.9643),
    "Волгоград":          (48.7080, 44.5133),
    "пос. Спутник (Самарская обл.)": (53.2700, 50.4200),  # под Самарой, approx
}

HUB_NAMES = {"КАБТЕХ (Алфимово)", "КАБТЕХ (Апрелевка)", "КАБТЕХ (Климовск)"}

TYPE_STYLE = {
    "Поставщик": {"color": "#e63946", "label": "Поставщик"},
    "Заказчик":  {"color": "#2a9d8f", "label": "Заказчик"},
    "Хаб":       {"color": "#1d3557", "label": "Хаб КАБТЕХ"},
}


def haversine(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    dla, dlo = lat2 - lat1, lon2 - lon1
    h = math.sin(dla/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlo/2)**2
    return 6371 * 2 * math.asin(math.sqrt(h))


def geocode(row):
    """Привязка точки к координатам по колонке «Город»."""
    city = row["Город"].strip()
    if city in COORDS:
        return COORDS[city], city
    # нечёткий матч: город может быть указан с уточнениями
    for k, v in COORDS.items():
        if k.split(" (")[0].lower() in city.lower() or city.lower() in k.lower():
            return v, k
    return None, city


def load_rows():
    with open(DATA_CSV, encoding="utf-8") as f:
        rd = csv.DictReader(f)
        rows = [{k.strip("№"): (v or "").strip() for k, v in r.items()} for r in rd]
    # нормализуем имя ключа №
    out = []
    for r in rows:
        fixed = {}
        for k, v in r.items():
            fixed["num" if k.startswith("№") else k] = v
        out.append(fixed)
    return out


def great_circle_arc(p1, p2, segments=40):
    """Дуга большого круга между двумя точками [lat, lon] — эффект «самолётной линии»."""
    lat1, lon1 = map(math.radians, p1)
    lat2, lon2 = map(math.radians, p2)
    d = 2 * math.asin(math.sqrt(
        math.sin((lat2-lat1)/2)**2 +
        math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2))
    if d == 0:
        return [p1, p2]
    pts = []
    for i in range(segments + 1):
        f = i / segments
        a = math.sin((1-f)*d) / math.sin(d)
        b = math.sin(f*d) / math.sin(d)
        x = a*math.cos(lat1)*math.cos(lon1) + b*math.cos(lat2)*math.cos(lon2)
        y = a*math.cos(lat1)*math.sin(lon1) + b*math.cos(lat2)*math.sin(lon2)
        z = a*math.sin(lat1) + b*math.sin(lat2)
        lat = math.atan2(z, math.sqrt(x*x + y*y))
        lon = math.atan2(y, x)
        pts.append([round(math.degrees(lat), 5), round(math.degrees(lon), 5)])
    return pts


def build_model(rows):
    nodes = {}     # id -> node dict
    routes = []    # {from,to,dist_km,arc}
    hubs = []

    for r in rows:
        coords, matched_city = geocode(r)
        is_hub = r["Название (поиск)"] in HUB_NAMES or "КАБТЕХ" in r["Название (поиск)"]
        typ = "Хаб" if is_hub else r["Тип"]
        nid = f'{r["Название (поиск)"]}#{r["Город"]}'
        node = {
            "id": nid,
            "name": r["Название"],
            "search": r["Название (поиск)"],
            "type": typ,
            "city": r["Город"],
            "matched_city": matched_city,
            "inn": r["ИНН"], "kpp": r["КПП"],
            "legal_addr": r["Юр. адрес"], "warehouse_addr": r["Адрес склада"],
            "access": r["Заезд"], "hours": r["Режим работы"],
            "contact": r["Контактное лицо"], "phone": r["Телефон"],
            "email": r["Email"], "notes": r["Примечания"],
            "coords": list(coords) if coords else None,
        }
        nodes[nid] = node
        if is_hub and coords:
            hubs.append(node)

    # маршруты: не-хаб -> ближайший хаб
    unconnected = []
    for nid, n in nodes.items():
        if n["type"] == "Хаб" or not n["coords"]:
            if n["type"] != "Хаб" and not n["coords"]:
                unconnected.append(nid)
            continue
        best = min(hubs, key=lambda h: haversine(n["coords"], h["coords"]))
        dist = haversine(n["coords"], best["coords"])
        routes.append({
            "from": nid, "to": best["id"],
            "dist_km": round(dist),
            "arc": great_circle_arc(n["coords"], best["coords"]),
        })

    return {"nodes": list(nodes.values()), "routes": routes,
            "hubs": [h["id"] for h in hubs], "unconnected": unconnected}


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Логистический атлас — КАБТЕХ</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
  :root { --bg:#0f1420; --panel:#161d2e; --line:#26314a; --txt:#e8edf6; --dim:#8fa0bf; }
  * { box-sizing: border-box; }
  html,body { margin:0; height:100%; font-family:'Segoe UI',system-ui,Arial,sans-serif; background:var(--bg); color:var(--txt); }
  #app { display:flex; height:100%; }
  #map { flex:1; }
  #side { width:390px; background:var(--panel); border-left:1px solid var(--line); display:flex; flex-direction:column; }
  .head { padding:14px 16px 10px; border-bottom:1px solid var(--line); }
  .head h1 { margin:0 0 2px; font-size:18px; letter-spacing:.5px; }
  .head .sub { color:var(--dim); font-size:12px; }
  .tools { padding:10px 16px; border-bottom:1px solid var(--line); display:flex; gap:8px; flex-wrap:wrap; }
  .tools input[type=search] { flex:1; min-width:150px; background:#0d1322; color:var(--txt); border:1px solid var(--line); border-radius:8px; padding:8px 10px; font-size:13px; outline:none; }
  .chip { cursor:pointer; user-select:none; font-size:12px; padding:6px 10px; border-radius:14px; border:1px solid var(--line); color:var(--dim); background:#0d1322; }
  .chip.on { color:#fff; border-color:transparent; }
  .legend-dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:5px; vertical-align:1px; }
  #list { overflow-y:auto; flex:1; }
  .item { padding:10px 16px; border-bottom:1px solid var(--line); cursor:pointer; }
  .item:hover, .item.sel { background:#1d2740; }
  .item .t { font-weight:600; font-size:13.5px; }
  .item .m { color:var(--dim); font-size:11.5px; margin-top:2px; }
  .badge { font-size:10px; padding:2px 7px; border-radius:9px; margin-right:6px; color:#fff; vertical-align:1px; }
  #detail { position:absolute; top:12px; left:12px; z-index:1000; width:340px; max-height:calc(100% - 24px); overflow:auto;
            background:rgba(15,20,32,.96); border:1px solid var(--line); border-radius:12px; padding:14px 16px; display:none; backdrop-filter: blur(4px);}
  #detail h3 { margin:0 0 2px; font-size:15px; }
  #detail .row { margin-top:8px; font-size:12px; }
  #detail .k { color:var(--dim); text-transform:uppercase; font-size:10px; letter-spacing:.6px; }
  #detail .v { margin-top:1px; }
  #detail .close { position:absolute; top:8px; right:10px; cursor:pointer; color:var(--dim); font-size:18px; line-height:1; }
  #detail a { color:#6ab0ff; text-decoration:none; }
  .stats { padding:8px 16px; border-top:1px solid var(--line); font-size:11.5px; color:var(--dim); }
  .route-label { background:transparent; border:none; box-shadow:none; color:#cfe0ff; font-size:10.5px; font-weight:600; text-shadow:0 0 4px #000, 0 0 4px #000; }
  .leaflet-container { background:#0b1020; }
  @media (max-width: 860px){ #app{flex-direction:column-reverse} #side{width:100%;height:45%} }
</style>
</head>
<body>
<div id="app">
  <div id="map"></div>
  <div id="side">
    <div class="head">
      <h1>ЛОГИСТИЧЕСКИЙ АТЛАС · КАБТЕХ</h1>
      <div class="sub">Маршрутные линии через хабы сети. Клик по точке или списку — карточка объекта.</div>
    </div>
    <div class="tools">
      <input id="q" type="search" placeholder="Поиск: название / город / ИНН…">
      <span class="chip on" data-t="Хаб" style="background:#1d3557"><span class="legend-dot" style="background:#fff"></span>Хабы</span>
      <span class="chip on" data-t="Поставщик" style="background:#e63946"><span class="legend-dot" style="background:#fff"></span>Поставщики</span>
      <span class="chip on" data-t="Заказчик" style="background:#2a9d8f"><span class="legend-dot" style="background:#fff"></span>Заказчики</span>
    </div>
    <div id="list"></div>
    <div class="stats" id="stats"></div>
  </div>
</div>
<div id="detail"><span class="close" onclick="hideDetail()">×</span><div id="dbody"></div></div>

<script type="application/json" id="model">__MODEL_JSON__</script>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const MODEL = JSON.parse(document.getElementById('model').textContent);
const STYLE = {"Хаб":{c:"#1d3557",l:"Хаб"},"Поставщик":{c:"#e63946",l:"Поставщик"},"Заказчик":{c:"#2a9d8f",l:"Заказчик"}};
const byId = Object.fromEntries(MODEL.nodes.map(n=>[n.id,n]));

const map = L.map('map', {zoomControl:false}).setView([55.5,44.0], 5);
L.control.zoom({position:'bottomright'}).addTo(map);
// тёмные тайлы CARTO (подложка «ночная карта»)
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution:'&copy; OpenStreetMap &copy; CARTO', subdomains:'abcd', maxZoom:19
}).addTo(map);

const layerNodes = L.layerGroup().addTo(map);
const layerRoutes = L.layerGroup().addTo(map);
const markers = {}; // id -> marker
let activeTypes = new Set(["Хаб","Поставщик","Заказчик"]);
let query = "";
let selected = null;

function esc(s){return (s||"").replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

function drawRoutes(){
  layerRoutes.clearLayers();
  for(const r of MODEL.routes){
    const a=byId[r.from], b=byId[r.to];
    if(!a.coords||!b.coords) continue;
    if(!(activeTypes.has(a.type)&&activeTypes.has(b.type))) continue;
    if(!visible(a)||!visible(b)) continue;
    const hot = selected && (r.from===selected||r.to===selected);
    L.polyline(r.arc,{color:hot?"#ffd166":"#7aa7ff",weight:hot?3.2:1.6,opacity:hot?0.95:0.55,dashArray:null})
      .bindTooltip(`${esc(a.search)} → ${esc(b.search)} · ${r.dist_km} км`,
                   {permanent:false,className:'route-label',direction:'center'})
      .addTo(layerRoutes);
    const mid=r.arc[Math.floor(r.arc.length/2)];
    L.marker(mid,{icon:L.divIcon({className:'route-label',html:`<div>${r.dist_km} км</div>`,iconSize:[60,14]}),interactive:false}).addTo(layerRoutes);
  }
}

function visible(n){
  if(!activeTypes.has(n.type)) return false;
  if(!query) return true;
  const hay=(n.search+' '+n.name+' '+n.city+' '+n.inn+' '+(n.warehouse_addr||'')+' '+(n.notes||'')).toLowerCase();
  return hay.includes(query);
}

function nodeIcon(n){
  const st=STYLE[n.type]||STYLE["Поставщик"];
  const size=n.type==="Хаб"?18:11;
  const sel=selected===n.id;
  return L.divIcon({className:'',iconSize:[size,size],iconAnchor:[size/2,size/2],
    html:`<div style="width:${size}px;height:${size}px;border-radius:50%;background:${st.c};
      border:${sel?'3px solid #ffd166':'2px solid rgba(255,255,255,.85)'};
      box-shadow:0 0 ${sel?14:6}px ${st.c};"></div>`});
}

function drawNodes(){
  layerNodes.clearLayers();
  for(const n of MODEL.nodes){
    if(!n.coords) continue;
    if(!visible(n)) continue;
    const m=L.marker(n.coords,{icon:nodeIcon(n)}).addTo(layerNodes);
    m.bindTooltip(`<b>${esc(n.search)}</b><br>${esc(n.city)}`,{direction:'top',offset:[0,-6]});
    m.on('click',()=>select(n.id));
    markers[n.id]=m;
  }
}

function drawList(){
  const el=document.getElementById('list');
  const items=MODEL.nodes.filter(visible).sort((a,b)=>a.type===b.type?a.search.localeCompare(b.search,'ru'):(a.type==="Хаб"?-1:b.type==="Хаб"?1:a.type.localeCompare(b.type,'ru')));
  el.innerHTML=items.map(n=>`<div class="item${selected===n.id?' sel':''}" data-id="${encodeURIComponent(n.id)}">
      <div class="t"><span class="badge" style="background:${STYLE[n.type].c}">${STYLE[n.type].l}</span>${esc(n.search)}</div>
      <div class="m">${esc(n.city)}${n.warehouse_addr?' · '+esc(n.warehouse_addr):''}</div></div>`).join('')
      || '<div class="item"><div class="m">Ничего не найдено</div></div>';
  el.querySelectorAll('.item[data-id]').forEach(d=>d.onclick=()=>{
      const id=decodeURIComponent(d.dataset.id); select(id);
      const n=byId[id]; if(n.coords) map.flyTo(n.coords, 8, {duration:.8});
  });
}

function showDetail(n){
  const linked=MODEL.routes.filter(r=>r.from===n.id||r.to===n.id)
      .map(r=>{const o=byId[r.from===n.id?r.to:r.from];return `${esc(o.search)} (${r.dist_km} км)`;});
  const row=(k,v)=>v?`<div class="row"><div class="k">${k}</div><div class="v">${v}</div></div>`:'';
  document.getElementById('dbody').innerHTML=`
    <h3>${esc(n.name)}</h3><div class="m" style="color:var(--dim);font-size:12px">${STYLE[n.type].l} · ${esc(n.city)}</div>
    ${row('Адрес склада',esc(n.warehouse_addr))}
    ${row('Юридический адрес',esc(n.legal_addr))}
    ${row('Заезд на склад',esc(n.access))}
    ${row('Режим работы',esc(n.hours))}
    ${row('Контактное лицо',esc(n.contact))}
    ${row('Телефон',esc(n.phone))}
    ${row('Email',n.email?`<a href="mailto:${esc(n.email)}">${esc(n.email)}</a>`:'')}
    ${row('ИНН / КПП',[n.inn,n.kpp].filter(Boolean).map(esc).join(' / '))}
    ${row('Примечания',esc(n.notes))}
    ${row('Маршруты',linked.length?linked.join('<br>'):'—')}`;
  document.getElementById('detail').style.display='block';
}
function hideDetail(){document.getElementById('detail').style.display='none';selected=null;refresh();}

function select(id){selected=(selected===id)?null:id;refresh();if(selected&&byId[selected])showDetail(byId[selected]);else hideDetail();}

function refresh(){drawRoutes();drawNodes();drawList();updateStats();}
function updateStats(){
  const vn=MODEL.nodes.filter(n=>visible(n)&&n.coords).length;
  const vr=MODEL.routes.filter(r=>{const a=byId[r.from],b=byId[r.to];return visible(a)&&visible(b);}).length;
  const km=MODEL.routes.reduce((s,r)=>{const a=byId[r.from],b=byId[r.to];return visible(a)&&visible(b)?s+r.dist_km:s;},0);
  document.getElementById('stats').textContent=`Узлов: ${vn} · Линий: ${vr} · Суммарная длина сети: ${km.toLocaleString('ru-RU')} км`;
}

document.getElementById('q').addEventListener('input',e=>{query=e.target.value.trim().toLowerCase();refresh();});
document.querySelectorAll('.chip').forEach(ch=>ch.addEventListener('click',()=>{
  const t=ch.dataset.t;
  if(activeTypes.has(t)){activeTypes.delete(t);ch.classList.remove('on');ch.style.background='#0d1322';}
  else{activeTypes.add(t);ch.classList.add('on');ch.style.background=STYLE[t].c;}
  refresh();
}));

// авто-инициализация чипов цветом
document.querySelectorAll('.chip').forEach(ch=>ch.style.background=STYLE[ch.dataset.t].c);
refresh();
if(MODEL.unconnected.length){
  console.warn('Нет координат для:',MODEL.unconnected);
}
</script>
</body>
</html>
"""


def main():
    embed = "--embed" in sys.argv
    rows = load_rows()
    model = build_model(rows)
    js = json.dumps(model, ensure_ascii=False).replace("</", "<\\/")
    html = HTML_TEMPLATE.replace("__MODEL_JSON__", js)
    OUT_HTML.write_text(html, encoding="utf-8")

    n_nodes = len(model["nodes"])
    n_routes = len(model["routes"])
    total_km = sum(r["dist_km"] for r in model["routes"])
    print(f"OK: узлов {n_nodes}, линий {n_routes}, суммарно {total_km:,} км".replace(",", " "))
    if model["unconnected"]:
        print("Без координат:", model["unconnected"])
    print(f"Файл: {OUT_HTML}")
    if embed:
        try:
            import urllib.request
            css = urllib.request.urlopen("https://unpkg.com/leaflet@1.9.4/dist/leaflet.css", timeout=30).read().decode()
            js_lib = urllib.request.urlopen("https://unpkg.com/leaflet@1.9.4/dist/leaflet.js", timeout=30).read().decode()
            html = html.replace('<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">', f"<style>{css}</style>")
            html = html.replace('<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>', f"<script>{js_lib}</script>")
            OUT_HTML.write_text(html, encoding="utf-8")
            print("Leaflet встроен — atlas.html работает без CDN (тайлы карты всё равно требуют интернет).")
        except Exception as e:
            print("Не удалось встроить Leaflet:", e)


if __name__ == "__main__":
    main()
