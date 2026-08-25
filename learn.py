"""Crosscut's memory.

GitHub Actions runs are stateless, so anything that improves with time needs a
store. State lives on an orphan `state` branch that is force-pushed each run, so
it persists indefinitely without growing the repo's history.

Four things accumulate here:

  1. Feed health, so dead feeds fail over to Google News automatically.
  2. Per-outlet baseline publishing rates, so a blindspot means "this outlet
     skipped a story it would normally cover" rather than "this outlet has a
     small RSS feed".
  3. Stable story identity across runs, so we can see when each side picked a
     story up.
  4. An outlet co-coverage matrix, which yields a data-derived axis of which
     outlets choose the same stories.
"""
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

STATE_VERSION = 2

# Feed self-healing thresholds, in consecutive failed runs.
FAILS_BEFORE_FALLBACK = 2
FAILS_BEFORE_DISABLE = 8
RETRY_ORIGINAL_EVERY = 60      # runs; occasionally re-test a healed feed's real URL

# How long a story stays eligible to be matched against a new cluster.
STORY_TTL_HOURS = 72

EWMA_ALPHA = 0.15              # weight on the newest run
LEANS = ["left", "lean-left", "center", "lean-right", "right"]
LEFT_SIDE = ("left", "lean-left")
RIGHT_SIDE = ("right", "lean-right")

GOOGLE_NEWS = ("https://news.google.com/rss/search"
               "?q=when:2d+site:{}&hl=en-US&gl=US&ceid=US:en")


# ------------------------------------------------------------------ store

def blank_state():
    return {
        "version": STATE_VERSION,
        "runs": 0,
        "updated": None,
        "outlets": {},
        "cooc": {},
        "story_total": 0,
        "active_stories": {},
        "next_story_id": 1,
    }


def load_state(path):
    p = Path(path) / "state.json"
    if not p.exists():
        return blank_state()
    try:
        s = json.loads(p.read_text("utf-8"))
    except Exception:
        return blank_state()
    if s.get("version") != STATE_VERSION:
        # Schema moved on. Keep the expensive-to-rebuild parts, reset the rest.
        fresh = blank_state()
        fresh["cooc"] = s.get("cooc", {})
        fresh["runs"] = s.get("runs", 0)
        fresh["story_total"] = s.get("story_total", 0)
        for name, o in s.get("outlets", {}).items():
            fresh["outlets"][name] = {**blank_outlet(), **{
                k: o[k] for k in ("ok", "fail", "consec_fail", "fallback", "orig_url",
                                  "disabled", "articles_ewma", "coverage_ewma")
                if k in o
            }}
        return fresh
    return s


def save_state(path, state):
    state["version"] = STATE_VERSION
    state["updated"] = datetime.now(timezone.utc).isoformat()
    d = Path(path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps(state, ensure_ascii=False), "utf-8")


def blank_outlet():
    return {
        "ok": 0, "fail": 0, "consec_fail": 0,
        "fallback": False, "orig_url": None, "disabled": False,
        "articles_ewma": None, "coverage_ewma": None,
        "last_ok": None, "last_error": None,
    }


def outlet_state(state, name):
    return state["outlets"].setdefault(name, blank_outlet())


# --------------------------------------------------- 1. feed self-healing

def domain_of(url):
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return None


def prepare_feeds(outlets, state):
    """Apply learned health to the configured feed list before fetching.

    Returns (feeds_to_try, notes). A feed that has failed repeatedly is
    rewritten to its Google News fallback; one that keeps failing after that is
    dropped, and the caller reports it rather than silently losing that outlet.
    """
    prepared, notes = [], []
    for o in outlets:
        st = outlet_state(state, o["name"])
        o = dict(o)

        if st["disabled"]:
            notes.append({"outlet": o["name"], "action": "disabled",
                          "detail": f"no working feed after {st['consec_fail']} consecutive failures"})
            continue

        if st["fallback"] and st.get("fallback_url"):
            # Periodically give the outlet's real feed another chance.
            if state["runs"] % RETRY_ORIGINAL_EVERY == 0 and st.get("orig_url"):
                o["url"] = st["orig_url"]
                notes.append({"outlet": o["name"], "action": "retry-original",
                              "detail": "re-testing the outlet's own feed"})
            else:
                o["url"] = st["fallback_url"]
                o["via"] = "google-news"
        prepared.append(o)
    return prepared, notes


def record_feed_result(state, outlet, ok, article_count, error=None):
    """Update health and, on repeated failure, arrange a fallback."""
    st = outlet_state(state, outlet["name"])
    if ok:
        st["ok"] += 1
        st["consec_fail"] = 0
        st["last_ok"] = datetime.now(timezone.utc).isoformat()
        st["last_error"] = None
        prev = st["articles_ewma"]
        st["articles_ewma"] = float(article_count) if prev is None \
            else prev + EWMA_ALPHA * (article_count - prev)
        # A retry of the original URL succeeded, so come off the fallback.
        if st["fallback"] and outlet["url"] == st.get("orig_url"):
            st["fallback"] = False
            st["fallback_url"] = None
        return None

    st["fail"] += 1
    st["consec_fail"] += 1
    st["last_error"] = error

    if st["consec_fail"] >= FAILS_BEFORE_DISABLE:
        st["disabled"] = True
        return {"outlet": outlet["name"], "action": "disabled", "detail": error}

    if st["consec_fail"] >= FAILS_BEFORE_FALLBACK and not st["fallback"]:
        dom = domain_of(outlet.get("url", ""))
        if dom and "news.google.com" not in outlet.get("url", ""):
            st["fallback"] = True
            st["orig_url"] = outlet["url"]
            st["fallback_url"] = GOOGLE_NEWS.format(dom)
            return {"outlet": outlet["name"], "action": "failover",
                    "detail": f"native feed failed {st['consec_fail']}x, switching to Google News"}
    return None


# ------------------------------------- 2. baseline rates + blindspot logic

def update_coverage_rates(state, stories, outlets_present):
    """coverage_ewma = share of this run's stories an outlet appeared in."""
    total = max(1, len(stories))
    hits = defaultdict(int)
    for s in stories:
        for name in {a["outlet"] for a in s["articles"]}:
            hits[name] += 1
    for name in outlets_present:
        st = outlet_state(state, name)
        rate = hits.get(name, 0) / total
        prev = st["coverage_ewma"]
        st["coverage_ewma"] = rate if prev is None else prev + EWMA_ALPHA * (rate - prev)


def expected_side(state, outlets, side):
    """Sum of baseline coverage rates for one side of the spectrum.

    This is the number of that side's outlets we'd expect on an average story,
    which is what makes a blindspot mean something when NPR carries 10 items
    and CNN carries 69.
    """
    exp = 0.0
    for o in outlets:
        if o["lean"] not in side:
            continue
        st = state["outlets"].get(o["name"])
        if not st or st.get("disabled"):
            continue
        r = st.get("coverage_ewma")
        exp += 0.12 if r is None else r      # prior for an outlet we know nothing about
    return exp


def blindspot_for(story_counts, exp_left, exp_right, total):
    """Flag a side only when it is materially below its own baseline.

    Returns (side_or_None, detail). `detail` carries observed vs expected so the
    UI can show why, instead of asking for blind trust.
    """
    obs_left = sum(story_counts.get(k, 0) for k in LEFT_SIDE)
    obs_right = sum(story_counts.get(k, 0) for k in RIGHT_SIDE)
    if total < 4:
        return None, None

    def ratio(obs, exp):
        return obs / exp if exp > 0.4 else None

    rl, rr = ratio(obs_left, exp_left), ratio(obs_right, exp_right)
    detail = {
        "left": {"observed": obs_left, "expected": round(exp_left, 2)},
        "right": {"observed": obs_right, "expected": round(exp_right, 2)},
    }
    # Require the other side to be genuinely present, not just noise.
    if rr is not None and rr <= 0.20 and obs_left >= 3 and (rl or 0) >= 0.5:
        return "right", detail
    if rl is not None and rl <= 0.20 and obs_right >= 3 and (rr or 0) >= 0.5:
        return "left", detail
    return None, detail


# ------------------------------------------- 3. stable story identity

def fingerprint(articles, idf, top=8):
    scores = defaultdict(float)
    for a in articles:
        for w in re.findall(r"[a-z0-9']+", a["title"].lower()):
            if len(w) > 3 and idf.get(w, 0) > 2.2:
                scores[w] += idf[w]
    return sorted(scores, key=scores.get, reverse=True)[:top]


def assign_ids(state, stories, idf, now):
    """Carry story ids across runs so first-seen times survive.

    Matching is by fingerprint overlap: a story keeps its identity while at
    least three of its distinctive terms persist.
    """
    active = state["active_stories"]
    cutoff = now - timedelta(hours=STORY_TTL_HOURS)
    for sid in [k for k, v in active.items()
                if datetime.fromisoformat(v["first_seen"]) < cutoff]:
        active.pop(sid, None)

    claimed = set()
    for s in stories:
        fp = set(fingerprint(s["articles"], idf))
        s["fingerprint"] = sorted(fp)

        best, best_overlap = None, 0
        for sid, rec in active.items():
            if sid in claimed:
                continue
            ov = len(fp & set(rec["fingerprint"]))
            if ov > best_overlap:
                best, best_overlap = sid, ov

        if best and best_overlap >= 3:
            sid = best
            rec = active[sid]
            rec["fingerprint"] = sorted(fp | set(rec["fingerprint"]))[:12]
        else:
            sid = f"s{state['next_story_id']}"
            state["next_story_id"] += 1
            rec = {"first_seen": now.isoformat(), "fingerprint": sorted(fp), "lean_first": {}}
            active[sid] = rec
            state["story_total"] += 1

        claimed.add(sid)
        s["id"] = sid
        s["first_seen"] = rec["first_seen"]

        # Record the first time each lean bucket touched this story.
        for a in s["articles"]:
            rec["lean_first"].setdefault(a["lean"], now.isoformat())
        s["lean_first"] = dict(rec["lean_first"])

        age_h = (now - datetime.fromisoformat(rec["first_seen"])).total_seconds() / 3600
        s["age_hours"] = round(age_h, 1)
        s["pickup"] = pickup_gap(rec["lean_first"])


def pickup_gap(lean_first):
    """Hours between the first side touching a story and the other side."""
    def earliest(side):
        times = [lean_first[k] for k in side if k in lean_first]
        return min(times) if times else None
    l, r = earliest(LEFT_SIDE), earliest(RIGHT_SIDE)
    if not l or not r:
        return None
    dl, dr = datetime.fromisoformat(l), datetime.fromisoformat(r)
    gap = abs((dr - dl).total_seconds()) / 3600
    if gap < 1.0:
        return None
    return {"first": "left" if dl < dr else "right", "hours": round(gap, 1)}


# ----------------------------------------- 4. learned co-coverage axis

def record_cooccurrence(state, stories):
    cooc = state["cooc"]
    for s in stories:
        names = sorted({a["outlet"] for a in s["articles"]})
        for i, a in enumerate(names):
            cooc[a] = cooc.get(a, 0) if isinstance(cooc.get(a), int) else cooc.get(a, 0)
            for b in names[i + 1:]:
                key = f"{a}|{b}"
                cooc[key] = cooc.get(key, 0) + 1
    # per-outlet story counts live under a reserved prefix
    for s in stories:
        for name in {a["outlet"] for a in s["articles"]}:
            k = f"#{name}"
            cooc[k] = cooc.get(k, 0) + 1


def learn_axis(state, outlets):
    """Derive a 1-D axis of *story-selection similarity* between outlets.

    Classical MDS on a co-coverage similarity matrix. Double-centering removes
    the dominant "how much does this outlet publish" component, so the leading
    eigenvector is the main contrast between outlets rather than their volume.

    IMPORTANT: this measures which outlets choose the same stories. That
    correlates with political lean but is not the same thing — it can just as
    easily pick up topic mix or newsroom size. Presented as such.
    """
    try:
        import numpy as np
    except ImportError:
        return None

    names = [o["name"] for o in outlets
             if not state["outlets"].get(o["name"], {}).get("disabled")]
    cooc = state["cooc"]
    n = len(names)
    if n < 8:
        return None

    counts = np.array([cooc.get(f"#{m}", 0) for m in names], float)
    if (counts > 0).sum() < 8 or counts.sum() < 200:
        return None                       # not enough history to say anything

    S = np.eye(n)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = sorted((names[i], names[j]))
            c = cooc.get(f"{a}|{b}", 0)
            denom = math.sqrt(max(1.0, counts[i]) * max(1.0, counts[j]))
            S[i, j] = S[j, i] = c / denom

    D2 = (1.0 - S) ** 2
    J = np.eye(n) - np.ones((n, n)) / n
    B = -0.5 * J @ D2 @ J
    vals, vecs = np.linalg.eigh(B)
    v = vecs[:, int(np.argmax(vals))]
    if v.std() < 1e-9:
        return None
    v = (v - v.mean()) / v.std()

    # Orient using the hand-curated labels as poles only, never as values.
    lean_of = {o["name"]: o["lean"] for o in outlets}
    left_mean = np.mean([v[i] for i, m in enumerate(names) if lean_of.get(m) in LEFT_SIDE] or [0])
    right_mean = np.mean([v[i] for i, m in enumerate(names) if lean_of.get(m) in RIGHT_SIDE] or [0])
    if left_mean > right_mean:
        v = -v

    # How well does the learned axis agree with the hand labels?
    idx = {"left": -2, "lean-left": -1, "center": 0, "lean-right": 1, "right": 2}
    hand = np.array([idx.get(lean_of.get(m), 0) for m in names], float)
    agreement = float(np.corrcoef(v, hand)[0, 1]) if hand.std() > 0 else 0.0

    return {
        "runs": state["runs"],
        "stories_observed": int(state["story_total"]),
        "agreement_with_hand_labels": round(agreement, 3),
        "confident": bool(state["story_total"] >= 400 and abs(agreement) > 0.3),
        "scores": {m: round(float(v[i]), 3) for i, m in enumerate(names)},
    }
