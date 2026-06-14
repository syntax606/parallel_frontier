# Parallel Frontier — Aggregation System Spec (v1.1)
**Design principle: one store, one digest, rare interrupts.**
Everything flows into a single database; you receive exactly one daily email; only regulatory-docket changes are allowed to interrupt you outside it. No frankenemailparade.

> **Changes from v1** (folded in after design review):
> 1. **One sequential workflow**, not two crons — kills concurrent-writer conflicts on the store.
> 2. **Store moves off git-commit persistence** to managed libSQL (Turso) — no binary-merge conflicts, no repo bloat. Git-commit fallback documented but discouraged.
> 3. **Schema separates identity from version** so docket change-detection actually has a previous state to diff against.
> 4. **Content is normalized before hashing** — the alert-rate goal is enforced by *what you hash*, not post-hoc tuning.
> 5. **Per-source heartbeat in the digest footer** — the one observability piece v1 was missing. Not a dashboard.
> 6. **Timezone behavior written down** (Actions cron is UTC-only, no DST).
> 7. **arXiv affiliation filtering → author allowlist** for v1 (arXiv metadata doesn't reliably expose affiliations).
> 8. **Scrapers get timeouts, retries, politeness** and fail loud via the heartbeat.

---

## Architecture

```
 ┌─────────────── COLLECTORS (pluggable, one module per source) ───────────────┐
 │  cac.py   tc260.py   miit.py   arxiv.py   rss.py   github_releases.py        │
 └──────────────────────────────┬──────────────────────────────────────────────┘
                                ▼
                       ┌─────────────────┐
                       │  libSQL store    │   items + item_versions
                       │  (Turso free     │   tier; normalized-content hash
                       │   tier, off-git) │   full row history = future dataset
                       └────────┬─────────┘
                                ▼
                       ┌─────────────────┐
                       │  CLASSIFIER      │   rules first; LLM pass optional later
                       │  tier: ALERT /   │
                       │  DIGEST / ARCHIVE│
                       └────────┬─────────┘
                  ┌─────────────┴──────────────┐
                  ▼                            ▼
        ┌──────────────────┐         ┌────────────────────┐
        │ ALERT (rare)      │         │ DAILY DIGEST (1/day)│
        │ immediate email/  │         │ one email, ~07:30 UK│
        │ Telegram: CAC or  │         │ grouped by category │
        │ TC260 docket diff │         │ + heartbeat footer  │
        └──────────────────┘         └────────────────────┘
```

**Runner:** GitHub Actions cron (free, no server to maintain). **One workflow, run in sequence** so the store only ever has a single writer:

- `0 6 * * *` — `pipeline.yml`: collect (all sources) → classify → diff/alert → assemble digest → send.
- Alert-tier collectors (CAC/TC260/MIIT) can additionally run on a tighter beat **only if** they write through the same serialized entrypoint. If you want 4-hourly docket polling, add `0 */4 * * *` to the *same* `pipeline.yml` with a `--collectors=cac,tc260,miit --alerts-only` flag — never a second workflow racing the first on the store.

> **Why one workflow:** two crons (`*/4` collect + daily digest) will eventually overlap. With a shared store both runs write and the loser's write is lost or conflicts. Serializing removes the entire concurrency failure class for zero cost.

> **Cron/timezone reality:** GitHub Actions cron is **UTC-only and does not observe DST**, and scheduled runs are best-effort (often delayed 5–30+ min). `0 6 * * *` = 07:00 BST in summer, 06:00 GMT in winter — it will drift an hour twice a year. This is fine for a digest. Pick the UTC time that lands near 07:30 UK in the season you care about and accept the drift, **or** have the job compute `Europe/London` locally and no-op if it's the wrong hour. Decision for v1: **fixed UTC, accept drift.** Also note: GitHub disables crons after **60 days with no commits** — a periodic no-op commit or a manual nudge keeps it alive.

**Repo layout:**

```
parallel-frontier-pipeline/
├── collectors/
│   ├── base.py          # Collector ABC: fetch() -> list[Item]; built-in timeout/retry
│   ├── cac.py           # CAC announcement + comment-period pages
│   ├── tc260.py         # TC260 standards/notices pages
│   ├── miit.py          # MIIT AI-relevant announcement feeds
│   ├── arxiv_labs.py    # arXiv API: broad category net + keyword gate + pinned lab/org roster
│   ├── rss.py           # generic RSS: lab blogs, 机器之心, 量子位, rival newsletters
│   └── github_rel.py    # lab GitHub orgs: releases + model-card commits
├── pipeline/
│   ├── store.py         # libSQL client; items + item_versions; normalized-hash dedupe
│   ├── classify.py      # tier rules (enums, not free text)
│   ├── normalize.py     # strip boilerplate before hashing  ← new
│   ├── heartbeat.py     # per-source run status            ← new
│   └── digest.py        # render daily email (HTML, grouped + heartbeat footer)
├── config.yaml          # all sources, keywords, recipients, arXiv net + roster in ONE file
└── .github/workflows/pipeline.yml
```

> Note: `data/items.db` is **gone**. The store lives in Turso (libSQL) and is reached via `TURSO_DATABASE_URL` + `TURSO_AUTH_TOKEN` in GitHub secrets. See *Persistence* below.

## Persistence

The store is **managed libSQL (Turso free tier)** — SQLite semantics, lives outside git.

> **Why not commit `items.db` back to the repo (v1's plan):** (1) a binary SQLite file **cannot be auto-merged** — any overlapping run produces a hard conflict on your crown-jewel data in a headless cron; (2) a binary that changes every run bloats git history with undiffable blobs (hundreds of MB within months). The dataset you want is the *rows*, not file snapshots.

**Alternatives, in order of preference:**
1. **Turso (libSQL)** — drop-in SQLite, generous free tier, off-git. *(chosen for v1)*
2. **Cloudflare D1** — equivalent; pick if already in the CF ecosystem.
3. **Git-commit `items.db`** — *only* if you refuse a managed dep. Then: single serialized writer (already enforced), `git pull --rebase` before push, and a periodic `sqlite3 items.db .dump > items.sql` so history is at least diffable. Discouraged.

History/“future dataset” is preserved by the `item_versions` table (below), not by file snapshots — export to Parquet/CSV any time with one query.

## Data model

```sql
-- Identity: one row per logical item (stable URL).
CREATE TABLE items (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,        -- 'cac', 'tc260', 'arxiv', 'rss:jiqizhixin', ...
  url TEXT UNIQUE NOT NULL,
  title TEXT,                  -- original language
  title_en TEXT,              -- filled by LLM pass later; empty in v1
  published_at TEXT,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  current_hash TEXT,          -- normalized-content hash of latest version
  tier TEXT NOT NULL,         -- enum: 'alert' | 'digest' | 'archive'
  category TEXT,              -- enum: 'regulation' | 'research' | 'discourse' | 'rivals'
  used_in_issue INTEGER       -- editorial bookkeeping, null until used
);

-- Version history: one row per (url, content_hash) ever seen.
-- This is what makes docket change-detection possible AND is the "future dataset".
CREATE TABLE item_versions (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(id),
  content_hash TEXT NOT NULL,   -- hash of NORMALIZED content (boilerplate stripped)
  raw_excerpt TEXT,             -- first ~2000 chars of the raw page at this version
  fetched_at TEXT NOT NULL,
  UNIQUE(item_id, content_hash)
);

-- Heartbeat: per-source result of each run. Powers the digest footer.
CREATE TABLE source_runs (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  run_at TEXT NOT NULL,
  ok INTEGER NOT NULL,          -- 1 success, 0 failure
  item_count INTEGER,           -- items returned (0 on a healthy-but-quiet source)
  error TEXT                     -- exception summary on failure
);
```

> **Why two tables:** v1 had `url UNIQUE` *and* used `content_hash` for change detection — these fight. When a watched page changes, the `INSERT` collides on `url` and you either no-op (miss the change) or overwrite the old hash (destroy the state you needed to diff). Splitting identity (`items`) from version (`item_versions`) means the diff reads the previous hash *before* writing the new one.

> `tier`/`category` are constrained to enums **in code** (and ideally `CHECK` constraints) to prevent `'regulation'` vs `'Regulation'` drift that would silently break digest grouping.

## Change detection & the alert rate

The CAC/TC260 alert is a **diff**, so it must be robust to noise:

1. Fetch page → extract the **main content region only** (drop nav, footer, visitor counters, “最后更新” timestamps, reordered link lists). This is `normalize.py`.
2. Hash the normalized content → `content_hash`.
3. Look up the item by `url`. If `content_hash` differs from `items.current_hash` → **ALERT**, then insert a new `item_versions` row and update `current_hash`/`last_seen`.

> **The alert-rate goal is enforced by what you hash, not by tuning later.** v1’s `ALERT < 2/week` is a goal, not a mechanism — a naive whole-page diff trips on nav tweaks, footer dates, and reordered lists, producing daily false alerts you’ll learn to ignore. Hashing the normalized main-content region is what makes the goal achievable. **Hard rule unchanged: ALERT tier must average < 2/week** — but now there’s a lever that delivers it.

## Classification rules (v1 — no LLM yet)

| Rule | Tier |
|---|---|
| Normalized-content change on CAC comment-period or TC260 draft pages | **ALERT** |
| Any CAC/TC260/MIIT item mentioning 人工智能 / 算法 / 大模型 / 生成式 | digest:regulation |
| arXiv item from the **broad category net** matching topic keywords | digest:research |
| arXiv item matching the **lab/org roster** (author or alias) | digest:research (pinned, always shown) |
| arXiv item in the net but no keyword/roster hit | archive (searchable, never emailed) |
| 机器之心 / 量子位 / Zhihu-sourced | digest:discourse |
| Rival newsletters (ChinAI, Geopolitechs, Concordia…) | digest:rivals |
| Everything else matching keywords | archive (searchable, never emailed) |

### arXiv: broad net + pinned lab/org roster

A daily **broad net** feeds the digest, with priority labs/orgs always caught on top. Three layers:

1. **Query (the net):** category-scoped over a rolling 24h window —
   `cat:cs.AI OR cat:cs.CL OR cat:cs.LG OR cat:cs.CV OR stat.ML` (tune in `config.yaml`).
   This is the broad net; ~100–300 items/day.
2. **Keyword gate (what reaches Research):** of the net, only items whose **title/abstract** hit
   `config.yaml: arxiv.keywords` (e.g. 大模型/LLM, agent, RL, MoE, long-context, …) appear in the
   digest. Keeps Research to ~10–30 lines/day. Net items with **no** keyword hit go to `archive`
   (searchable, never emailed) — nothing is lost.
3. **Lab/org roster (always caught):** `config.yaml: arxiv.roster` lists priority labs/orgs, each as
   `{ authors: [...], aliases: [...] }`. A paper is a **roster hit** if it matches a roster author
   name **or** an alias string (lab name, model-family name) in title/abstract/comments. Roster hits
   are **pinned to the top of Research and always shown in full, regardless of the keyword gate.**

> **Why not affiliation, and why roster ≠ plain author list.** arXiv’s API does not reliably expose
> affiliations (sparse/inconsistent field), so affiliation-filtering won’t work. And a *bare* author
> allowlist is noisy here: romanized Chinese names collide massively (`au:"Wei Wang"` etc.). The
> roster mitigates this by pairing author names with alias strings and only firing the pin on a
> name **or** alias match — and roster hits are surfaced, not used to *exclude*, so a false positive
> costs one extra line, not a missed paper. Robust affiliation extraction (PDF/GROBID) is v2.

## Collector robustness (base.py)

Every collector inherits, so failures are loud and runs are polite:

- **Per-request timeout** (e.g. 20s) — a hung government site must never hang the runner.
- **Retry with backoff** (e.g. 3 tries, exponential) on transient errors.
- **Politeness**: a small inter-request delay; respect obvious rate limits. Government sites and WAFs will tarpit aggressive clients (and Actions runners are datacenter IPs that some sites block outright).
- **Never swallow failure silently.** A collector that throws records `source_runs.ok = 0` with the error; the pipeline continues with other sources and the failure surfaces in the heartbeat footer.

## The daily digest (the only email)

One HTML email, ~07:30 UK:

1. **⚡ Docket** — anything alert-tier from the last 24h (usually empty)
2. **Regulation** — new official items, original title + machine-translated gloss
3. **Research** — broad arXiv net, keyword-gated, one line each. **Roster labs/orgs pinned at top, always shown in full**; keyword hits below. Footer note: "+N more in net → archive."
4. **Discourse** — media/Zhihu items
5. **Rivals published** — titles only, so you never duplicate them unknowingly
6. **Heartbeat footer** — per-source last run: `cac ✓ (12) · tc260 ✓ (3) · miit ✗ error · arxiv ✓ (0) …` plus “open capture file” link.

> **Why the heartbeat (the observability v1 forgot):** the system’s whole value is catching rare regulatory diffs on fragile, WAF’d pages via best-effort cron. With no failure signal, a broken `cac.py` returning `[]` reads as “quiet week,” not “blind for a month” — the exact failure mode that matters most. The heartbeat is **not a dashboard** (the anti-goal stands): it’s per-source last-success + count, surfaced in the one email you already read. A `✓ (0)` (healthy but quiet) looks different from `✗ error` (broken).

**Delivery:** SMTP via a free transactional tier — **Resend or Postmark** (API key in GitHub secrets). Gmail SMTP increasingly blocks unattended app logins; avoid for cron. **Send failure falls through to the heartbeat** (logged to `source_runs` as a `digest` pseudo-source) so a silently undelivered digest is detectable on the next run. Optional: mirror alert-tier only to Telegram for true interrupts.

## What stays manual (by design)

- **WeChat 公众号** — no reliable API; 2×/week skim, drop links into the capture file by hand
- **Zhihu 如何评价 threads** — judgment-driven; manual within 48h of major releases
- **Editorial selection** — the pipeline gathers and sorts; it never decides what matters

## Build order (one weekend)

1. **Sat AM:** repo + `store.py` (Turso) + `rss.py` (instant value: all RSS unified)
2. **Sat PM:** `cac.py` + `tc260.py` + `normalize.py` with normalized-content diffing — the crown jewel
3. **Sun AM:** `classify.py` + `heartbeat.py` + `digest.py` + the single `pipeline.yml`
4. **Sun PM:** `arxiv_labs.py` (broad net + keyword gate + roster pin) + `github_rel.py`; let it soak for a week before tuning

## v2 (only after ~4 manual issues)

- LLM pass: translate titles, 1-line summaries, suggested category (Claude API, batched, pennies/day)
- Author extraction from arXiv items (PDF/GROBID) → seeds the who's-who tables and enables true affiliation filtering
- Weekly auto-generated "fortnight in review" skeleton as issue starting point

## Anti-goals (write these down and obey them)

- No dashboard. Dashboards are where attention goes to die; the digest is the interface. *(The heartbeat footer is part of the digest, not a dashboard.)*
- No more than ONE scheduled email per day, ever. *(The heartbeat rides inside it.)*
- No new source unless it has fired ≥3 useful items in the capture file manually first.
- No platform. This is plumbing for one analyst, not a product (yet).
- No second workflow that races the store. One serialized writer, always.
