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
GITHUB_TREE   = "https://api.github.com/repos/EXBO-Studio/stalcraft-database/git/trees/main?recursive=1"
GITHUB_RAW    = "https://raw.githubusercontent.com/EXBO-Studio/stalcraft-database/main"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="STALCRAFT API", version="5.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ITEMS_DB: dict = {}   # id -> {name, category, color, icon_path, item_path}
_token: str = ""

# ── Color mapping ─────────────────────────────────────────────
# Artifact quality values (numeric from API lots)
ART_QUALITY = {0:"COMMON",1:"UNCOMMON",2:"SPECIAL",3:"RARE",4:"EPIC",5:"LEGENDARY"}
# Reverse: color string -> numeric quality index for filtering
COLOR_TO_Q = {
    # Artifacts
    "ARTEFACT_COMMON":0,"ARTEFACT_UNCOMMON":1,"ARTEFACT_SPECIAL":2,
    "ARTEFACT_RARE":3,"ARTEFACT_EPIC":4,"ARTEFACT_LEGENDARY":5,
    # Some items use these
    "COMMON":0,"UNCOMMON":1,"SPECIAL":2,"RARE":3,"EPIC":4,"LEGENDARY":5,
    # Item ranks
    "RANK_BEGINNER":0,"RANK_STALKER":1,"RANK_VETERAN":2,"RANK_MASTER":3,"RANK_LEGEND":4,
    "BEGINNER":0,"STALKER":1,"VETERAN":2,"MASTER":3,"LEGEND":4,
}

RARITY_MAP = {
    "white":[0],"green":[1],"blue":[2],"red":[3,4],"yellow":[5]
}

async def get_token() -> str:
    global _token
    async with aiohttp.ClientSession() as s:
        async with s.post(TOKEN_URL, data={"grant_type":"client_credentials","client_id":CLIENT_ID,"client_secret":CLIENT_SECRET,"scope":""}) as r:
            if r.status == 200:
                d = await r.json()
                _token = d.get("access_token","")
                log.info("Токен получен")
            else:
                log.error(f"Ошибка токена: {r.status}")
    return _token

def sc_hdrs(): return {"Authorization": f"Bearer {_token}"}

# ── Items loading ─────────────────────────────────────────────
async def load_items():
    global ITEMS_DB
    if ITEMS_DB: return
    log.info("Загружаю дерево файлов...")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(GITHUB_TREE, headers={"Accept":"application/vnd.github+json"}, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status != 200:
                    log.error(f"GitHub: {r.status}"); return
                data = await r.json()
        tree = data.get("tree", [])
        for node in tree:
            path = node.get("path","")
            if not path.startswith(f"{REGION}/items/"): continue
            if not path.endswith(".json"): continue
            parts = path.split("/")
            if len(parts) < 4: continue
            item_id  = parts[-1].replace(".json","")
            # Category: everything after "items/" except filename
            cat_path = "/".join(parts[2:-1])   # e.g. "weapon/pistol"
            cat_main = parts[2]                 # e.g. "weapon"
            # Icon path mirrors item path but in icons/ folder
            icon_path = f"{REGION}/icons/{cat_path}/{item_id}.png"
            ITEMS_DB[item_id] = {
                "name":      item_id,
                "category":  cat_main,
                "cat_path":  cat_path,
                "color":     "",
                "icon_path": icon_path,
                "item_path": path,
                "_loaded":   False,
            }
        log.info(f"Найдено {len(ITEMS_DB)} предметов")
        # Batch load names+colors for first 300
        await batch_load_names(list(ITEMS_DB.keys())[:300])
    except Exception as e:
        log.error(f"Ошибка загрузки: {e}")

async def load_one(session, item_id):
    info = ITEMS_DB.get(item_id)
    if not info or info.get("_loaded"): return
    try:
        url = f"{GITHUB_RAW}/{info['item_path']}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status == 200:
                d = await r.json(content_type=None)
                # Name
                name_obj = d.get("name",{})
                if isinstance(name_obj, dict):
                    lines = name_obj.get("lines",{})
                    name = lines.get("ru", lines.get("en", item_id))
                else:
                    name = str(name_obj) if name_obj else item_id
                # Color
                color = d.get("color","")
                ITEMS_DB[item_id].update({"name":name,"color":color,"_loaded":True})
    except Exception:
        pass

async def batch_load_names(ids: List[str]):
    async with aiohttp.ClientSession() as s:
        await asyncio.gather(*[load_one(s, iid) for iid in ids], return_exceptions=True)
    loaded = sum(1 for v in ITEMS_DB.values() if v.get("_loaded"))
    log.info(f"Имена загружены: {loaded}")

@app.on_event("startup")
async def startup():
    await get_token()
    await load_items()

# ── Helpers ───────────────────────────────────────────────────
def search_ids(q="", category=""):
    if not ITEMS_DB: return []
    ql = q.lower().strip()
    result = []
    for iid, d in ITEMS_DB.items():
        if category and d.get("category") != category: continue
        if ql:
            name = d.get("name", iid).lower()
            if not (ql in name or ql in iid.lower() or any(w in name for w in ql.split())):
                continue
        result.append(iid)
    return result

def fmt_time(end_str):
    try:
        end = datetime.fromisoformat(end_str.replace("Z","+00:00"))
        s = int((end - datetime.now(timezone.utc)).total_seconds())
        if s <= 0: return "Истёк"
        d,r=divmod(s,86400); h,r=divmod(r,3600); m,_=divmod(r,60)
        if d: return f"{d}д {h}ч"
        if h: return f"{h}ч {m}м"
        return f"{m}м"
    except: return "—"

def enrich(lot, iid):
    lot["_id"]       = iid
    info             = ITEMS_DB.get(iid, {})
    lot["_name"]     = info.get("name", iid)
    lot["_category"] = info.get("category","misc")
    lot["_color"]    = info.get("color","")       # color string from DB
    lot["_icon"]     = info.get("icon_path","")
    lot["_timeLeft"] = fmt_time(lot.get("endTime",""))
    add = lot.get("additional",{})
    lot["_quality"]  = add.get("quality", 0)
    lot["_enh"]      = add.get("potentialLevel",0)
    lot["_studied"]  = add.get("isResearched", add.get("researched", None))
    amt = lot.get("amount",1) or 1
    price = lot.get("buyoutPrice") or lot.get("startPrice") or 0
    lot["_perUnit"]  = price // amt if amt > 1 and price else None
    return lot

# ── Routes ────────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    await load_items()
    return {"status":"ok","items":len(ITEMS_DB),"loaded":sum(1 for v in ITEMS_DB.values() if v.get("_loaded")),"token":bool(_token)}

@app.get("/api/items/search")
async def items_search(q:str="",category:str="",limit:int=40):
    await load_items()
    ids = search_ids(q, category)
    # Load names for unloaded items
    unloaded = [i for i in ids[:limit] if not ITEMS_DB.get(i,{}).get("_loaded")]
    if unloaded:
        await batch_load_names(unloaded)
    return [
        {"id":iid,"name":ITEMS_DB[iid]["name"],"category":ITEMS_DB[iid]["category"],
         "color":ITEMS_DB[iid]["color"],"icon_path":ITEMS_DB[iid]["icon_path"]}
        for iid in ids[:limit] if iid in ITEMS_DB
    ]

@app.get("/api/lots")
async def lots(q:str="",category:str="",rarity:str="",enhancement:str="",
               qty_from:Optional[int]=None,qty_to:Optional[int]=None,
               sort:str="price",asc:bool=True,page:int=0,per_page:int=10):
    await load_items()
    ids = search_ids(q, category)
    if not ids: return {"lots":[],"total":0,"page":page,"pages":0}
    unloaded=[i for i in ids[:40] if not ITEMS_DB.get(i,{}).get("_loaded")]
    if unloaded: await batch_load_names(unloaded)
    rar_vals = RARITY_MAP.get(rarity) if rarity else None
    async with aiohttp.ClientSession(headers=sc_hdrs()) as s:
        res = await asyncio.gather(*[s.get(f"{API_BASE}/auction/{i}/lots",params={"limit":200}) for i in ids[:40]],return_exceptions=True)
        all_lots=[]
        for r,i in zip(res,ids[:40]):
            if isinstance(r,Exception): continue
            try:
                d = await r.json()
                for lot in d.get("lots",[]):
                    all_lots.append(enrich(lot,i))
            except: pass
    filtered=[]
    for lot in all_lots:
        if rar_vals is not None and lot["_quality"] not in rar_vals: continue
        if enhancement and lot["_enh"]!=int(enhancement): continue
        amt=lot.get("amount",1)
        if qty_from is not None and amt<qty_from: continue
        if qty_to is not None and amt>qty_to: continue
        filtered.append(lot)
    def key(l):
        if sort=="per_unit": amt=l.get("amount",1) or 1; return (l.get("buyoutPrice") or l.get("startPrice") or 0)/amt
        if sort=="price": return l.get("buyoutPrice") or l.get("startPrice") or 0
        if sort=="bid": return l.get("startPrice",0)
        if sort=="amount": return l.get("amount",0)
        if sort=="quality": return l.get("_quality",0)
        return l.get("endTime","")
    filtered.sort(key=key,reverse=not asc)
    total=len(filtered);pages=max(1,-(total//-per_page))
    return {"lots":filtered[page*per_page:(page+1)*per_page],"total":total,"page":page,"pages":pages}

@app.get("/api/history/{item_id}")
async def history(item_id:str,limit:int=50):
    async with aiohttp.ClientSession(headers=sc_hdrs()) as s:
        async with s.get(f"{API_BASE}/auction/{item_id}/history",params={"limit":limit}) as r:
            if r.status!=200: raise HTTPException(404,"История не найдена")
            data=await r.json()
            records=data.get("prices",data) if isinstance(data,dict) else data
    prices=[r.get("price",r.get("buyoutPrice",0)) for r in records if r.get("price") or r.get("buyoutPrice")]
    stats={}
    if prices:
        stats={"min":min(prices),"max":max(prices),"avg":int(sum(prices)/len(prices)),"median":int(statistics.median(prices)),"stdev":int(statistics.stdev(prices)) if len(prices)>2 else 0}
    chart=[]
    for r in records:
        p=r.get("price",r.get("buyoutPrice",0)); ts=r.get("time",r.get("soldAt",""))
        if p and ts:
            try: dt=datetime.fromisoformat(ts.replace("Z","+00:00")); chart.append({"ts":dt.isoformat(),"price":p,"amount":r.get("amount",1)})
            except: pass
    chart.sort(key=lambda x:x["ts"])
    return {"item_id":item_id,"item_name":ITEMS_DB.get(item_id,{}).get("name",item_id),"records":chart,"stats":stats}

@app.get("/api/profitable")
async def profitable(q:str="",category:str="",rarity:str="",threshold:float=0.80):
    await load_items()
    ids=search_ids(q,category)[:30]; rar_vals=RARITY_MAP.get(rarity) if rarity else None
    if not ids: return {"lots":[],"stats":{"checked":0,"found":0}}
    async with aiohttp.ClientSession(headers=sc_hdrs()) as s:
        res=await asyncio.gather(*[s.get(f"{API_BASE}/auction/{i}/lots",params={"limit":200}) for i in ids],return_exceptions=True)
    by_item={}
    for r,i in zip(res,ids):
        if isinstance(r,Exception): continue
        try:
            d=await r.json()
            lots=[enrich(l,i) for l in d.get("lots",[])]
            if rar_vals: lots=[l for l in lots if l["_quality"] in rar_vals]
            if lots: by_item[i]=lots
        except: pass
    profitable=[]; checked=0
    for i,lots in by_item.items():
        checked+=len(lots)
        if len(lots)<2: continue
        nz=[l.get("buyoutPrice") or l.get("startPrice") or 0 for l in lots]; nz=[p for p in nz if p>0]
        if len(nz)>=2:
            med=statistics.median(nz)
            for lot in lots:
                p=lot.get("buyoutPrice") or lot.get("startPrice") or 0
                if 0<p<=med*threshold:
                    disc=round((1-p/med)*100,1); lot.update({"_discount":disc,"_avg_price":int(med),"_profit_label":f"-{disc}% от рынка"}); profitable.append(lot)
    seen=set(); unique=[]
    for l in profitable:
        k=(l.get("_id"),l.get("startTime",""),l.get("startPrice",0))
        if k not in seen: seen.add(k); unique.append(l)
    unique.sort(key=lambda l:l.get("_discount",0),reverse=True)
    return {"lots":unique[:50],"stats":{"checked":checked,"found":len(unique)}}
