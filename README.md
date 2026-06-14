# Parallel Frontier — aggregation pipeline

**One store, one digest, rare interrupts.** A small, pluggable pipeline that
collects China-AI regulatory, research, and discourse signals into a single
store and emails exactly one daily digest — with rare, alert-only interrupts for
regulatory docket changes. No dashboard, one email a day, by design.

See [SPEC.md](SPEC.md) for the full design and rationale.

## How it works

```
collectors/ ── fetch ──▶ SQLite/libSQL store ──▶ digest (one email/day)
  rss, cac, tc260,          items + item_versions      ⚡ alert on docket diff
  arxiv_labs, github_rel,   + source_runs (heartbeat)
  miit                      content-hash dedupe
```

- **Collectors** each expose `fetch() -> list[Item]`; `run()` upserts and records
  a per-source heartbeat. Failures are isolated — one bad source never sinks a run.
- **Store** ([pipeline/store.py](pipeline/store.py)) separates item *identity*
  (`items`) from *version* (`item_versions`) so docket change-detection always has
  a prior state to diff. Local dev uses stdlib `sqlite3`; production uses Turso
  (libSQL) when `TURSO_DATABASE_URL` is set.
- **Tiering** happens in the collectors (rules-first): `alert` / `digest` / `archive`.
- **Digest** ([pipeline/digest.py](pipeline/digest.py)) renders Docket / Regulation /
  Research / Discourse / Rivals + a per-source heartbeat footer.

## Repo layout

```
collectors/   base, rss, docket, listing, cac, tc260, arxiv_labs, github_rel, miit
pipeline/     store, normalize, digest, run
config.yaml   all sources, keywords, arXiv net + roster, recipients
.github/workflows/pipeline.yml   one workflow, two crons, serialized
```

## Local development

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt

# Render the digest from a local SQLite DB without sending:
PF_DB_PATH=/tmp/pf.db ./.venv/bin/python -m pipeline.run --dry-run

# Other modes:
python -m pipeline.run                # full: collect all + digest + send
python -m pipeline.run --alerts-only  # docket sources only; alert on change
python -m pipeline.run --collect-only # collect, no digest
```

With no `RESEND_API_KEY`, sending is skipped and the email is printed instead.
Without `TURSO_DATABASE_URL`, the store is a local file (`$PF_DB_PATH` or
`data/items.db`).

## Going live (GitHub Actions)

The workflow runs on two UTC crons through one serialized entrypoint: a daily
full digest (06:00) and a 4-hourly docket poll (alerts only). Set these repo
secrets:

| Secret | Purpose |
|---|---|
| `TURSO_DATABASE_URL` | production libSQL store URL |
| `TURSO_AUTH_TOKEN` | Turso auth token |
| `RESEND_API_KEY` | digest/alert delivery |
| `GITHUB_TOKEN` | built-in; raises `github_rel` to 5000 req/hr |

Then set `digest.sender` in [config.yaml](config.yaml) to a Resend-verified
domain and `digest.recipients` to your address.

> GitHub cron is UTC-only (no DST) and best-effort; it disables crons after 60
> days with no commits. See SPEC for the timezone/DST notes.

## Deferred sources

Documented inline in [config.yaml](config.yaml), per the SPEC rule *"no new
source until it has fired ≥3 useful items"*:

- **MIIT** — listing pages are JS-rendered SPAs; collector is ready, page needs
  the backing AJAX endpoint (browser devtools).
- **机器之心** — no working public RSS feed.
- **GeopoliTech** — feed valid but dormant (0 entries).

## Status

v1 complete and live-verified: TC260/CAC docket alerts, 6 RSS feeds, arXiv
net+roster, GitHub releases — all flowing into the store and digest. LLM
enrichment (title translation, summaries) is v2.
