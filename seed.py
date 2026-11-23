"""產生示範資料：目前進行中的路況＋過去 30 天的歷史紀錄（給統計頁用）

只在資料庫是空的時候寫入。  python seed.py  可單獨執行（會讀 REDIS_URL）。
"""
import random, time

import rr_core
from rr_core import AREAS, TYPES, area_of

RIDERS = ["忍4騎士", "中和小黃", "永和豆漿控", "板橋阿翔", "夜騎俠", "通勤族小陳",
          "新店山道王", "雙北跑跑", "重機女孩", "JET 阿宏"]
NOTES = {
    "police":   ["攔酒測", "警車兩台在路口", "臨檢中，慢慢過", "員警在橋下"],
    "camera":   ["固定式測速 50", "移動式測速，限速 40", "區間測速起點"],
    "accident": ["兩台機車擦撞，佔一個車道", "汽機車事故，救護車已到", "有人滑倒，小心"],
    "work":     ["機慢車道封閉", "挖路施工，路面不平", "夜間施工到 05:00"],
    "pothole":  ["很深的坑，晚上看不到", "人孔蓋凹陷", "柏油剝落一大片"],
    "flood":    ["積水到腳踏板", "地下道積水，改走平面", "排水孔堵住"],
}
# 各類型在一天中的權重（讓統計圖看起來合理）
HOUR_W = {
    "police":   [6, 5, 4, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 3, 4, 6, 8, 8],
    "accident": [1, 1, 1, 1, 1, 2, 3, 8, 9, 5, 3, 3, 4, 3, 3, 3, 4, 7, 9, 8, 4, 3, 2, 2],
    "work":     [6, 6, 5, 4, 3, 1, 1, 1, 2, 3, 3, 3, 2, 3, 3, 3, 2, 1, 1, 1, 2, 4, 6, 7],
    "flood":    [1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 3, 3, 4, 5, 6, 7, 7, 6, 4, 3, 2, 1, 1, 1],
    "pothole":  [1] * 24, "camera": [1] * 24,
}
TYPE_W = {"police": 30, "accident": 18, "work": 14, "pothole": 16, "camera": 10, "flood": 8}
HOT = ["中和區", "永和區", "板橋區", "中和區", "新店區", "三重區", "大安區", "萬華區", "中正區", "土城區"]


def pick(rnd, w):
    return rnd.choices(list(w), weights=list(w.values()))[0]


def run(store, force=False):
    r = store.r
    if not force and r.exists(rr_core.K_SEQ):
        return 0
    rnd = random.Random(42)
    now = int(time.time())
    # 1) 過去 30 天歷史（只寫 hist:*，用 Pipeline 一次送出）
    p = r.pipeline(transaction=False)
    for _ in range(420):
        typ = pick(rnd, TYPE_W)
        area = rnd.choice(HOT if rnd.random() < 0.75 else list(AREAS))
        lat, lng = AREAS[area]
        lat += rnd.uniform(-0.012, 0.012); lng += rnd.uniform(-0.012, 0.012)
        day = rnd.randint(1, 30)
        hour = rnd.choices(range(24), weights=HOUR_W[typ])[0]
        lt = time.localtime(now - day * 86400)
        ts = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, rnd.randint(0, 59), 0, 0, 0, -1)))
        if typ == "police" and rnd.random() < 0.4:  # 週五、六晚上臨檢比較多
            ts += ((4 + rnd.randint(0, 1)) - time.localtime(ts).tm_wday) % 7 * 86400
            if ts > now:
                ts -= 7 * 86400
        rid = f"h{r.incr(rr_core.K_SEQ):06d}"
        rider = rnd.choice(RIDERS)
        t = time.localtime(ts)
        p.hset(f"hist:{rid}", mapping={"type": typ, "area": area_of(lat, lng), "hour": t.tm_hour,
                                        "wday": t.tm_wday, "created": ts, "rider": rider, "loc": f"{lng},{lat}"})
        p.zincrby(rr_core.K_RANK, 1, rider)
    p.execute()
    # 2) 現在進行中的路況（中和、永和、板橋一帶）
    n = 0
    for _ in range(28):
        typ = pick(rnd, TYPE_W)
        area = rnd.choice(["中和區", "中和區", "永和區", "板橋區", "新店區", "中正區"])
        lat, lng = AREAS[area]
        lat += rnd.uniform(-0.01, 0.01); lng += rnd.uniform(-0.01, 0.01)
        age = rnd.randint(1, max(2, min(TYPES[typ]["ttl"] // 2, 3 * 3600)))
        v = store.add(typ, lat, lng, rnd.choice(NOTES[typ]), rnd.choice(RIDERS), ts=now - age)
        r.expire(f"report:{v['id']}", TYPES[typ]["ttl"] - age)
        n += 1
    return n


if __name__ == "__main__":
    import sys
    r, mode = rr_core.connect()
    print(mode, "寫入", run(rr_core.Store(r), force="--force" in sys.argv), "筆進行中路況")
