"""Crosscut build step.

Pulls every feed in feeds.json, groups articles that are covering the same
story, works out how that coverage is distributed across the political
spectrum, and writes a single static data/stories.json for the site to read.

Runs in GitHub Actions on a cron. No API keys, no paid services, no database.
"""
import concurrent.futures as cf
import html
import json
import math
import re
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser

ROOT = Path(__file__).parent
UA = {"User-Agent": "Mozilla/5.0 (compatible; Crosscut/0.1; +https://github.com/OblivionsPeak/crosscut)"}

MAX_AGE_HOURS = 36      # ignore anything staler than this
SIM_THRESHOLD = 0.32    # cosine similarity to join a cluster
MIN_SHARED_RARE = 1     # clusters must share at least one distinctive term
MIN_CLUSTER = 2         # a "story" needs at least this many outlets

LEANS = ["left", "lean-left", "center", "lean-right", "right"]

STOP = set("""
a an the and or but if then than that this these those of in on at to for from by with about
as is are was were be been being it its it's he she they them his her their we you i us our your
has have had do does did will would can could should may might must not no nor so such
after before during over under up down out off again more most other some any each new news says
say said report reports reported new latest live update updates video watch photos opinion
""".split())


# --------------------------------------------------------------- fetching

def fetch(outlet):
    """Return (outlet, entries, error). Never raises."""
    try:
        req = urllib.request.Request(outlet["url"], headers=UA)
        raw = urllib.request.urlopen(req, timeout=25).read()
        parsed = feedparser.parse(raw)
        if not parsed.entries:
            return outlet, [], "feed parsed but contained no entries"
        return outlet, parsed.entries, None
    except Exception as exc:
        return outlet, [], f"{type(exc).__name__}: {exc}"[:160]


def clean_title(title, outlet_name, via):
    t = html.unescape(title or "").strip()
    # Google News appends " - Outlet Name" to every headline.
    if via == "google-news":
        t = re.sub(r"\s+-\s+[^-]{2,40}$", "", t).strip()
    return re.sub(r"\s+", " ", t)


def entry_time(entry):
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                return datetime.fromtimestamp(time.mktime(st), tz=timezone.utc)
            except Exception:
                pass
    return None


def summarise(entry):
    raw = entry.get("summary") or entry.get("description") or ""
    text = re.sub(r"<[^>]+>", " ", html.unescape(raw))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:280]


def collect(outlets):
    articles, failures = [], []
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)

    with cf.ThreadPoolExecutor(max_workers=16) as pool:
        for outlet, entries, err in pool.map(fetch, outlets):
            if err:
                failures.append({"outlet": outlet["name"], "error": err})
                continue
            seen = set()
            for e in entries:
                title = clean_title(e.get("title", ""), outlet["name"], outlet.get("via"))
                link = e.get("link") or ""
                if not title or not link or title.lower() in seen:
                    continue
                seen.add(title.lower())
                when = entry_time(e)
                if when and when < cutoff:
                    continue
                articles.append({
                    "title": title,
                    "url": link,
                    "outlet": outlet["name"],
                    "lean": outlet["lean"],
                    "published": when.isoformat() if when else None,
                    "summary": summarise(e),
                })
    return articles, failures


# ------------------------------------------------------------- clustering

def tokenise(title):
    words = [w for w in re.findall(r"[a-z0-9']+", title.lower()) if len(w) > 2 and w not in STOP]
    grams = list(words)
    grams += [f"{a}_{b}" for a, b in zip(words, words[1:])]   # bigrams carry the entities
    return grams


def build_vectors(articles):
    docs = [tokenise(a["title"]) for a in articles]
    df = Counter()
    for d in docs:
        df.update(set(d))
    n = max(1, len(docs))
    idf = {t: math.log(n / (1 + c)) + 1.0 for t, c in df.items()}

    vectors = []
    for d in docs:
        tf = Counter(d)
        vec = {t: (1 + math.log(c)) * idf.get(t, 1.0) for t, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        vectors.append({t: v / norm for t, v in vec.items()})
    return vectors, idf


def cosine(a, b):
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(t, 0.0) for t, v in a.items())


def cluster(articles, vectors, idf):
    """Greedy single-pass clustering against running centroids.

    Cheap, order-dependent, and good enough for a few thousand headlines a day.
    The rare-term guard stops two unrelated stories merging just because they
    share common vocabulary.
    """
    order = sorted(range(len(articles)), key=lambda i: articles[i]["published"] or "", reverse=True)
    centroids, members = [], []

    def rare_terms(vec):
        return {t for t in vec if idf.get(t, 0) > 2.0}

    for i in order:
        vec = vectors[i]
        rare = rare_terms(vec)
        best, best_score = -1, 0.0
        for ci, cen in enumerate(centroids):
            score = cosine(vec, cen)
            if score < SIM_THRESHOLD or score <= best_score:
                continue
            if len(rare & rare_terms(cen)) < MIN_SHARED_RARE:
                continue
            best, best_score = ci, score
        if best < 0:
            centroids.append(dict(vec))
            members.append([i])
        else:
            members[best].append(i)
            cen, k = centroids[best], len(members[best])
            for t, v in vec.items():                      # running mean
                cen[t] = cen.get(t, 0.0) + (v - cen.get(t, 0.0)) / k

    return merge_pass(centroids, members, idf)


def merge_pass(centroids, members, idf):
    """Second pass to repair splits left by the order-dependent first pass.

    A terse headline ("Dolly Parton dead at 80") and a descriptive one ("Dolly
    Parton, country music legend, dies at 80") share few terms, so they can land
    in separate clusters. Left alone that fabricates blindspots: half the
    coverage ends up in a cluster the other half is missing from. Candidate
    pairs are found through an inverted index on distinctive terms, so this
    stays cheap even with a few thousand headlines.
    """
    parent = list(range(len(centroids)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    rare = [{t for t, v in c.items() if idf.get(t, 0) > 2.6 and v > 0.10} for c in centroids]
    index = defaultdict(list)
    for ci, terms in enumerate(rare):
        for t in terms:
            index[t].append(ci)

    checked = set()
    for term, cids in index.items():
        if len(cids) > 40:            # too common to be distinctive; skip
            continue
        for a in range(len(cids)):
            for b in range(a + 1, len(cids)):
                i, j = cids[a], cids[b]
                if (i, j) in checked:
                    continue
                checked.add((i, j))
                shared = rare[i] & rare[j]
                if len(shared) < 2:
                    continue
                if cosine(centroids[i], centroids[j]) >= 0.22:
                    union(i, j)

    merged = defaultdict(list)
    for ci, idxs in enumerate(members):
        merged[find(ci)].extend(idxs)
    return list(merged.values())


# ---------------------------------------------------------------- scoring

def score_story(arts):
    counts = Counter(a["lean"] for a in arts)
    total = len(arts)
    left = counts["left"] + counts["lean-left"]
    right = counts["right"] + counts["lean-right"]
    sided = left + right

    blindspot = None
    if total >= 4 and sided >= 3:
        if right == 0 or (right / sided) <= 0.12:
            blindspot = "right"     # the right is not covering this
        elif left == 0 or (left / sided) <= 0.12:
            blindspot = "left"
    return {
        "leans": {k: counts.get(k, 0) for k in LEANS},
        "total": total,
        "outlets": len({a["outlet"] for a in arts}),
        "blindspot": blindspot,
    }


def representative(arts):
    """Prefer a centre/wire headline; they tend to be the least loaded."""
    rank = {"center": 0, "lean-left": 1, "lean-right": 1, "left": 2, "right": 2}
    return sorted(arts, key=lambda a: (rank.get(a["lean"], 3), -len(a["title"])))[0]["title"]


# ------------------------------------------------------------------- main

def main():
    cfg = json.loads((ROOT / "feeds.json").read_text("utf-8"))
    outlets = cfg["outlets"]
    print(f"fetching {len(outlets)} feeds...")

    articles, failures = collect(outlets)
    print(f"  {len(articles)} articles, {len(failures)} feed failures")
    if not articles:
        raise SystemExit("no articles fetched - refusing to write an empty dataset")

    vectors, idf = build_vectors(articles)
    groups = cluster(articles, vectors, idf)
    print(f"  {len(groups)} groups")

    stories = []
    for gi, idxs in enumerate(groups):
        arts = [articles[i] for i in idxs]
        if len(arts) < MIN_CLUSTER:
            continue
        if len({a["outlet"] for a in arts}) < MIN_CLUSTER:
            continue                                   # same outlet repeating itself
        s = score_story(arts)
        arts.sort(key=lambda a: (LEANS.index(a["lean"]), a["outlet"]))
        stories.append({
            "id": f"s{gi}",
            "title": representative(arts),
            **s,
            "articles": arts,
        })

    stories.sort(key=lambda s: (-s["outlets"], -s["total"]))
    ungrouped = sum(1 for g in groups if len(g) < MIN_CLUSTER)

    out = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "article_count": len(articles),
        "outlet_count": len({a["outlet"] for a in articles}),
        "story_count": len(stories),
        "single_source_count": ungrouped,
        "leans": LEANS,
        "outlets": [{"name": o["name"], "lean": o["lean"]} for o in outlets],
        "failures": failures,
        "stories": stories[:120],
    }
    dest = ROOT / "data"
    dest.mkdir(exist_ok=True)
    (dest / "stories.json").write_text(json.dumps(out, ensure_ascii=False), "utf-8")

    blind = sum(1 for s in stories if s["blindspot"])
    print(f"  wrote {len(out['stories'])} stories ({blind} with a blindspot)")
    for f in failures:
        print(f"  FEED FAILED: {f['outlet']}: {f['error']}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
