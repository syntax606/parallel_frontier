"""Pipeline entrypoint — the single serialized writer (SPEC: one workflow).

Modes:
  (default)      collect ALL sources -> build digest -> send (the 06:00 run)
  --alerts-only  collect only docket sources -> send an immediate alert IF a
                 docket change was detected (the 4-hourly poll). No digest.
  --collect-only collect all, no digest (debugging)
  --dry-run      build the digest but print it instead of sending

Run:  python -m pipeline.run [--alerts-only|--collect-only|--dry-run]

All collectors record their own heartbeat. Digest send success/failure is also
recorded (as the `digest` pseudo-source) so an undelivered email is visible on
the next run.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import yaml

from collectors import arxiv_labs, cac, github_rel, miit, rss, tc260
from collectors.base import Collector
from pipeline.digest import build_alert, build_digest, send_via_resend
from pipeline.store import Store, connect, utc_now

log = logging.getLogger("pipeline.run")

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    path = Path(os.environ.get("PF_CONFIG", REPO_ROOT / "config.yaml"))
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def build_collectors(cfg: dict, *, alerts_only: bool) -> list[Collector]:
    """Assemble collectors. Alert sources always; the rest only on full runs.

    arxiv_labs / github_rel / miit are added here as those modules land.
    """
    collectors: list[Collector] = []
    collectors += cac.collectors_from_config(cfg)
    collectors += tc260.collectors_from_config(cfg)
    collectors += miit.collectors_from_config(cfg)   # alert-capable (listing)
    if not alerts_only:
        collectors += rss.collectors_from_config(cfg)
        collectors += arxiv_labs.collectors_from_config(cfg)
        collectors += github_rel.collectors_from_config(cfg)
    return collectors


def run_collectors(store: Store, collectors: list[Collector]) -> None:
    for c in collectors:
        c.run(store)  # never raises for source errors; records its own heartbeat


def do_alerts_only(store: Store, cfg: dict, collectors: list[Collector]) -> bool:
    """Run alert collectors; send an immediate alert if a docket change appeared.

    Returns True if an alert was sent.
    """
    started = utc_now()
    run_collectors(store, collectors)

    new_alerts = store.alerts_since(started)
    if not new_alerts:
        log.info("docket poll: no changes")
        return False

    alert = build_alert(new_alerts)
    log.info("docket poll: %d change(s) -> sending alert", len(new_alerts))
    _send(store, alert, cfg)
    return True


def do_full(store: Store, cfg: dict, collectors: list[Collector], *, dry_run: bool) -> None:
    run_collectors(store, collectors)
    dcfg = cfg.get("digest") or {}
    digest = build_digest(store, capture_url=dcfg.get("capture_url") or None)

    if dry_run:
        sys.stderr.write(f"[dry-run] subject: {digest.subject}\n")
        print(digest.html)
        return
    _send(store, digest, cfg)


def _send(store: Store, digest, cfg: dict) -> None:
    """Send via the configured provider; record a `digest` heartbeat either way."""
    dcfg = cfg.get("digest") or {}
    recipients = dcfg.get("recipients") or []
    sender = dcfg.get("sender")

    if not os.environ.get("RESEND_API_KEY"):
        sys.stderr.write(
            "[no-send] RESEND_API_KEY unset; printing instead of sending.\n"
            f"subject: {digest.subject}\n"
        )
        print(digest.html)
        return

    try:
        send_via_resend(digest, recipients, sender)
        store.record_run("digest", ok=True, item_count=1)
        log.info("sent: %s", digest.subject)
    except Exception as exc:
        store.record_run("digest", ok=False, error=f"{type(exc).__name__}: {exc}")
        log.error("digest send failed: %s", exc)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parallel Frontier pipeline")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--alerts-only", action="store_true",
                      help="docket sources only; send immediate alert on change")
    mode.add_argument("--collect-only", action="store_true",
                      help="collect all sources, no digest")
    parser.add_argument("--dry-run", action="store_true",
                        help="build the digest but print instead of sending")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config()

    with connect() as store:
        collectors = build_collectors(cfg, alerts_only=args.alerts_only)
        log.info("running %d collectors (%s)", len(collectors),
                 "alerts-only" if args.alerts_only else "full")

        if args.alerts_only:
            do_alerts_only(store, cfg, collectors)
        elif args.collect_only:
            run_collectors(store, collectors)
        else:
            do_full(store, cfg, collectors, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
