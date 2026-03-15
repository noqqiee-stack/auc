#!/usr/bin/env python3
"""
STALCRAFT Auction API v9

Редкость/ранг = КОНСТАНТА предмета из базы данных:
  - поле color: ARTEFACT_COMMON/UNCOMMON/SPECIAL/RARE/EPIC/LEGENDARY
                RANK_BEGINNER/STALKER/VETERAN/MASTER/LEGEND
  - если color=="DEFAULT" — парсим из infoBlocks

additional.quality в лоте = % эффективности артефакта (85-175), НЕ редкость.
additional.potentialLevel = заточка.
"""
import asyncio, os, statistics, logging
from typing import Optional, List
from datetime import datetime, timezone
import aiohttp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

CLIENT_ID     = os.getenv("CLIENT_ID",     "1434")
CLIENT_SECRET = os.getenv("CLIENT_SECRET", "0dMttjkdBsyVyRSRInJpKYWahGnSxBwgFehkuTCb")
REGION        = os.getenv("REGION",        "ru")
API_BASE      = f"https://eapi.stalcraft.net/{REGION}"
TOKEN_URL     = "https://exbo.net/oauth/token"
GITHUB_TREE   = "https://api.github.com/repos/EXBO-Studio/stalcraft-database/git/trees/main?recursive=1"
GITHUB_RAW    = "https://raw.githubusercontent.com/EXBO-Studio/stalcraft-database/main"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="STALCRAFT API", version="9.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

COLOR_IDX = {
    "ARTEFACT_COMMON":0,"ARTEFACT_UNCOMMON":1,"ARTEFACT_SPECIAL":2,
    "ARTEFACT_RARE":3,"ARTEFACT_EPIC":4,"ARTEFACT_LEGENDARY":5,
    "RANK_BEGINNER":0,"RANK_STALKER":1,"RANK_VETERAN":2,"RANK_MASTER":3,"RANK_LEGEND":4,
}

BLOCK_IDX = {
    "core.quality.common":0,"core.quality.uncommon":1,"core.quality.special":2,
    "core.quality.rare":3,"core.quality.epic":4,"core.quality.legendary":5,
    "core.rank.picklock":0,"core.rank.stalker":1,"core.rank.veteran":2,
    "core.rank.master":3,"core.rank.legend":4,
}

def parse_idx(blocks):
    for block in blocks:
        if not isinstance(block,dict): continue
        for el in block.get("elements",[]):
            if not isinstance(el,dict): continue
            for f in ("key","value"):
                obj=el.get(f,{})
                if isinstance(obj,dict):
                    k=obj.get("key","")
                    if k in BLOCK_IDX: return BLOCK_IDX[k]
    return 0

ITEMS_DB: dict = {}
_token: str = ""

async def get_token():
    global _token
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(TOKEN_URL,data={"grant_type":"client_credentials",
                "client_id":CLIENT_ID,"client_secret":CLIENT_SECRET,"scope":""}) as r:
                if r.status==200:
                    _token=(await r.json()).get("access_token","")
                    log.info("Токен получен")
                else: log.error(f"Токен: {r.status}")
    except Exception as e: log.error(f"Токен ошибка: {e}")
    return _token

def hdrs(): return {"Authorization":f"Bearer {_token}"}

async def load_items():
    global ITEMS_DB
    if ITEMS_DB: return
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(GITHUB_TREE,headers={"Accept":"application/vnd.github+json"},
                             timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status!=200: return
                data=await r.json()
        for node in data.get("tree",[]):
            path=node.get("path","")
            if not path.startswith(f"{REGION}/items/") or not path.endswith(".json"): continue
            parts=path.split("/")
            if len(parts)<4: continue
            iid=parts[-1].replace(".json","")
            cat=parts[2]
            cp="/".join(parts[2:-1])
            ITEMS_DB[iid]={"name":iid,"category":cat,"is_art":cat=="artefact",
                           "color_idx":0,"icon_path":f"{REGION}/icons/{cp}/{iid}.png",
                           "item_path":path,"_loaded":False}
        log.info(f"Найдено {len(ITEMS_DB)} предметов")
        await _batch(list(ITEMS_DB.keys())[:400])
    except Exception as e: log.error(f"load_items: {e}")

async def _load_one(s,iid):
    info=ITEMS_DB.get(iid)
    if not info or info["_loaded"]: return
    try:
        async with s.get(f"{GITHUB_RAW}/{info['item_path']}",
                         timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status!=200: return
            d=await r.json(content_type=None)
        n=d.get("name",{})
        name=(n.get("lines",{}).get("ru") or n.get("lines",{}).get("en") or iid) \
            if isinstance(n,dict) else (str(n) if n else iid)
        cs=(d.get("color") or "DEFAULT").strip()
        idx=COLOR_IDX.get(cs,-1)
        if idx<0: idx=parse_idx(d.get("infoBlocks",[]))
        raw=d.get("category",info["category"])
        is_art=str(raw).split("/")[0]=="artefact"
        ITEMS_DB[iid].update({"name":name,"color_idx":idx,"is_art":is_art,"_loaded":True})
    except: pass

async def _batch(ids):
    async with aiohttp.ClientSession() as s:
        await asyncio.gather(*[_load_one(s,i) for i in ids],return_exceptions=True)

@app.on_event("startup")
async def startup(): await get_token(); await load_items()

def search_ids(q,category):
    if not ITEMS_DB: return []
    ql=q.lower().strip()
    out=[]
    for iid,d in ITEMS_DB.items():
        if category and d["category"]!=category: continue
        if ql:
            name=d.get("name",iid).lower()
            if not(ql in name or ql in iid.lower() or any(w in name for w in ql.split())): continue
        out.append(iid)
    return out

def fmt_t(s):
    try:
        diff=int((datetime.fromisoformat(s.replace("Z","+00:00"))-datetime.now(timezone.utc)).total_seconds())
        if diff<=0: return "Истёк"
        d,r=divmod(diff,86400);h,r=divmod(r,3600);m,_=divmod(r,60)
        if d: return f"{d}д {h}ч"
        if h: return f"{h}ч {m}м"
        return f"{m}м"
    except: return "—"

def enrich(lot,iid):
    item=ITEMS_DB.get(iid,{})
    add=lot.get("additional") or {}
    lot["_id"]=iid
    lot["_name"]=item.get("name",iid)
    lot["_category"]=item.get("category","misc")
    lot["_is_art"]=item.get("is_art",False)
    lot["_icon"]=item.get("icon_path","")
    # Редкость/ранг = КОНСТАНТА из базы предмета
    lot["_quality"]=item.get("color_idx",0)
    # Заточка = additional.potentialLevel
    lot["_enh"]=int(add.get("potentialLevel") or 0)
    # % эффективности артефакта (additional.quality = 85-175)
    lot["_eff"]=round(float(add["quality"]),1) if item.get("is_art") and "quality" in add else None
    lot["_studied"]=add.get("isResearched")
    lot["_timeLeft"]=fmt_t(lot.get("endTime",""))
    amt=lot.get("amount",1) or 1
    p=lot.get("buyoutPrice") or lot.get("startPrice") or 0
    lot["_perUnit"]=p//amt if amt>1 and p else None
    return lot

@app.get("/api/health")
async def health():
    await load_items()
    return {"status":"ok","items":len(ITEMS_DB),
            "loaded":sum(1 for v in ITEMS_DB.values() if v["_loaded"]),"token":bool(_token)}

@app.get("/api/items/search")
async def items_search(q:str="",category:str="",limit:int=40):
    await load_items()
    ids=search_ids(q,category)
    unloaded=[i for i in ids[:limit] if not ITEMS_DB[i]["_loaded"]]
    if unloaded: await _batch(unloaded)
    return [{"id":iid,"name":ITEMS_DB[iid]["name"],"category":ITEMS_DB[iid]["category"],
             "is_art":ITEMS_DB[iid]["is_art"],"color_idx":ITEMS_DB[iid]["color_idx"],
             "icon_path":ITEMS_DB[iid]["icon_path"]} for iid in ids[:limit] if iid in ITEMS_DB]

@app.get("/api/lots")
async def get_lots(q:str="",category:str="",quality_values:str="",
                   enh_from:Optional[int]=None,enh_to:Optional[int]=None,
                   sort:str="price",asc:bool=True,page:int=0,per_page:int=10):
    if not _token: await get_token()
    await load_items()
    ids=search_ids(q,category)
    if not ids: return {"lots":[],"total":0,"page":page,"pages":0}
    unloaded=[i for i in ids[:40] if not ITEMS_DB[i]["_loaded"]]
    if unloaded: await _batch(unloaded)
    qf=[int(x) for x in quality_values.split(",") if x.strip().lstrip("-").isdigit()] if quality_values else []
    async with aiohttp.ClientSession(headers=hdrs()) as s:
        res=await asyncio.gather(*[s.get(f"{API_BASE}/auction/{iid}/lots",
                                         params={"limit":200,"additional":"true"})
                                   for iid in ids[:40]],return_exceptions=True)
        all_lots=[]
        for resp,iid in zip(res,ids[:40]):
            if isinstance(resp,Exception): continue
            try:
                d=await resp.json()
                for lot in d.get("lots",[]): all_lots.append(enrich(lot,iid))
            except: pass
    filtered=[]
    for lot in all_lots:
        if qf and lot["_quality"] not in qf: continue
        if enh_from is not None and lot["_enh"]<enh_from: continue
        if enh_to is not None and lot["_enh"]>enh_to: continue
        filtered.append(lot)
    def sk(l):
        if sort=="price": return l.get("buyoutPrice") or l.get("startPrice") or 0
        if sort=="bid": return l.get("startPrice",0)
        if sort=="amount": return l.get("amount",0)
        if sort=="quality": return l["_quality"]
        if sort=="enh": return l["_enh"]
        return l.get("endTime","")
    filtered.sort(key=sk,reverse=not asc)
    total=len(filtered);pages=max(1,-(total//-per_page))
    return {"lots":filtered[page*per_page:(page+1)*per_page],"total":total,"page":page,"pages":pages}

@app.get("/api/history/{item_id}")
async def history(item_id:str,limit:int=50):
    if not _token: await get_token()
    async with aiohttp.ClientSession(headers=hdrs()) as s:
        async with s.get(f"{API_BASE}/auction/{item_id}/history",params={"limit":limit}) as r:
            if r.status!=200: raise HTTPException(404,"История не найдена")
            data=await r.json()
            records=data.get("prices",data) if isinstance(data,dict) else data
    prices=[r.get("price",r.get("buyoutPrice",0)) for r in records if r.get("price") or r.get("buyoutPrice")]
    stats={}
    if prices:
        stats={"min":min(prices),"max":max(prices),"avg":int(sum(prices)/len(prices)),
               "median":int(statistics.median(prices)),
               "stdev":int(statistics.stdev(prices)) if len(prices)>2 else 0}
    chart=[]
    for rec in records:
        p=rec.get("price",rec.get("buyoutPrice",0)); ts=rec.get("time",rec.get("soldAt",""))
        if p and ts:
            try: chart.append({"ts":datetime.fromisoformat(ts.replace("Z","+00:00")).isoformat(),"price":p})
            except: pass
    chart.sort(key=lambda x:x["ts"])
    return {"item_id":item_id,"item_name":ITEMS_DB.get(item_id,{}).get("name",item_id),
            "records":chart,"stats":stats}
