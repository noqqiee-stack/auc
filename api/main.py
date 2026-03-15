#!/usr/bin/env python3
import asyncio
import json
import os
import statistics
import logging
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
ITEMS_URL     = "https://raw.githubusercontent.com/EXBO-Studio/stalcraft-database/main/ru/items.json"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="STALCRAFT Auction API", version="4.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

ITEMS_DB: dict = {}
_access_token: str = ""

RARITY_MAP = {
    "white": [0], "green": [1], "blue": [2],
    "red": [3, 4], "yellow": [5],
}

# ── Получаем App Access Token ────────────────────────────────────
async def get_access_token() -> str:
    global _access_token
    async with aiohttp.ClientSession() as session:
        async with session.post(
            TOKEN_URL,
            data={
                "grant_type":    "client_credentials",
                "client_id":     CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "scope":         "",
            },
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                _access_token = data.get("access_token", "")
                log.info(f"Access token получен: {_access_token[:30]}...")
                return _access_token
            else:
                text = await resp.text()
                log.error(f"Ошибка получения токена: {resp.status} {text}")
                return ""

def sc_headers() -> dict:
    return {"Authorization": f"Bearer {_access_token}"}

# ── База предметов с GitHub EXBO ─────────────────────────────────
async def load_items():
    global ITEMS_DB
    log.info("Загружаю предметы с GitHub EXBO...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(ITEMS_URL, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    items = data if isinstance(data, list) else list(data.values())
                    for item in items:
                        iid = item.get("id", "")
                        if not iid:
                            continue
                        name_obj = item.get("name", {})
                        name = name_obj.get("ru", name_obj.get("en", iid)) \
                               if isinstance(name_obj, dict) else str(name_obj or iid)
                        ITEMS_DB[iid] = {
                            "name":     name,
                            "category": item.get("category", "misc"),
                        }
                    log.info(f"Загружено {len(ITEMS_DB)} предметов")
    except Exception as e:
        log.error(f"Ошибка загрузки предметов: {e}")

@app.on_event("startup")
async def startup():
    await get_access_token()
    await load_items()

# ── Helpers ──────────────────────────────────────────────────────
def search_ids(query: str = "", category: str = "") -> List[str]:
    q = query.lower().strip()
    result = []
    for iid, d in ITEMS_DB.items():
        if category and d.get("category") != category:
            continue
        if q:
            name = d.get("name", "").lower()
            if not (q in name or any(w in name for w in q.split())):
                continue
        result.append(iid)
    return result

def fmt_time_left(end_str: str) -> str:
    try:
        end   = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        delta = end - datetime.now(timezone.utc)
        s     = int(delta.total_seconds())
        if s <= 0: return "Истёк"
        d, r = divmod(s, 86400); h, r = divmod(r, 3600); m, _ = divmod(r, 60)
        if d: return f"{d}д {h}ч"
        if h: return f"{h}ч {m}м"
        return f"{m}м"
    except Exception:
        return "—"

def enrich_lot(lot: dict, item_id: str) -> dict:
    lot["_id"]       = item_id
    lot["_name"]     = ITEMS_DB.get(item_id, {}).get("name", item_id)
    lot["_category"] = ITEMS_DB.get(item_id, {}).get("category", "misc")
    lot["_timeLeft"] = fmt_time_left(lot.get("endTime", ""))
    add              = lot.get("additional", {})
    lot["_quality"]  = add.get("quality", 0)
    lot["_enh"]      = add.get("potentialLevel", 0)
    amt              = lot.get("amount", 1) or 1
    price            = lot.get("buyoutPrice") or lot.get("startPrice") or 0
    lot["_perUnit"]  = price // amt if amt > 1 and price else None
    return lot

# ── Routes ───────────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    return {"status": "ok", "items": len(ITEMS_DB), "token": bool(_access_token)}

@app.get("/api/items/search")
async def api_items_search(q: str = "", category: str = "", limit: int = 30):
    ids = search_ids(q, category)[:limit]
    return [{"id": iid, **ITEMS_DB[iid]} for iid in ids if iid in ITEMS_DB]

@app.get("/api/lots")
async def api_lots(
    q: str = "", category: str = "", rarity: str = "",
    enhancement: str = "", qty_from: Optional[int] = None,
    qty_to: Optional[int] = None, seller: str = "",
    sort: str = "price", asc: bool = True,
    page: int = 0, per_page: int = 10,
):
    ids = search_ids(q, category)
    if not ids:
        return {"lots": [], "total": 0, "page": page, "pages": 0}

    rar_vals = RARITY_MAP.get(rarity) if rarity else None

    async with aiohttp.ClientSession(headers=sc_headers()) as session:
        tasks = [
            session.get(f"{API_BASE}/auction/{iid}/lots", params={"limit": 200})
            for iid in ids[:40]
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)
        all_lots = []
        for resp, iid in zip(responses, ids[:40]):
            if isinstance(resp, Exception): continue
            try:
                data = await resp.json()
                for lot in data.get("lots", []):
                    all_lots.append(enrich_lot(lot, iid))
            except Exception:
                pass

    filtered = []
    for lot in all_lots:
        if rar_vals is not None and lot["_quality"] not in rar_vals: continue
        if enhancement and lot["_enh"] != int(enhancement): continue
        amt = lot.get("amount", 1)
        if qty_from is not None and amt < qty_from: continue
        if qty_to is not None and amt > qty_to: continue
        if seller:
            s = (lot.get("sellerName") or lot.get("seller") or "").lower()
            if seller.lower() not in s: continue
        filtered.append(lot)

    def _key(l):
        if sort == "per_unit":
            amt = l.get("amount", 1) or 1
            return (l.get("buyoutPrice") or l.get("startPrice") or 0) / amt
        if sort == "price": return l.get("buyoutPrice") or l.get("startPrice") or 0
        if sort == "bid":   return l.get("startPrice", 0)
        return l.get("endTime", "")

    filtered.sort(key=_key, reverse=not asc)
    total = len(filtered)
    pages = max(1, -(-total // per_page))
    return {"lots": filtered[page*per_page:(page+1)*per_page], "total": total, "page": page, "pages": pages}

@app.get("/api/history/{item_id}")
async def api_history(item_id: str, limit: int = 50):
    async with aiohttp.ClientSession(headers=sc_headers()) as session:
        try:
            async with session.get(
                f"{API_BASE}/auction/{item_id}/history", params={"limit": limit}
            ) as resp:
                if resp.status != 200:
                    raise HTTPException(404, "История не найдена")
                data    = await resp.json()
                records = data.get("prices", data) if isinstance(data, dict) else data
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(500, str(e))

    prices = [r.get("price", r.get("buyoutPrice", 0)) for r in records
              if r.get("price") or r.get("buyoutPrice")]
    stats  = {}
    if prices:
        stats = {
            "min":    min(prices), "max": max(prices),
            "avg":    int(sum(prices)/len(prices)),
            "median": int(statistics.median(prices)),
            "stdev":  int(statistics.stdev(prices)) if len(prices) > 2 else 0,
        }
    chart_data = []
    for r in records:
        p  = r.get("price", r.get("buyoutPrice", 0))
        ts = r.get("time", r.get("soldAt", ""))
        if p and ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                chart_data.append({"ts": dt.isoformat(), "price": p, "amount": r.get("amount", 1)})
            except Exception:
                pass
    chart_data.sort(key=lambda x: x["ts"])
    return {"item_id": item_id, "item_name": ITEMS_DB.get(item_id, {}).get("name", item_id),
            "records": chart_data, "stats": stats}

@app.get("/api/profitable")
async def api_profitable(q: str = "", category: str = "", rarity: str = "", threshold: float = 0.80):
    ids      = search_ids(q, category)[:30]
    rar_vals = RARITY_MAP.get(rarity) if rarity else None
    if not ids:
        return {"lots": [], "stats": {"checked": 0, "found": 0}}

    async with aiohttp.ClientSession(headers=sc_headers()) as session:
        tasks     = [session.get(f"{API_BASE}/auction/{iid}/lots", params={"limit": 200}) for iid in ids]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

    by_item: dict = {}
    for resp, iid in zip(responses, ids):
        if isinstance(resp, Exception): continue
        try:
            data = await resp.json()
            lots = []
            for lot in data.get("lots", []):
                lot = enrich_lot(lot, iid)
                if rar_vals and lot["_quality"] not in rar_vals: continue
                lots.append(lot)
            if lots: by_item[iid] = lots
        except Exception:
            pass

    profitable = []; total_checked = 0
    for iid, lots in by_item.items():
        total_checked += len(lots)
        if len(lots) < 2: continue
        nz = [l.get("buyoutPrice") or l.get("startPrice") or 0 for l in lots]
        nz = [p for p in nz if p > 0]
        if len(nz) >= 2:
            med = statistics.median(nz)
            for lot in lots:
                p = lot.get("buyoutPrice") or lot.get("startPrice") or 0
                if 0 < p <= med * threshold:
                    disc = round((1 - p/med)*100, 1)
                    lot.update({"_discount": disc, "_avg_price": int(med), "_profit_label": f"-{disc}% от рынка"})
                    profitable.append(lot)

    seen = set(); unique = []
    for l in profitable:
        k = (l.get("_id"), l.get("startTime",""), l.get("startPrice",0))
        if k not in seen: seen.add(k); unique.append(l)
    unique.sort(key=lambda l: l.get("_discount",0), reverse=True)
    return {"lots": unique[:50], "stats": {"checked": total_checked, "found": len(unique)}}
