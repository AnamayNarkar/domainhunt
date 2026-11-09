import csv, io, json, os, sys
sys.path.insert(0, os.path.dirname(__file__))
import pytest, domainhunt as dh

class Cfg: retry_limit = 3; delay = 0
def mkdb():
    db = dh.open_db(":memory:"); return db
def cand(w, sc=50): return dict(word=w, source="wordnet", zipf=3.5, score=sc, syllables=2, definition="d")
def ok(name, reg=True, price=True):
    d = {"name": name, "registrable": reg, "tier": "standard"}
    if reg and price: d["pricing"] = {"currency": "USD", "registration_cost": "11.17", "renewal_cost": "11.17"}
    if not reg: d["reason"] = "domain_unavailable"
    return d
no_sleep = lambda s: None

def test_parse_args_dedupes_and_validates_tlds():
    assert dh.parse_args(["--tlds", ".COM", "com", "io"]).tlds == ["com", "io"]
    with pytest.raises(SystemExit): dh.parse_args(["--tlds", "co m"])

def test_profiles_set_min_zipf():
    assert dh.parse_args(["--profile", "rare"]).min_zipf == 2.5
    assert dh.parse_args(["--profile", "common"]).min_zipf == 3.5
    assert dh.parse_args(["--min-zipf", "3.1"]).min_zipf == 3.1

def test_word_regex_ascii_lowercase_only():
    assert all(not dh.WORD_RE.match(w) for w in ["café", "ice-cream", "two words", "abc1", "Apple", ""])
    assert dh.WORD_RE.match("apple")

def test_score_tent_prefers_midfrequency_and_penalizes_technical():
    mid = dh.score("harbor", 3.5, 2, 0.0, False)[0]
    ubiquitous = dh.score("harbor", 6.0, 2, 0.0, False)[0]
    tech = dh.score("harbor", 3.5, 2, 1.0, False)[0]
    assert mid > ubiquitous and mid > tech
    assert dh.score("harbitis", 3.5, 2, 0.5, True)[0] < dh.score("harbitis", 3.5, 2, 0.5, False)[0]

def test_score_unknown_pronunciation_is_neutral_not_excluded():
    unknown, known = dh.score("harbor", 3.5, None, 0, False)[0], dh.score("harbor", 3.5, 2, 0, False)[0]
    assert 0 < unknown < known

def test_ugly_clusters_penalized():
    assert dh.score("strngthx", 3.5, 2, 0, False)[0] < dh.score("stronger", 3.5, 2, 0, False)[0]

def test_inflection_filter():
    words = {"plan", "plans", "agree", "agreed", "walk", "walking", "glass", "glasses", "bus"}
    assert dh.is_inflection("plans", words) and dh.is_inflection("agreed", words) and dh.is_inflection("walking", words)
    assert not dh.is_inflection("glass", words) and not dh.is_inflection("bus", words)

def test_batches_never_exceed_20_for_any_tld_count():
    db = mkdb(); seen = []
    def api(doms): seen.append(len(doms)); return [ok(d) for d in doms], {}
    for tlds in (["com"], ["com", "io", "ai"], ["a1", "bb", "cc", "dd", "ee", "ff", "gg"]):
        db.execute("DELETE FROM domains"); dh.enqueue(db, [cand(f"w{i}") for i in range(13)], tlds)
        dh.run_checks(db, dh.pending(db, {f"w{i}" for i in range(13)}, tlds, 3), Cfg, api, no_sleep)
    assert seen and max(seen) <= 20

def test_registrable_true_without_price_is_still_available():
    db = mkdb(); dh.enqueue(db, [cand("zeta")], ["com"])
    dh.run_checks(db, ["zeta.com"], Cfg, lambda d: ([ok("zeta.com", True, price=False)], {}), no_sleep)
    r = db.execute("SELECT registrable, reg_cost, status FROM domains").fetchone()
    assert r == (1, None, "ok")

def test_missing_response_entry_is_error_not_unavailable():
    db = mkdb(); dh.enqueue(db, [cand("aa"), cand("bb")], ["com"])
    dh.run_checks(db, ["aa.com", "bb.com"], Cfg, lambda d: ([ok("aa.com", False)], {}), no_sleep)
    rows = dict(db.execute("SELECT domain, status FROM domains").fetchall())
    assert rows == {"aa.com": "ok", "bb.com": "error"}
    assert db.execute("SELECT registrable FROM domains WHERE domain='bb.com'").fetchone()[0] is None
    assert dh.pending(db, {"aa", "bb"}, ["com"], 3) == ["bb.com"]          # retried next run

def test_429_honours_retry_after_then_succeeds():
    calls, waits = [], []
    def api(d):
        calls.append(1)
        if len(calls) < 3: raise dh.Transient("HTTP 429", retry_after=7)
        return [ok(x) for x in d], {}
    db = mkdb(); dh.enqueue(db, [cand("zz")], ["com"])
    dh.run_checks(db, ["zz.com"], Cfg, api, waits.append)
    assert waits[:2] == [7, 7] and len(calls) == 3

def test_transient_gives_up_after_retry_limit_and_marks_error():
    calls = []
    def api(d): calls.append(1); raise dh.Transient("HTTP 503")
    db = mkdb(); dh.enqueue(db, [cand("zz")], ["com"])
    dh.run_checks(db, ["zz.com"], Cfg, api, no_sleep)
    assert len(calls) == Cfg.retry_limit + 1
    assert db.execute("SELECT status, attempts FROM domains").fetchone() == ("error", 1)

def test_auth_error_is_fatal_and_not_retried():
    calls = []
    def api(d): calls.append(1); raise dh.Fatal("HTTP 403")
    db = mkdb(); dh.enqueue(db, [cand("zz")], ["com"])
    with pytest.raises(dh.Fatal): dh.run_checks(db, ["zz.com"], Cfg, api, no_sleep)
    assert len(calls) == 1

def test_bad_request_not_retried_and_attempts_bounded():
    calls = []
    def api(d): calls.append(1); raise dh.BadRequest("HTTP 400")
    db = mkdb(); dh.enqueue(db, [cand("zz")], ["com"])
    for _ in range(5): dh.run_checks(db, dh.pending(db, {"zz"}, ["com"], 3), Cfg, api, no_sleep)
    assert len(calls) == 3                                                  # stops after retry_limit attempts

def test_resume_skips_checked_and_new_tld_checks_only_new_domains():
    db = mkdb(); seen = []
    def api(d): seen.extend(d); return [ok(x, False) for x in d], {}
    dh.enqueue(db, [cand("aa"), cand("bb")], ["com"])
    dh.run_checks(db, dh.pending(db, {"aa", "bb"}, ["com"], 3), Cfg, api, no_sleep)
    dh.enqueue(db, [cand("aa"), cand("bb")], ["com", "io"])
    seen.clear(); dh.run_checks(db, dh.pending(db, {"aa", "bb"}, ["com", "io"], 3), Cfg, api, no_sleep)
    assert sorted(seen) == ["aa.io", "bb.io"]
    seen.clear(); dh.run_checks(db, dh.pending(db, {"aa", "bb"}, ["com", "io"], 3), Cfg, api, no_sleep)
    assert seen == []

def test_export_consistent_header_com_first(tmp_path):
    db = mkdb(); dh.enqueue(db, [cand("aa", 10), cand("bb", 90)], ["com", "io"])
    dh.run_checks(db, ["aa.com", "aa.io", "bb.io", "bb.com"], Cfg, lambda d: ([ok(x) for x in d], {}), no_sleep)
    p = tmp_path / "available.csv"; assert dh.export_available(db, str(p)) == 4
    rows = list(csv.reader(open(p)))
    assert rows[0] == dh.COLUMNS and all(len(r) == len(dh.COLUMNS) for r in rows)
    assert [r[0] for r in rows[1:]] == ["bb.com", "aa.com", "bb.io", "aa.io"]

def test_rate_header_parsing():
    class H(dict): pass
    assert dh.rate_wait(H({"ratelimit": '"registrar_domain_check";r=0;t=12'})) == 12.5
    assert dh.rate_wait(H({"ratelimit": '"registrar_domain_check";r=20;t=12'})) == 0
    assert dh.rate_wait({}) == 0

def test_legacy_import_keeps_files_and_does_not_trust_seen(tmp_path, capsys):
    (tmp_path / "available.csv").write_text("word,com_price,io_price\nfoo,11.17,\nbar,,32.00\n")
    (tmp_path / "seen.txt").write_text("foo\nbar\nbaz\n")
    db = mkdb(); dh.import_legacy(db, str(tmp_path))
    assert sorted(r[0] for r in db.execute("SELECT domain FROM domains")) == ["bar.io", "foo.com"]
    assert (tmp_path / "seen.txt").read_text() == "foo\nbar\nbaz\n"
