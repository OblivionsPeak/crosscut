# Crosscut

One story, every side. A free cross-spectrum news reader that runs entirely on
GitHub Pages and GitHub Actions — no server, no API keys, no subscription.

It pulls public RSS from ~50 US outlets, groups articles covering the same
story, and shows how that coverage is distributed left to right. A **blindspot**
is a story one side is largely not reporting.

## How it runs

GitHub Pages is static and browsers can't fetch RSS directly (CORS), so all the
work happens at build time:

```
Actions cron (every 2h)
   → build.py fetches ~50 feeds in parallel
   → clusters headlines into stories
   → writes data/stories.json
   → uploads the whole site as a Pages artifact
   → deploy-pages publishes it
```

`data/` is gitignored. It's generated fresh each run and published as an
artifact, so the repo never accumulates a commit of JSON every two hours.

Run it locally:

```bash
pip install -r requirements.txt
python build.py
python -m http.server 4791     # then open http://localhost:4791
```

## Clustering

Headlines are tokenised into words plus bigrams, weighted by TF-IDF, and grouped
greedily against running centroids. Two things stop it misbehaving:

- **A rare-term guard.** Two headlines must share at least one distinctive term
  before they can join, so unrelated stories don't merge on common vocabulary.
- **A second merge pass.** The greedy first pass is order-dependent, so a terse
  headline (*"Dolly Parton dead at 80"*) and a descriptive one (*"Dolly Parton,
  country music legend, dies at 80"*) can land in separate clusters. Left alone
  that **fabricates blindspots** — half the coverage sits in a cluster the other
  half is missing from. The merge pass finds candidate pairs through an inverted
  index on distinctive terms and unions them.

That second pass is not cosmetic. Without it a real run split Dolly Parton's
death into two clusters and flagged a phantom "left blindspot" on coverage that
21 left-leaning outlets were running.

## The lean ratings — read this

Ground News doesn't rate outlets itself; it averages **AllSides**, **Ad Fontes
Media** and **Media Bias/Fact Check**. All three license their data
commercially, so this project can't use them.

The `lean` values in `feeds.json` are instead a **hand-curated placement** of each
outlet on a US left/right axis, based on commonly cited positions in public
media-bias work. They are not a licensed dataset and carry no methodology behind
them beyond that. They apply to the **outlet**, never to an individual article.

Disagree with one? Edit `feeds.json`. That file is the entire configuration —
outlets, feed URLs and leans.

## Feeds

51 outlets: 8 left, 13 lean-left, 11 center, 9 lean-right, 10 right.

Six outlets are reached through Google News site-search rather than a native
feed, because AP, Reuters, USA Today, The Epoch Times and Townhall have all
retired or locked down their public RSS. Those entries are marked
`"via": "google-news"`, and the build strips the `" - Outlet"` suffix Google
appends to every headline.

MSNBC is deliberately absent — it has no working public feed and no Google News
coverage under a site query, and its reporting largely mirrors NBC.

If a feed dies, the build reports it and the site shows a health line naming the
missing outlets, rather than silently pretending the spectrum is still balanced.

## What improves over time

Actions runs are stateless, so anything that learns needs a store. State lives
on an orphan `state` branch that is force-pushed each run — one commit deep, so
it persists indefinitely without growing history. It's written *after* a
successful build, so a failed run can't wipe what's been learned.

**Feed self-healing.** Every feed's success rate is tracked. After 2 consecutive
failures a feed is rewritten to its Google News site-search fallback; after 8 it
is disabled and reported. A healed feed's original URL is re-tested every 60
runs in case the outlet fixes it. Washington Times silently vanished from one
deploy run — this makes that self-correcting.

**Blindspots normalized by baseline.** The first version just counted articles,
which was wrong: NPR's feed carries 10 items and CNN's carries 69, so
low-volume outlets looked absent constantly and generated false blindspots. Each
outlet now has a learned `coverage_ewma` — the share of stories it typically
appears in. A side is flagged only when its participation falls materially below
*its own* baseline, and the UI shows observed-vs-expected on hover rather than
asking for blind trust.

**Stable story identity.** Stories keep an id across runs, matched on
fingerprint overlap of their distinctive terms. That yields story age and the
pickup gap — *"Left covered this 9h before the right"* — which only exists
because first-seen times survive between builds.

**Learned co-coverage axis.** An outlet × outlet co-coverage matrix accumulates
every run. Classical MDS over it (double-centred, so the dominant "how much does
this outlet publish" component drops out) gives a 1-D axis of which outlets pick
the same stories. The hand labels are used *only* to orient the sign, never as
input values.

Two honest caveats on that last one:

- It measures **story-selection similarity**, not political bias. Those
  correlate, but the axis could as easily be tracking topic mix or newsroom
  size. The UI says so.
- **It is not yet working.** After 93 stories, correlation with the hand labels
  is r ≈ 0.13 — very weak. It's gated behind a confidence check (400+ stories
  and |r| > 0.3) and reports itself as unconfident until then. Whether it
  converges to something meaningful over weeks is an open empirical question,
  not a promise.

## Limits

- **Headlines, summaries and links only.** Full article text is never copied.
- **Clustering is imperfect.** Coverage of one event can still land in two
  groups. Click through before trusting a blindspot.
- **Scheduled workflows get auto-disabled** after 60 days of repo inactivity,
  and GitHub delays cron runs under load. `workflow_dispatch` is enabled so you
  can force a refresh by hand.
- Only US outlets on a US left/right axis. It says nothing about factual
  accuracy — a lean is not a quality score.
