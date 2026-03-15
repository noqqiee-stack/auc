#!/usr/bin/env python3
import asyncio, json, os, statistics, logging
from typing import Optional, List
from datetime import datetime, timezone
import aiohttp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

CLIENT_ID     = os.getenv("CLIENT_ID", "1434")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "0dMttjkdBsyVyRSRInJpKYWahGnSxBwgFehkuTCb")
REGION        = os.getenv("REGION", "ru")
API_BASE      = f"https://eapi.stalcraft.net/{REGION}"
TOKEN_URL     = "https://exbo.net/oauth/token"
GITHUB_TREE   = f"https://api.github.com/repos/EXBO-Studio/stalcraft-database/git/trees/main?recursive=1"
GITHUB_RAW    = "https://raw.githubusercontent.com/EXBO-Studio/stalcraft-database/main"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="STALCRAFT API", version="6.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Items DB ──────────────────────────────────────────────────
# id -> {name, category, cat_path, quality_key, rank_key, icon_path, item_path, _loaded}
ITEMS_DB: dict = {}
_token: str = ""

# ── Quality / Rank key → index mapping ───────────────────────
QUALITY_KEYS = {
    "core.quality.common":     0,
    "core.quality.uncommon":   1,
    "core.quality.special":    2,
    "core.quality.rare":       3,
    "core.quality.epic":       4,
    "core.quality.legendary":  5,
}
RANK_KEYS = {
    "core.rank.picklock": 0,
    "core.rank.stalker":  1,
    "core.rank.veteran":  2,
    "core.rank.master":   3,
    "core.rank.legend":   4,
}

# Numeric quality from API lots → display info
ART_QUALITY_NAMES = {
    0:"Обычный", 1:"Необычный", 2:"Особый",
    3:"Редкий",  4:"Исключительный", 5:"Легендарный"
}
ART_QUALITY_COLORS = {
    0:"#9ca3af", 1:"#4ade80", 2:"#60a5fa",
    3:"#c084fc", 4:"#f97474", 5:"#fbbf24"
}
RANK_NAMES  = {0:"Отмычка", 1:"Сталкер", 2:"Ветеран", 3:"Мастер", 4:"Легенда"}
RANK_COLORS = {0:"#9ca3af", 1:"#60a5fa", 2:"#f472b6", 3:"#f97474", 4:"#fbbf24"}

# For rarity filter param
RARITY_MAP = {
    "white": [0], "green": [1], "blue": [2],
    "purple": [3], "red": [4], "yellow": [5]
}

# ── Category → main category mapping ─────────────────────────
def main_cat(cat_path: str) -> str:
    return cat_path.split("/")[0] if "/" in cat_path else cat_path

# ── Token ─────────────────────────────────────────────────────
async def get_token() -> str:
    global _token
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(TOKEN_URL, data={
                "grant_type":"client_credentials",
                "client_id":CLIENT_ID,
                "client_secret":CLIENT_SECRET,
                "scope":""
            }) as r:
                if r.status == 200:
                    d = await r.json()
                    _token = d.get("access_token","")
                    log.info("Токен получен")
                else:
                    log.error(f"Ошибка токена: {r.status}")
    except Exception as e:
        log.error(f"Token error: {e}")
    return _token

def sc_hdrs(): return {"Authorization": f"Bearer {_token}"}

# ── Load items tree ───────────────────────────────────────────
async def load_items():
    global ITEMS_DB
    if ITEMS_DB: return
    log.info("Загружаю дерево предметов с GitHub...")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                GITHUB_TREE,
                headers={"Accept": "application/vnd.github+json"},
                timeout=aiohttp.ClientTimeout(total=30)
            ) as r:
                if r.status != 200:
                    log.error(f"GitHub tree: {r.status}"); return
                data = await r.json()

        tree = data.get("tree", [])
        count = 0
        for node in tree:
            path = node.get("path","")
            # Items: ru/items/{category}/{optional_subcat}/{id}.json
            if not path.startswith(f"{REGION}/items/"): continue
            if not path.endswith(".json"): continue
            parts = path.split("/")
            if len(parts) < 4: continue
            item_id  = parts[-1].replace(".json","")
            cat_path = "/".join(parts[2:-1])   # e.g. "artefact/biochemical"
            cat_main = parts[2]                 # e.g. "artefact"
            icon_path = f"{REGION}/icons/{cat_path}/{item_id}.png"
            ITEMS_DB[item_id] = {
                "name":       item_id,
                "category":   cat_main,
                "cat_path":   cat_path,
                "quality_idx": -1,   # -1 = unknown
                "rank_idx":    -1,
                "icon_path":  icon_path,
                "item_path":  path,
                "_loaded":    False,
            }
            count += 1
        log.info(f"Найдено {count} предметов в дереве")
        # Load names+quality for first 500 items
        await batch_load(list(ITEMS_DB.keys())[:500])
    except Exception as e:
        log.error(f"load_items error: {e}")

async def load_one(session, item_id):
    info = ITEMS_DB.get(item_id)
    if not info or info.get("_loaded"): return
    try:
        url = f"{GITHUB_RAW}/{info['item_path']}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200: return
            d = await r.json(content_type=None)
            # Name
            name_obj = d.get("name",{})
            if isinstance(name_obj, dict):
                lines = name_obj.get("lines",{})
                name = lines.get("ru", lines.get("en", item_id))
            else:
                name = str(name_obj) if name_obj else item_id
            # Extract quality / rank from infoBlocks
            quality_idx = -1
            rank_idx    = -1
            for block in d.get("infoBlocks",[]):
                for elem in block.get("elements",[]):
                    k = elem.get("key",{})
                    kkey = k.get("key","") if isinstance(k,dict) else ""
                    v = elem.get("value",{})
                    vkey = v.get("key","") if isinstance(v,dict) else ""
                    # Quality from key (artifact)
                    if kkey in QUALITY_KEYS:
                        quality_idx = QUALITY_KEYS[kkey]
                    # Rank from value (item rank displayed as value)
                    if vkey in RANK_KEYS:
                        rank_idx = RANK_KEYS[vkey]
                    # Also check if key itself is rank
                    if kkey in RANK_KEYS:
                        rank_idx = RANK_KEYS[kkey]
            ITEMS_DB[item_id].update({
                "name":        name,
                "quality_idx": quality_idx,
                "rank_idx":    rank_idx,
                "_loaded":     True,
            })
    except Exception:
        pass

async def batch_load(ids: List[str]):
    if not ids: return
    async with aiohttp.ClientSession() as s:
        # Load in chunks of 50 to avoid overwhelming
        for i in range(0, len(ids), 50):
            chunk = ids[i:i+50]
            await asyncio.gather(*[load_one(s, iid) for iid in chunk], return_exceptions=True)
    loaded = sum(1 for v in ITEMS_DB.values() if v.get("_loaded"))
    log.info(f"Загружено имён: {loaded}")

@app.on_event("startup")
async def startup():
    await get_token()
    await load_items()

# ── Search ────────────────────────────────────────────────────
def search_ids(q: str = "", category: str = "") -> List[str]:
    if not ITEMS_DB: return []
    ql = q.lower().strip()
    result = []
    for iid, d in ITEMS_DB.items():
        if category and d.get("category") != category: continue
        if ql:
            name = d.get("name", iid).lower()
            if not (ql in name or ql in iid.lower() or
                    any(w in name for w in ql.split())):
                continue
        result.append(iid)
    return result

def fmt_time(end_str: str) -> str:
    try:
        end = datetime.fromisoformat(end_str.replace("Z","+00:00"))
        s = int((end - datetime.now(timezone.utc)).total_seconds())
        if s <= 0: return "Истёк"
        d,r=divmod(s,86400); h,r=divmod(r,3600); m,_=divmod(r,60)
        if d: return f"{d}д {h}ч"
        if h: return f"{h}ч {m}м"
        return f"{m}м"
    except: return "—"

def is_artefact(cat: str) -> bool:
    return cat == "artefact"

def enrich(lot: dict, iid: str) -> dict:
    lot["_id"]       = iid
    info             = ITEMS_DB.get(iid, {})
    lot["_name"]     = info.get("name", iid)
    lot["_category"] = info.get("category","misc")
    lot["_icon"]     = info.get("icon_path","")
    lot["_timeLeft"] = fmt_time(lot.get("endTime",""))
    add = lot.get("additional",{})
    quality = add.get("quality", 0)
    lot["_quality"]  = quality
    lot["_enh"]      = add.get("potentialLevel",0)
    lot["_studied"]  = add.get("isResearched", add.get("researched", None))
    # Color and name based on category
    art = is_artefact(info.get("category",""))
    if art:
        lot["_color_hex"] = ART_QUALITY_COLORS.get(quality, "#9ca3af")
        lot["_rarity_name"] = ART_QUALITY_NAMES.get(quality, "—")
    else:
        # For non-artifacts, quality maps to rank
        lot["_color_hex"] = RANK_COLORS.get(quality, "#9ca3af")
        lot["_rarity_name"] = RANK_NAMES.get(quality, "—")
    amt = lot.get("amount",1) or 1
    price = lot.get("buyoutPrice") or lot.get("startPrice") or 0
    lot["_perUnit"] = price // amt if amt > 1 and price else None
    return lot

# ── Routes ────────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    await load_items()
    return {
        "status": "ok",
        "items":  len(ITEMS_DB),
        "loaded": sum(1 for v in ITEMS_DB.values() if v.get("_loaded")),
        "token":  bool(_token),
    }

@app.get("/api/items/search")
async def items_search(q: str="", category: str="", limit: int=40):
    await load_items()
    ids = search_ids(q, category)
    # Load unloaded items on demand
    unloaded = [i for i in ids[:limit] if not ITEMS_DB.get(i,{}).get("_loaded")]
    if unloaded:
        await batch_load(unloaded)
    result = []
    for iid in ids[:limit]:
        if iid not in ITEMS_DB: continue
        d = ITEMS_DB[iid]
        # Determine color for display
        art = is_artefact(d.get("category",""))
        qi  = d.get("quality_idx", -1)
        ri  = d.get("rank_idx", -1)
        if art and qi >= 0:
            color_hex  = ART_QUALITY_COLORS.get(qi, "#9ca3af")
            rarity_name = ART_QUALITY_NAMES.get(qi, "")
        elif not art and ri >= 0:
            color_hex  = RANK_COLORS.get(ri, "#9ca3af")
            rarity_name = RANK_NAMES.get(ri, "")
        else:
            color_hex  = "#9ca3af"
            rarity_name = ""
        result.append({
            "id":          iid,
            "name":        d["name"],
            "category":    d["category"],
            "cat_path":    d.get("cat_path",""),
            "icon_path":   d.get("icon_path",""),
            "quality_idx": qi,
            "rank_idx":    ri,
            "color_hex":   color_hex,
            "rarity_name": rarity_name,
            "is_artefact": art,
        })
    return result

@app.get("/api/lots")
async def lots(
    q:str="", category:str="", rarity:str="",
    enhancement:str="", qty_from:Optional[int]=None, qty_to:Optional[int]=None,
    sort:str="price", asc:bool=True, page:int=0, per_page:int=10,
):
    await load_items()
    ids = search_ids(q, category)
    if not ids: return {"lots":[],"total":0,"page":page,"pages":0}
    unloaded = [i for i in ids[:40] if not ITEMS_DB.get(i,{}).get("_loaded")]
    if unloaded: await batch_load(unloaded)

    rar_vals = RARITY_MAP.get(rarity) if rarity else None

    async with aiohttp.ClientSession(headers=sc_hdrs()) as s:
        responses = await asyncio.gather(
            *[s.get(f"{API_BASE}/auction/{i}/lots", params={"limit":200}) for i in ids[:40]],
            return_exceptions=True
        )
        all_lots = []
        for r, i in zip(responses, ids[:40]):
            if isinstance(r, Exception): continue
            try:
                d = await r.json()
                for lot in d.get("lots",[]): all_lots.append(enrich(lot, i))
            except: pass

    filtered = []
    for lot in all_lots:
        if rar_vals is not None and lot["_quality"] not in rar_vals: continue
        if enhancement and lot["_enh"] != int(enhancement): continue
        amt = lot.get("amount",1)
        if qty_from is not None and amt < qty_from: continue
        if qty_to   is not None and amt > qty_to:   continue
        filtered.append(lot)

    def key(l):
        if sort=="per_unit": amt=l.get("amount",1) or 1; return (l.get("buyoutPrice") or l.get("startPrice") or 0)/amt
        if sort=="price":    return l.get("buyoutPrice") or l.get("startPrice") or 0
        if sort=="bid":      return l.get("startPrice",0)
        if sort=="amount":   return l.get("amount",0)
        if sort=="quality":  return l.get("_quality",0)
        return l.get("endTime","")

    filtered.sort(key=key, reverse=not asc)
    total = len(filtered); pages = max(1, -(total//-per_page))
    return {"lots":filtered[page*per_page:(page+1)*per_page],"total":total,"page":page,"pages":pages}

@app.get("/api/history/{item_id}")
async def history(item_id: str, limit: int=50):
    async with aiohttp.ClientSession(headers=sc_hdrs()) as s:
        try:
            async with s.get(f"{API_BASE}/auction/{item_id}/history", params={"limit":limit}) as r:
                if r.status != 200: raise HTTPException(404, "История не найдена")
                data = await r.json()
                records = data.get("prices", data) if isinstance(data,dict) else data
        except HTTPException: raise
        except Exception as e: raise HTTPException(500, str(e))
    prices = [r.get("price", r.get("buyoutPrice",0)) for r in records if r.get("price") or r.get("buyoutPrice")]
    stats = {}
    if prices:
        stats = {"min":min(prices),"max":max(prices),"avg":int(sum(prices)/len(prices)),
                 "median":int(statistics.median(prices)),"stdev":int(statistics.stdev(prices)) if len(prices)>2 else 0}
    chart = []
    for r in records:
        p=r.get("price",r.get("buyoutPrice",0)); ts=r.get("time",r.get("soldAt",""))
        if p and ts:
            try:
                dt=datetime.fromisoformat(ts.replace("Z","+00:00"))
                chart.append({"ts":dt.isoformat(),"price":p,"amount":r.get("amount",1)})
            except: pass
    chart.sort(key=lambda x:x["ts"])
    return {"item_id":item_id,"item_name":ITEMS_DB.get(item_id,{}).get("name",item_id),"records":chart,"stats":stats}

@app.get("/api/profitable")
async def profitable(q:str="", category:str="", rarity:str="", threshold:float=0.80):
    await load_items()
    ids = search_ids(q, category)[:30]
    rar_vals = RARITY_MAP.get(rarity) if rarity else None
    if not ids: return {"lots":[],"stats":{"checked":0,"found":0}}
    async with aiohttp.ClientSession(headers=sc_hdrs()) as s:
        responses = await asyncio.gather(
            *[s.get(f"{API_BASE}/auction/{i}/lots", params={"limit":200}) for i in ids],
            return_exceptions=True
        )
    by_item: dict = {}
    for r, i in zip(responses, ids):
        if isinstance(r, Exception): continue
        try:
            d = await r.json()
            lots = [enrich(l,i) for l in d.get("lots",[])]
            if rar_vals: lots = [l for l in lots if l["_quality"] in rar_vals]
            if lots: by_item[i] = lots
        except: pass
    profitable = []; checked = 0
    for i, lots in by_item.items():
        checked += len(lots)
        if len(lots) < 2: continue
        nz = [l.get("buyoutPrice") or l.get("startPrice") or 0 for l in lots]
        nz = [p for p in nz if p > 0]
        if len(nz) >= 2:
            med = statistics.median(nz)
            for lot in lots:
                p = lot.get("buyoutPrice") or lot.get("startPrice") or 0
                if 0 < p <= med * threshold:
                    disc = round((1-p/med)*100,1)
                    lot.update({"_discount":disc,"_avg_price":int(med),"_profit_label":f"-{disc}% от рынка"})
                    profitable.append(lot)
    seen = set(); unique = []
    for l in profitable:
        k = (l.get("_id"), l.get("startTime",""), l.get("startPrice",0))
        if k not in seen: seen.add(k); unique.append(l)
    unique.sort(key=lambda l:l.get("_discount",0), reverse=True)
    return {"lots":unique[:50],"stats":{"checked":checked,"found":len(unique)}}
