import json, os, random, time, urllib.request, urllib.error, boto3

BUCKET = os.environ["BUCKET"]
ACCT = os.environ["CF_ACCOUNT_ID"]
TOKEN = os.environ["CF_API_TOKEN"]
URL = f"https://api.cloudflare.com/client/v4/accounts/{ACCT}/registrar/domain-check"
RULE = "domainhunt-every-minute"
TLDS = ["com", "io"]
PER_CALL = 20 // len(TLDS)   # API max 20 domains/request
CALLS = 12                   # per invocation; API limit 30 req/60s, invoked 1/min
HERE = os.path.dirname(__file__)
s3 = boto3.client("s3")
rd = lambda f: [w.strip() for w in open(os.path.join(HERE, f)) if w.strip()]
WORDS = rd("words.txt")

def get(key, default):
    try: return json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
    except s3.exceptions.NoSuchKey: return default

def check(domains):
    req = urllib.request.Request(URL, json.dumps({"domains": domains}).encode(), {
        "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json", "User-Agent": "domainhunt/2.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)["result"]["domains"]

def handler(event, ctx):
    data = get("results.json", {"checked": 0, "available": [], "updated": None})
    data.pop("recent", None)
    for r in data["available"]: r.pop("kind", None)
    seen = set(get("state/seen.json", []))
    pool = [w for w in WORDS if w not in seen]
    if not pool:   # every dictionary word checked -> delete our own schedule
        ev = boto3.client("events")
        ev.remove_targets(Rule=RULE, Ids=["1"])
        ev.delete_rule(Name=RULE)
        return {"done": True, "schedule_deleted": True}
    random.shuffle(pool)
    now = lambda: time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    for i in range(CALLS):
        words = [pool.pop() for _ in range(min(PER_CALL, len(pool)))]
        if not words: break
        try:
            res = check([w + "." + t for w in words for t in TLDS])
        except urllib.error.HTTPError as e:
            print("HTTP", e.code, e.read()[:200])
            if e.code == 429: break
            continue
        by = {d["name"]: d for d in res}
        for w in words:
            row = {"word": w, "at": now()}
            for t in TLDS:
                d = by.get(f"{w}.{t}")
                p = (d or {}).get("pricing", {})
                row[t] = {"ok": bool(d and d.get("registrable")), "price": p.get("registration_cost"), "renewal": p.get("renewal_cost")}
            seen.add(w)
            data["checked"] += 1
            if any(row[t]["ok"] for t in TLDS):
                data["available"].append(row)
        time.sleep(2.1)
    data["checked"] = len(seen)
    data["updated"] = now()
    s3.put_object(Bucket=BUCKET, Key="state/seen.json", Body=json.dumps(sorted(seen)))
    s3.put_object(Bucket=BUCKET, Key="results.json", Body=json.dumps(data), ContentType="application/json", CacheControl="no-cache")
    return {"checked": data["checked"], "available": len(data["available"])}
