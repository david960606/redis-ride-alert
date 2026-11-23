"""騎士即時路況回報系統 — Redis 資料存取層

用到的 Redis 功能：
  - Hash + TTL（EXPIRE）：每筆路況依類型自動過期
  - GEO（GEOADD / GEOSEARCH）：查附近路況
  - Streams（XADD / XREADGROUP / XACK）：事件紀錄＋消費者群組
  - Pub/Sub（PUBLISH / SUBSCRIBE）：即時推播給騎士
  - Transactions（WATCH / MULTI / EXEC）：投票「還在／已排除」不會算錯
  - Pipelines：批次讀寫
  - Sorted Set（ZINCRBY / ZREVRANGE）：熱心騎士排行榜
  - Index + Aggregation（FT.CREATE / FT.AGGREGATE）：歷史統計
"""
import json, math, os, time, uuid

import redis

# ── 路況類型：名稱、圖示顏色、存活時間（秒）──
TYPES = {
    "police":   {"name": "臨檢",     "color": "#3b82f6", "ttl": 30 * 60},
    "camera":   {"name": "測速照相", "color": "#a855f7", "ttl": 30 * 86400},
    "accident": {"name": "事故",     "color": "#ef4444", "ttl": 60 * 60},
    "work":     {"name": "施工",     "color": "#f59e0b", "ttl": 86400},
    "pothole":  {"name": "坑洞",     "color": "#78716c", "ttl": 7 * 86400},
    "flood":    {"name": "積水",     "color": "#06b6d4", "ttl": 3 * 3600},
}
GONE_LIMIT = 3          # 3 個人說「已排除」就刪除
STILL_EXTEND = 15 * 60  # 有人說「還在」就延長 15 分鐘（不超過原本 TTL）

# 雙北行政區中心點（用最近的中心點判斷回報在哪一區）
AREAS = {
    "中和區": (24.9994, 121.4997), "永和區": (25.0083, 121.5160), "板橋區": (25.0115, 121.4627),
    "新店區": (24.9675, 121.5418), "三重區": (25.0614, 121.4875), "新莊區": (25.0360, 121.4500),
    "土城區": (24.9722, 121.4433), "大安區": (25.0263, 121.5436), "中正區": (25.0324, 121.5199),
    "萬華區": (25.0286, 121.4979), "信義區": (25.0330, 121.5654), "中山區": (25.0645, 121.5332),
    "松山區": (25.0500, 121.5577), "大同區": (25.0634, 121.5130), "文山區": (24.9887, 121.5702),
    "內湖區": (25.0690, 121.5890), "士林區": (25.0930, 121.5250), "北投區": (25.1320, 121.5010),
    "蘆洲區": (25.0850, 121.4730), "五股區": (25.0830, 121.4380),
}

K_GEO, K_STREAM, K_CH = "rr:geo", "rr:events", "rr:alerts"
K_RANK, K_SEQ = "rr:riders", "rr:seq"
GROUP = "notifier"
IDX_HIST = "idx:hist"


def area_of(lat, lng):
    return min(AREAS, key=lambda a: (AREAS[a][0] - lat) ** 2 + ((AREAS[a][1] - lng) * 0.906) ** 2)


def _load_env():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v)


def connect():
    _load_env()
    if os.environ.get("REDIS_URL") == "local":
        os.environ.pop("REDIS_URL")
    url = os.environ.get("REDIS_URL")
    if url:
        return redis.Redis.from_url(url, decode_responses=True), "cloud"
    import fakeredis  # 沒設定 REDIS_URL 時用本機模擬（開發測試用）
    return fakeredis.FakeRedis(decode_responses=True), "local"


class Store:
    def __init__(self, r):
        self.r = r
        self.has_search = self._setup_index()
        try:
            self.r.xgroup_create(K_STREAM, GROUP, id="$", mkstream=True)
        except redis.ResponseError:
            pass  # 群組已存在

    # ── Index：歷史紀錄 hist:* 建索引，給統計頁做 Aggregation ──
    def _setup_index(self):
        try:
            self.r.execute_command("FT.INFO", IDX_HIST)
            return True
        except Exception as e:
            if "unknown" in str(e).lower() and "index" not in str(e).lower():
                return False  # 沒有搜尋模組
        try:
            self.r.execute_command(
                "FT.CREATE", IDX_HIST, "ON", "HASH", "PREFIX", "1", "hist:", "SCHEMA",
                "type", "TAG", "area", "TAG", "hour", "NUMERIC", "SORTABLE",
                "wday", "NUMERIC", "created", "NUMERIC", "SORTABLE", "loc", "GEO")
            return True
        except Exception:
            return False

    # ── 新增回報：Transaction（MULTI/EXEC）一次寫入 Hash、TTL、GEO、歷史、排行、Stream ──
    def add(self, typ, lat, lng, note="", rider="匿名騎士", ts=None):
        if typ not in TYPES:
            raise ValueError("未知的路況類型")
        lat, lng = float(lat), float(lng)
        if not (21 < lat < 26.5 and 119 < lng < 123):
            raise ValueError("位置不在台灣")
        ts = int(ts or time.time())
        rid = f"{self.r.incr(K_SEQ):06d}"
        lt = time.localtime(ts)
        data = {"id": rid, "type": typ, "lat": lat, "lng": lng, "note": note[:60],
                "rider": rider[:16] or "匿名騎士", "created": ts, "still": 0, "gone": 0,
                "area": area_of(lat, lng)}
        hist = {"type": typ, "area": data["area"], "hour": lt.tm_hour, "wday": lt.tm_wday,
                "created": ts, "rider": data["rider"], "loc": f"{lng},{lat}"}
        p = self.r.pipeline(transaction=True)
        p.hset(f"report:{rid}", mapping=data)
        p.expire(f"report:{rid}", TYPES[typ]["ttl"])
        p.geoadd(K_GEO, (lng, lat, rid))
        p.hset(f"hist:{rid}", mapping=hist)
        p.zincrby(K_RANK, 1, data["rider"])
        p.xadd(K_STREAM, {"event": "new", "id": rid, "data": json.dumps(data, ensure_ascii=False)},
               maxlen=5000, approximate=True)
        p.execute()
        return self._view(data)

    # ── 查附近：GEOSEARCH + Pipeline 批次讀 Hash 與剩餘 TTL ──
    def nearby(self, lat, lng, km=3.0):
        hits = self.r.geosearch(K_GEO, longitude=lng, latitude=lat, radius=km, unit="km",
                                withdist=True, sort="ASC", count=200)
        if not hits:
            return []
        p = self.r.pipeline(transaction=False)
        for rid, _ in hits:
            p.hgetall(f"report:{rid}")
            p.ttl(f"report:{rid}")
        res = p.execute()
        out, dead = [], []
        for i, (rid, dist) in enumerate(hits):
            d, ttl = res[2 * i], res[2 * i + 1]
            if not d:
                dead.append(rid)  # Hash 已過期 → 順手從 GEO 移除
                continue
            v = self._view(d)
            v["dist"] = round(float(dist), 2)
            v["ttl"] = ttl
            out.append(v)
        if dead:
            self.r.zrem(K_GEO, *dead)
        return out

    # ── 投票：WATCH 樂觀鎖，同時很多人按也不會算錯 ──
    def vote(self, rid, kind, rider="匿名騎士"):
        key = f"report:{rid}"
        if kind not in ("still", "gone"):
            raise ValueError("kind 只能是 still 或 gone")
        with self.r.pipeline() as p:
            while True:
                try:
                    p.watch(key)
                    d = p.hgetall(key)
                    if not d:
                        p.unwatch()
                        return {"ok": False, "msg": "這筆路況已經過期"}
                    n = int(d.get(kind, 0)) + 1
                    p.multi()
                    if kind == "gone" and n >= GONE_LIMIT:
                        p.delete(key)
                        p.zrem(K_GEO, rid)
                        evt = "removed"
                    else:
                        p.hset(key, kind, n)
                        if kind == "still":
                            full = TYPES[d["type"]]["ttl"]
                            left = self.r.ttl(key)
                            p.expire(key, min(full, max(left, 0) + STILL_EXTEND))
                        evt = "vote"
                    p.zincrby(K_RANK, 1, rider[:16] or "匿名騎士")
                    p.xadd(K_STREAM, {"event": evt, "id": rid, "kind": kind, "n": n}, maxlen=5000, approximate=True)
                    p.execute()
                    return {"ok": True, "event": evt, "n": n}
                except redis.WatchError:
                    continue  # 別人剛好也改了 → 重試

    # ── Streams 消費者：讀新事件 → PUBLISH 給所有連線中的騎士 ──
    def consume_once(self, consumer="worker-1", block=2000):
        resp = self.r.xreadgroup(GROUP, consumer, {K_STREAM: ">"}, count=50, block=block)
        n = 0
        for _, msgs in resp or []:
            for mid, f in msgs:
                self.r.publish(K_CH, json.dumps(f, ensure_ascii=False))
                self.r.xack(K_STREAM, GROUP, mid)
                n += 1
        return n

    def history(self, count=30):
        return [{"sid": mid, **f} for mid, f in self.r.xrevrange(K_STREAM, count=count)]

    def ranking(self, n=10):
        return [{"rider": m, "score": int(s)} for m, s in self.r.zrevrange(K_RANK, 0, n - 1, withscores=True)]

    # ── 統計：FT.AGGREGATE GROUPBY + REDUCE COUNT（沒有搜尋模組時改用程式計算）──
    def stats(self, days=30, typ=None):
        since = int(time.time()) - days * 86400
        if self.has_search:
            try:
                return self._stats_ft(since, typ)
            except Exception:
                pass
        return self._stats_scan(since, typ)

    def _agg(self, q, field):
        res = self.r.execute_command("FT.AGGREGATE", IDX_HIST, q, "GROUPBY", "1", f"@{field}",
                                     "REDUCE", "COUNT", "0", "AS", "n", "SORTBY", "2", "@n", "DESC",
                                     "LIMIT", "0", "50")
        out = {}
        rows = [x["extra_attributes"] for x in res["results"]] if isinstance(res, dict) else res[1:]  # RESP3 / RESP2
        for row in rows:
            d = dict(zip(row[::2], row[1::2])) if isinstance(row, list) else row
            out[str(d.get(field))] = int(d["n"])
        return out

    def _stats_ft(self, since, typ):
        q = f"@created:[{since} +inf]" + (f" @type:{{{typ}}}" if typ else "")
        return {"by_area": self._agg(q, "area"), "by_type": self._agg(q, "type"),
                "by_hour": {int(k): v for k, v in self._agg(q, "hour").items()},
                "by_wday": {int(k): v for k, v in self._agg(q, "wday").items()}, "engine": "FT.AGGREGATE"}

    def _stats_scan(self, since, typ):
        out = {"by_area": {}, "by_type": {}, "by_hour": {}, "by_wday": {}, "engine": "SCAN（本機模擬）"}
        keys = list(self.r.scan_iter("hist:*", count=500))
        p = self.r.pipeline(transaction=False)
        for k in keys:
            p.hgetall(k)
        for d in p.execute():
            if not d or int(d["created"]) < since or (typ and d["type"] != typ):
                continue
            for f, key in (("by_area", d["area"]), ("by_type", d["type"]),
                           ("by_hour", int(d["hour"])), ("by_wday", int(d["wday"]))):
                out[f][key] = out[f].get(key, 0) + 1
        out["by_area"] = dict(sorted(out["by_area"].items(), key=lambda x: -x[1]))
        return out

    @staticmethod
    def _view(d):
        t = TYPES[d["type"]]
        return {"id": d["id"], "type": d["type"], "name": t["name"], "color": t["color"],
                "lat": float(d["lat"]), "lng": float(d["lng"]), "note": d.get("note", ""),
                "rider": d.get("rider", ""), "created": int(d["created"]), "area": d.get("area", ""),
                "still": int(d.get("still", 0)), "gone": int(d.get("gone", 0)), "ttl_full": t["ttl"]}
