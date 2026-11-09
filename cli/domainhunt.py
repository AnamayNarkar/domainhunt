#!/usr/bin/env python3
"""Find brandable single English words with an available .com/.io (Cloudflare Registrar *check* API only;
nothing is ever registered). Resumable via SQLite. See README.md."""
import argparse, csv, hashlib, json, os, random, re, sqlite3, sys, time, urllib.error, urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

API = "https://api.cloudflare.com/client/v4/accounts/{}/registrar/domain-check"
MAX_BATCH = 20                                   # Cloudflare hard limit per request
WORD_RE, TLD_RE = re.compile(r"^[a-z]+$"), re.compile(r"^[a-z]{2,24}$")
PROFILES = {"common": 3.5, "balanced": 2.8, "rare": 2.5}   # min Zipf per profile
TECH_DOMAINS = {"medicine", "biology", "anatomy", "physiology", "pathology", "pharmacology", "chemistry",
                "biochemistry", "botany", "zoology", "genetics", "immunology", "microbiology", "surgery",
                "dentistry", "neurology", "psychiatry", "embryology", "histology", "cytology", "virology",
                "bacteriology", "mycology", "entomology", "ornithology", "paleontology", "pharmacy"}
TAXON_RE = re.compile(r"\b(type genus|genus of|family of|order of|class of|phylum)\b")
MED_SUFFIXES = ("itis", "osis", "ectomy", "otomy", "ostomy", "emia", "aemia", "pathy", "plasty", "scopy", "algia")
BAD_WORDS = {"fuck", "shit", "cunt", "bitch", "nigger", "faggot", "whore", "slut", "rape", "nazi", "retard",
             "dick", "cock", "pussy", "bastard", "damn", "piss", "twat", "wank", "kike", "spic", "chink"}
UGLY_RE = re.compile(r"[^aeiouy]{4,}|q(?!u)|(.)\1\1")
COLUMNS = ["domain", "word", "tld", "brand_score", "zipf_frequency", "definition", "syllables", "source",
           "registrable", "reason", "tier", "currency", "registration_cost", "renewal_cost", "checked_at"]

now_iso = lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

# ---------------------------------------------------------------- scoring (0-100, weights documented in README)
def score(word, zipf, syllables, tech_ratio, med_suffix):
    n = len(word)
    s_len = 20 if 5 <= n <= 8 else 14 if n in (4, 9, 10) else 6
    # tent: recognizable-but-not-ubiquitous words score best (ubiquitous words are always already registered)
    s_freq = max(0.0, 25 * (zipf - 2.0)) if zipf < 3.0 else 25.0 if zipf <= 4.0 else max(0.0, 25 - (zipf - 4.0) * 10)
    s_freq = min(25.0, s_freq)
    s_syl = 10 if syllables is None else {1: 16, 2: 20, 3: 16, 4: 8}.get(syllables, 4)   # unknown = neutral
    s_pron = (10 if syllables is not None else 5) - (5 if UGLY_RE.search(word) else 0)
    s_sem = 25 * (1 - tech_ratio) - (8 if med_suffix and tech_ratio > 0 else 0)
    parts = dict(length=s_len, frequency=round(s_freq, 1), syllables=s_syl, pronounce=s_pron, semantic=round(s_sem, 1))
    return max(0.0, min(100.0, round(sum(parts.values()), 1))), parts

def is_inflection(w, words):
    if w.endswith("s") and not w.endswith("ss") and w[:-1] in words: return True
    if w.endswith("ed") and any(len(b) >= 3 and b in words for b in (w[:-2], w[:-1])): return True
    return w.endswith("ing") and any(len(b) >= 3 and b in words for b in (w[:-3], w[:-3] + "e"))

def wn_info(wn, word):
    syns = wn.synsets(word)
    if not syns: return None
    tech = inst = 0
    for s in syns:
        doms = {d.name().split(".")[0] for d in s.topic_domains()}
        if doms & TECH_DOMAINS or s.lexname() == "noun.body" or TAXON_RE.search(s.definition()): tech += 1
        if s.instance_hypernyms(): inst += 1          # people / places = proper nouns
    return dict(tech=tech / len(syns), proper=inst == len(syns), definition=syns[0].definition(), pos=syns[0].pos())

# ---------------------------------------------------------------- candidate pipeline
def build_candidates(a, log=print):
    import nltk
    from nltk.corpus import wordnet as wn
    from wordfreq import top_n_list, zipf_frequency
    import cmudict
    try: wn.synsets("test")
    except LookupError: nltk.download("wordnet", quiet=True)
    cmu = cmudict.dict()
    stats = []
    def stage(name, n, note=""): stats.append((name, n, note)); log(f"  {name:<34}{n:>8}  {note}")
    wn_words = {l for l in wn.all_lemma_names() if WORD_RE.match(l)}
    wf = {w for w in top_n_list("en", a.wordfreq_n, wordlist="best", ascii_only=True) if WORD_RE.match(w)}
    stage("1 WordNet lemmas (a-z only)", len(wn_words)); stage("2 wordfreq top-N (a-z only)", len(wf))
    stage("3 combined unique", len(wn_words | wf))
    pool = {w for w in wn_words | wf if a.min_len <= len(w) <= a.max_len}
    stage("4 length filter", len(pool), f"{a.min_len}-{a.max_len} letters")
    zipf = {w: zipf_frequency(w, "en") for w in pool}
    pool = sorted(w for w in pool if a.min_zipf <= zipf[w] <= a.max_zipf)
    stage("5 frequency filter", len(pool), f"{a.min_zipf} <= zipf <= {a.max_zipf}")
    drops, keep = Counter(), []
    for w in pool:
        if w not in wn_words: drops["not a WordNet lemma (name/slang/typo/inflection)"] += 1; continue
        if is_inflection(w, wn_words): drops["inflection (plural/-ed/-ing) of another word"] += 1; continue
        if w in BAD_WORDS: drops["profanity/offensive"] += 1; continue
        info = wn_info(wn, w)
        if info is None: drops["no WordNet sense"] += 1
        elif info["proper"]: drops["proper noun (person/place)"] += 1
        elif info["tech"] >= 1.0: drops["only technical/medical/biological senses"] += 1
        else: keep.append((w, info))
    stage("6 semantic filter", len(keep))
    for k, v in drops.most_common(): log(f"      - dropped {v:>6}: {k}")
    out, with_pron = [], 0
    for w, info in keep:
        prons = cmu.get(w); syl = sum(p[-1].isdigit() for p in prons[0]) if prons else None
        with_pron += prons is not None
        med = w.endswith(MED_SUFFIXES)
        sc, parts = score(w, zipf[w], syl, info["tech"], med)
        out.append(dict(word=w, source="+".join(s for s, ok in (("wordnet", w in wn_words), ("wordfreq", w in wf)) if ok),
                        zipf=round(zipf[w], 2), score=sc, syllables=syl, definition=info["definition"], pos=info["pos"],
                        tech_ratio=round(info["tech"], 2), why=json.dumps(parts)))
    stage("7 pronunciation (CMUdict)", with_pron, f"{len(keep) - with_pron} unknown, kept (neutral)")
    out.sort(key=lambda r: (-r["score"], hashlib.md5(r["word"].encode()).hexdigest()))  # stable, non-alphabetical tiebreak
    stage("8 FINAL candidates", len(out))
    return out, stats

# ---------------------------------------------------------------- Cloudflare client
class Fatal(Exception): pass                     # 401/403: stop the whole run
class BadRequest(Exception): pass                # 400 / success=false: this batch is bad, don't hammer it
class Transient(Exception):
    def __init__(self, msg, retry_after=None): super().__init__(msg); self.retry_after = retry_after

def api_post(domains, acct, token, timeout=30):
    req = urllib.request.Request(API.format(acct), json.dumps({"domains": domains}).encode(), {
        "Authorization": f"Bearer {token}", "Content-Type": "application/json", "User-Agent": "domainhunt/3.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: body, headers = r.read(), r.headers
    except urllib.error.HTTPError as e:
        msg = e.read()[:200].decode("utf8", "replace")
        if e.code in (401, 403): raise Fatal(f"HTTP {e.code} (check token/permissions): {msg}")
        if e.code == 429:
            ra = e.headers.get("Retry-After"); raise Transient("HTTP 429", float(ra) if ra and ra.replace(".", "").isdigit() else None)
        if e.code >= 500: raise Transient(f"HTTP {e.code}")
        raise BadRequest(f"HTTP {e.code}: {msg}")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise Transient(f"network error: {e}")
    try: j = json.loads(body)
    except ValueError: raise Transient("malformed JSON response")
    if not j.get("success"): raise BadRequest(f"API errors: {j.get('errors')}")
    if not isinstance((j.get("result") or {}).get("domains"), list): raise Transient("response missing result.domains")
    return j["result"]["domains"], headers

def call_with_retries(domains, cfg, api, sleep):
    for attempt in range(cfg.retry_limit + 1):
        try: return api(domains)
        except Transient as e:
            if attempt == cfg.retry_limit: raise
            wait = e.retry_after if e.retry_after is not None else min(60, 2 ** attempt + random.random())
            print(f"  transient ({e}); retry in {wait:.1f}s"); sleep(wait)

def rate_wait(headers):
    """Cloudflare sends: ratelimit: "name";r=<remaining>;t=<seconds to reset>"""
    m = re.search(r"r=(\d+);\s*t=(\d+)", (headers.get("ratelimit") if headers else "") or "")
    return int(m.group(2)) + 0.5 if m and int(m.group(1)) <= 1 else 0

# ---------------------------------------------------------------- SQLite
SCHEMA = """CREATE TABLE IF NOT EXISTS domains(domain TEXT PRIMARY KEY, word TEXT, tld TEXT, source TEXT, zipf REAL,
 score REAL, syllables INT, definition TEXT, registrable INT, reason TEXT, tier TEXT, currency TEXT, reg_cost TEXT,
 renewal_cost TEXT, checked_at TEXT, status TEXT DEFAULT 'pending', attempts INT DEFAULT 0, last_error TEXT)"""

def open_db(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    db = sqlite3.connect(path); db.execute(SCHEMA); return db

def enqueue(db, cands, tlds):
    db.executemany("INSERT OR IGNORE INTO domains(domain,word,tld,source,zipf,score,syllables,definition) VALUES(?,?,?,?,?,?,?,?)",
                   [(f"{c['word']}.{t}", c["word"], t, c["source"], c["zipf"], c["score"], c["syllables"], c["definition"])
                    for c in cands for t in tlds])
    db.commit()

def pending(db, words, tlds, retry_limit):
    q = ",".join("?" * len(tlds))
    rows = db.execute(f"SELECT domain, word FROM domains WHERE tld IN ({q}) AND (status='pending' OR (status='error' AND attempts<?)) "
                      "ORDER BY score DESC, domain", (*tlds, retry_limit)).fetchall()
    return [d for d, w in rows if w in words]

def run_checks(db, domains, cfg, api, sleep=time.sleep):
    done = 0
    for i in range(0, len(domains), MAX_BATCH):
        batch = domains[i:i + MAX_BATCH]
        try: result, headers = call_with_retries(batch, cfg, api, sleep)
        except (BadRequest, Transient) as e:
            db.executemany("UPDATE domains SET status='error', attempts=attempts+1, last_error=? WHERE domain=?", [(str(e)[:300], d) for d in batch])
            db.commit(); print(f"  batch failed: {e}"); sleep(cfg.delay); continue
        by = {str(d.get("name", "")).lower(): d for d in result}
        for dom in batch:
            d = by.get(dom)
            if d is None or "registrable" not in d:      # never assume "taken" when the answer is missing
                db.execute("UPDATE domains SET status='error', attempts=attempts+1, last_error='missing from API response' WHERE domain=?", (dom,))
                continue
            p = d.get("pricing") or {}
            db.execute("UPDATE domains SET registrable=?, reason=?, tier=?, currency=?, reg_cost=?, renewal_cost=?, checked_at=?, status='ok', last_error=NULL WHERE domain=?",
                       (int(bool(d["registrable"])), d.get("reason"), d.get("tier"), p.get("currency"), p.get("registration_cost"), p.get("renewal_cost"), now_iso(), dom))
            done += 1
            if d["registrable"]: print(f"  AVAILABLE {dom}")
        db.commit(); sleep(max(cfg.delay, rate_wait(headers)))
    return done

# ---------------------------------------------------------------- export / legacy
def export_available(db, path):
    rows = db.execute("SELECT domain,word,tld,score,zipf,definition,syllables,source,registrable,reason,tier,currency,reg_cost,renewal_cost,checked_at "
                      "FROM domains WHERE registrable=1 ORDER BY (tld!='com'), score DESC, domain").fetchall()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f); w.writerow(COLUMNS); w.writerows(rows)
    return len(rows)

def import_legacy(db, d):
    """available.csv (word,com_price,io_price,...) -> rows marked available (stale until rechecked).
    seen.txt is NOT imported: it can't distinguish 'taken' from 'missing API entry'. Old files are left untouched."""
    p = os.path.join(d, "available.csv"); n = 0
    if os.path.exists(p):
        for r in csv.DictReader(open(p)):
            for k, v in r.items():
                if k and k.endswith("_price") and v:
                    t = k[:-6]; db.execute("INSERT OR IGNORE INTO domains(domain,word,tld,registrable,reg_cost,checked_at,status) VALUES(?,?,?,1,?, 'legacy-import','ok')", (f"{r['word']}.{t}", r["word"], t, v)); n += 1
        db.commit()
    s = os.path.join(d, "seen.txt")
    print(f"legacy: imported {n} available domains" + (f"; ignored {len(open(s).read().split())} words in seen.txt (will be re-verified)" if os.path.exists(s) else ""))

def write_candidates(cands, path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    keys = ["word", "score", "zipf", "syllables", "pos", "tech_ratio", "source", "definition", "why"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, keys, extrasaction="ignore"); w.writeheader(); w.writerows(cands)

# ---------------------------------------------------------------- CLI
def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tlds", nargs="+", default=["com", "io"]); ap.add_argument("--profile", choices=PROFILES, default="balanced")
    ap.add_argument("--min-zipf", type=float, help="overrides profile (common 3.5 / balanced 2.8 / rare 2.5)")
    ap.add_argument("--max-zipf", type=float, default=5.0, help="skip ubiquitous words (always already registered)")
    ap.add_argument("--min-len", type=int, default=4); ap.add_argument("--max-len", type=int, default=10)
    ap.add_argument("--wordfreq-n", type=int, default=150000); ap.add_argument("--dry-run", action="store_true", help="no API calls; preview + candidates.csv")
    ap.add_argument("--top", type=int, default=100); ap.add_argument("--limit", type=int, help="max domains to check this run")
    ap.add_argument("--seed", type=int, help="shuffle check order (default: best brand score first)")
    ap.add_argument("--delay", type=float, default=2.1, help="seconds between requests (limit is 30/min)")
    ap.add_argument("--retry-limit", type=int, default=3); ap.add_argument("--recheck", type=int, metavar="DAYS", help="re-check results older than DAYS")
    ap.add_argument("--export", action="store_true", help="only export available.csv from the DB"); ap.add_argument("--out", default="out")
    ap.add_argument("--import-legacy", metavar="DIR", help="import old available.csv (seen.txt is ignored)")
    a = ap.parse_args(argv)
    a.tlds = list(dict.fromkeys(t.lower().lstrip(".") for t in a.tlds))
    bad = [t for t in a.tlds if not TLD_RE.match(t)]
    if bad: ap.error(f"invalid TLDs: {bad}")
    if a.min_zipf is None: a.min_zipf = PROFILES[a.profile]
    return a

def main(argv=None):
    a = parse_args(argv); db = open_db(os.path.join(a.out, "domainhunt.db"))
    if a.import_legacy: import_legacy(db, a.import_legacy)
    if a.export: print(f"exported {export_available(db, os.path.join(a.out, 'available.csv'))} available domains"); return
    print("Building candidates:"); cands, _ = build_candidates(a)
    write_candidates(cands, os.path.join(a.out, "candidates.csv"))
    if a.dry_run:
        print(f"\nTop {a.top} by brand score (full list: {a.out}/candidates.csv)")
        for c in cands[:a.top]: print(f"{c['score']:5.1f}  {c['word']:<12} z={c['zipf']:<4} syl={c['syllables']}  [{c['pos']}] {c['definition'][:70]}")
        return
    acct, token = os.environ.get("CF_ACCOUNT_ID"), os.environ.get("CF_API_TOKEN")
    if not (acct and token): sys.exit("set CF_ACCOUNT_ID and CF_API_TOKEN")
    enqueue(db, cands, a.tlds)
    if a.recheck:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=a.recheck)).strftime("%Y-%m-%d %H:%M:%S")
        db.execute("UPDATE domains SET status='pending' WHERE status='ok' AND (checked_at<? OR checked_at='legacy-import')", (cutoff,)); db.commit()
    todo = pending(db, {c["word"] for c in cands}, a.tlds, a.retry_limit)
    if a.seed is not None: random.Random(a.seed).shuffle(todo)
    if a.limit: todo = todo[:a.limit]
    print(f"\n{len(todo)} domains to check ({len(a.tlds)} TLDs: {', '.join(a.tlds)})")
    try: n = run_checks(db, todo, a, lambda doms: api_post(doms, acct, token))
    except Fatal as e: sys.exit(f"stopping: {e}")
    except KeyboardInterrupt: n = 0; print("\ninterrupted - progress saved")
    print(f"checked {n}; exported {export_available(db, os.path.join(a.out, 'available.csv'))} available domains to {a.out}/available.csv")

if __name__ == "__main__":
    main()
