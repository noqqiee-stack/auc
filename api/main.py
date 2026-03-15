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

# Client-Id + Client-Secret напрямую в заголовках (App Token)
SC_HEADERS = {
    "Client-Id":     CLIENT_ID,
    "Client-Secret": CLIENT_SECRET,
}

# GitHub Tree API — отдаёт ВСЕ файлы репо одним запросом
GITHUB_TREE_URL = (
    "https://api.github.com/repos/EXBO-Studio/stalcraft-database"
    "/git/trees/main?recursive=1"
)
GITHUB_RAW = "https://raw.githubusercontent.com/EXBO-Studio/stalcraft-database/main"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="STALCRAFT Auction API", version="5.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ITEMS_DB: dict = {}

RARITY_MAP = {
    "white": [0], "green": [1], "blue": [2],
    "red": [3, 4], "yellow": [5],
}

CATEGORY_MAP = {
    "weapon":     "weapon",
    "armor":      "armor",
    "attachment": "attachment",
    "container":  "container",
    "device":     "device",
    "misc":       "misc",
    "artefact":   "artifact",
    "artifact":   "artifact",
}

async def load_items():
    """Загружает список предметов через GitHub Tree API."""
    global ITEMS_DB
    if ITEMS_DB:
        return

    log.info("Загружаю дерево файлов с GitHub...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                GITHUB_TREE_URL,
                headers={"Accept": "application/vnd.github+json"},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status != 200:
                    log.error(f"GitHub Tree API: {resp.status}")
                    return
                data = await resp.json()

        tree = data.get("tree", [])
        # Ищем файлы вида: ru/items/{category}/{subcat?}/{id}.json
        items_found = 0
        for node in tree:
            path = node.get("path", "")
            if not path.startswith("ru/items/"):
                continue
            if not path.endswith(".json"):
                continue

            parts = path.split("/")
            # parts: ["ru", "items", "weapon", "pistol", "0n9q.json"] или
            #        ["ru", "items", "artefact", "0n9q.json"]
            if len(parts) < 4:
                continue

            item_id = parts[-1].replace(".json", "")
            category_raw = parts[2]  # weapon / armor / artefact / misc ...
            category = CATEGORY_MAP.get(category_raw, category_raw)

            # Имя будем подгружать по требованию, пока храним id + category
            ITEMS_DB[item_id] = {
                "name":     item_id,  # временно
                "category": category,
                "path":     path,
                "_loaded":  False,
            }
            items_found += 1

        log.info(f"Найдено {items_found} предметов в дереве GitHub")

        # Подгружаем имена для первых 500 предметов (быстрый batch)
        await load_item_names_batch(list(ITEMS_DB.keys())[:500])

    except Exception as e:
        log.error(f"Ошибка загрузки дерева: {e}")


async def load_item_names_batch(item_ids: List[str]):
    """Подгружает имена предметов из GitHub."""
    async def fetch_one(session, item_id):
        info = ITEMS_DB.get(item_id)
        if not info or info.get("_loaded"):
            return
        path = info.get("path", "")
        if not path:
            return
        try:
            url = f"{GITHUB_RAW}/{path}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    d = await resp.json(content_type=None)
                    name_obj = d.get("name", {})
                    if isinstance(name_obj, dict):
                        lines = name_obj.get("lines", {})
                        name = lines.get("ru", lines.get("en", item_id))
                    else:
                        name = str(name_obj) if name_obj else item_id
                    ITEMS_DB[item_id]["name"]    = name
                    ITEMS_DB[item_id]["_loaded"] = True
        except Exception:
            pass

    async with aiohttp.ClientSession() as session:
        tasks = [fetch_one(session, iid) for iid in item_ids]
        await asyncio.gather(*tasks, return_exceptions=True)
    log.info(f"Имена загружены для {sum(1 for v in ITEMS_DB.values() if v.get('_loaded'))} предметов")


@app.on_event("startup")
async def startup():
    await load_items()


def search_ids(query: str = "", category: str = "") -> List[str]:
    if not ITEMS_DB:
        return []
    q = query.lower().strip()
    result = []
    for iid, d in ITEMS_DB.items():
        if category and d.get("category") != category:
            continue
        if q:
            name = d.get("name", iid).lower()
            if not (q in name or q in iid.lower() or
                    any(w in name for w in q.split())):
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


@app.get("/api/health")
async def health():
    await load_items()
    return {
        "status": "ok",
        "items":  len(ITEMS_DB),
        "loaded": sum(1 for v in ITEMS_DB.values() if v.get("_loaded")),
    }


@app.get("/api/items/search")
async def api_items_search(q: str = "", category: str = "", limit: int = 30):
    await load_items()
    ids = search_ids(q, category)[:limit]
    return [{"id": iid, "name": ITEMS_DB[iid]["name"],
             "category": ITEMS_DB[iid]["category"]}
            for iid in ids if iid in ITEMS_DB]


@app.get("/api/lots")
async def api_lots(
    q: str = "", category: str = "", rarity: str = "",
    enhancement: str = "", qty_from: Optional[int] = None,
    qty_to: Optional[int] = None, seller: str = "",
    sort: str = "price", asc: bool = True,
    page: int = 0, per_page: int = 10,
):
    await load_items()
    ids = search_ids(q, category)
    if not ids:
        return {"lots": [], "total": 0, "page": page, "pages": 0}

    # Подгружаем имена если не загружены
    unloaded = [iid for iid in ids[:40] if not ITEMS_DB.get(iid, {}).get("_loaded")]
    if unloaded:
        await load_item_names_batch(unloaded)

    rar_vals = RARITY_MAP.get(rarity) if rarity else None

    async with aiohttp.ClientSession(headers=SC_HEADERS) as session:
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
    return {
        "lots":  filtered[page*per_page:(page+1)*per_page],
        "total": total, "page": page, "pages": pages,
    }


@app.get("/api/history/{item_id}")
async def api_history(item_id: str, limit: int = 50):
    async with aiohttp.ClientSession(headers=SC_HEADERS) as session:
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
                chart_data.append({"ts": dt.isoformat(), "price": p,
                                   "amount": r.get("amount", 1)})
            except Exception:
                pass
    chart_data.sort(key=lambda x: x["ts"])
    return {
        "item_id":   item_id,
        "item_name": ITEMS_DB.get(item_id, {}).get("name", item_id),
        "records":   chart_data,
        "stats":     stats,
    }


@app.get("/api/profitable")
async def api_profitable(
    q: str = "", category: str = "",
    rarity: str = "", threshold: float = 0.80,
):
    await load_items()
    ids      = search_ids(q, category)[:30]
    rar_vals = RARITY_MAP.get(rarity) if rarity else None
    if not ids:
        return {"lots": [], "stats": {"checked": 0, "found": 0}}

    async with aiohttp.ClientSession(headers=SC_HEADERS) as session:
        tasks     = [session.get(f"{API_BASE}/auction/{iid}/lots",
                                  params={"limit": 200}) for iid in ids]
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
                    lot.update({"_discount": disc, "_avg_price": int(med),
                                "_profit_label": f"-{disc}% от рынка"})
                    profitable.append(lot)

    seen = set(); unique = []
    for l in profitable:
        k = (l.get("_id"), l.get("startTime",""), l.get("startPrice",0))
        if k not in seen: seen.add(k); unique.append(l)
    unique.sort(key=lambda l: l.get("_discount",0), reverse=True)
    return {"lots": unique[:50], "stats": {"checked": total_checked, "found": len(unique)}}
