import json, time, uuid, random, sys, os
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "http://localhost:7860"

CONSULT = [
    "商品什么时候发货？", "运费怎么算？", "退款要多久到账？",
    "这个产品有红色吗？", "尺寸选哪个好？", "能开发票吗？",
    "客服工作时间是几点？", "怎么联系人工客服？", "订单状态怎么看？",
    "支持货到付款吗？", "有线下门店吗？", "保修期多久？",
    "能发顺丰吗？", "退货地址在哪？", "批量购买有优惠吗？",
    "你们的客服电话是多少？", "快递半路丢了怎么办？", "订错了能改吗？",
    "用什么支付方式？", "需要多少钱才能免运费？",
]
COMPLAINT = [
    "产品坏了，我要退货！", "收到的是空箱子，气死我了",
    "你们的物流也太慢了吧", "客服态度差，我要投诉",
    "第二次了，每次都是这个毛病",
    "这跟描述完全不一样啊",
    "退货时还非要我自己出运费",
    "联系三天了都没人理我",
    "换货等了半个月都没收到",
    "维修报价比我买新的还贵",
]
CHITCHAT = ["你好", "在吗？", "辛苦了", "谢谢", "哈哈", "好的", "天气真好", "晚安", "早上好", "随便问问", "你真可爱", "有用！", "不错不错", "继续努力", "我没什么问题了", "只是一个建议"]
ENDING = ["没有其他问题了，再见", "好的，谢谢，拜拜", "可以了，就这样吧", "明白了，谢谢"]
PRODUCT = [
    "A100 信号放大器怎么用？", "X200 的电池能用多久？",
    "M300 开不了机怎么办？", "蓝牙连不上，显示 e0",
    "WiFi 信号很弱，是不是路由器问题？",
    "S400 的固件怎么更新？", "能关掉状态灯吗？太亮了",
    "N500 麦克风没声音", "L600 底座适配什么型号？",
]
USERS = [f"test_user_{i}" for i in range(1, 51)]

POOLS = [(CONSULT, "consult"), (COMPLAINT, "complaint"), (CHITCHAT, "chitchat"), (ENDING, "ending"), (PRODUCT, "product")]

OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "e2e_samples.json")

def pick():
    pool, cat = POOLS[random.randint(0, 4)]
    return random.choice(pool), cat

def send_one(msg, cat, uid, sid, idx):
    payload = json.dumps({"message": msg, "session_id": sid, "stream": False}).encode("utf-8")
    req = Request(f"{BASE}/api/chat", data=payload, headers={"Content-Type": "application/json", "X-User-Id": uid})
    t0 = time.time()
    try:
        resp = urlopen(req, timeout=180)
        body = json.loads(resp.read().decode("utf-8"))
        lat = time.time() - t0
        return {"ok": True, "latency": round(lat, 3), "data": body, "message": msg, "category": cat, "user_id": uid, "session_id": sid, "index": idx}
    except HTTPError as e:
        return {"ok": False, "latency": round(time.time()-t0, 3), "status": e.code, "error": str(e)[:200], "message": msg, "category": cat, "user_id": uid, "session_id": sid, "index": idx}
    except Exception as e:
        return {"ok": False, "latency": round(time.time()-t0, 3), "error": str(e)[:200], "message": msg, "category": cat, "user_id": uid, "session_id": sid, "index": idx}

def main():
    resume_idx = 0
    existing = []
    if os.path.exists(OUTPUT):
        with open(OUTPUT, "r", encoding="utf-8") as f:
            existing_data = json.load(f)
            existing = existing_data.get("samples", [])
            resume_idx = existing[-1]["index"] if existing else 0
            print(f"Resuming from index {resume_idx + 1}, {len(existing)} existing samples", flush=True)

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    all_samples = existing[:]
    successes = sum(1 for s in existing if s.get("ok"))
    failures = len(existing) - successes
    total_lat = sum(s.get("latency", 0) for s in existing if s.get("ok"))

    # warmup
    if resume_idx == 0:
        send_one("你好", "prewarm", "prewarm-session", 0)
        time.sleep(3)
        print("Prewarm done.", flush=True)

    BATCH_SIZE = 5  # 并发数
    target = 1000
    start = resume_idx + 1

    with ThreadPoolExecutor(max_workers=BATCH_SIZE) as pool:
        for batch_start in range(start, target + 1, BATCH_SIZE):
            batch_end = min(batch_start + BATCH_SIZE - 1, target)
            batch_idx = list(range(batch_start, batch_end + 1))
            futures = {}
            for idx in batch_idx:
                msg, cat = pick()
                uid = random.choice(USERS)
                sid = f"e2e-{uuid.uuid4().hex[:8]}"
                futures[pool.submit(send_one, msg, cat, uid, sid, idx)] = idx

            for future in as_completed(futures):
                result = future.result()
                all_samples.append(result)
                if result["ok"]:
                    successes += 1
                    total_lat += result["latency"]
                else:
                    failures += 1

            # save every 50
            if batch_end % 50 == 0 or batch_end >= target:
                avg = round(total_lat / max(successes, 1), 3)
                with open(OUTPUT, "w", encoding="utf-8") as f:
                    json.dump({"total": len(all_samples), "samples": all_samples,
                               "summary": {"ok": successes, "fail": failures, "avg_latency": avg}},
                              f, ensure_ascii=False, indent=2)
                pct = len(all_samples) / target * 100
                print(f"[{len(all_samples)}/{target}] ok={successes} fail={failures} avg={avg}s ({pct:.0f}%)", flush=True)

            # brief cooldown between batches
            time.sleep(random.uniform(0.5, 1.5))

    print(f"\nDONE: {len(all_samples)} requests", flush=True)
    print(f"  Success: {successes} / Failures: {failures}", flush=True)
    print(f"  Output: {OUTPUT}", flush=True)

if __name__ == "__main__":
    main()