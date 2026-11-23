"""騎士即時路況回報系統 — Flask 後端

啟動：python app.py   （設定環境變數 REDIS_URL 就連 Redis Cloud，沒設定用本機模擬）
"""
import json, os, queue, threading, time

from flask import Flask, Response, jsonify, render_template, request

import rr_core
from rr_core import K_CH, TYPES, Store

r, MODE = rr_core.connect()
store = Store(r)
app = Flask(__name__)
app.json.sort_keys = False  # 保持路況類型的順序
app.jinja_env.policies["json.dumps_kwargs"] = {"sort_keys": False, "ensure_ascii": False}

# ── 背景工作：Streams 消費者群組 → Pub/Sub ──
def worker():
    while True:
        try:
            store.consume_once(block=1000)
        except Exception as e:
            print("worker error:", e)
            time.sleep(2)

# ── 本機模擬模式下 pubsub 只能在同一個 process，用一條訂閱執行緒把訊息分給各個瀏覽器 ──
listeners, lock = set(), threading.Lock()

def fanout():
    ps = r.pubsub(ignore_subscribe_messages=True)
    ps.subscribe(K_CH)
    while True:
        try:
            m = ps.get_message(timeout=1.0)
        except Exception as e:
            print("pubsub error:", e)
            time.sleep(2)
            ps = r.pubsub(ignore_subscribe_messages=True)
            ps.subscribe(K_CH)
            continue
        if m and m["type"] == "message":
            with lock:
                for q in list(listeners):
                    q.put(m["data"])

threading.Thread(target=worker, daemon=True).start()
threading.Thread(target=fanout, daemon=True).start()


@app.get("/")
def index():
    return render_template("index.html", types=TYPES, mode=MODE)


@app.get("/stats")
def stats_page():
    return render_template("stats.html", types=TYPES, mode=MODE)


@app.get("/api/types")
def api_types():
    return jsonify(TYPES)


@app.get("/api/nearby")
def api_nearby():
    lat, lng = float(request.args.get("lat", 24.9994)), float(request.args.get("lng", 121.4997))
    km = min(float(request.args.get("km", 3)), 20)
    return jsonify(store.nearby(lat, lng, km))


@app.post("/api/report")
def api_report():
    b = request.get_json(force=True)
    try:
        return jsonify(store.add(b.get("type"), b.get("lat"), b.get("lng"), b.get("note", ""), b.get("rider", "")))
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400


@app.post("/api/vote/<rid>")
def api_vote(rid):
    b = request.get_json(force=True)
    try:
        return jsonify(store.vote(rid, b.get("kind"), b.get("rider", "")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.get("/api/stats")
def api_stats():
    days = min(int(request.args.get("days", 30)), 365)
    typ = request.args.get("type") or None
    s = store.stats(days, typ if typ in TYPES else None)
    s["ranking"] = store.ranking()
    s["recent"] = store.history(15)
    return jsonify(s)


@app.get("/api/events")
def api_events():
    q = queue.Queue(maxsize=200)
    with lock:
        listeners.add(q)

    def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    yield f"data: {q.get(timeout=15)}\n\n"
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            with lock:
                listeners.discard(q)

    return Response(gen(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if __name__ == "__main__":
    if MODE == "local" or os.environ.get("SEED"):
        import seed
        seed.run(store)
    port = int(os.environ.get("PORT", 5080))
    print(f"Redis 模式：{MODE}，開啟 http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True, debug=False)
