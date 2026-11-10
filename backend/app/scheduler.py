"""Scheduler for a self-hosted install (one computer, docker compose): does what Cloud Scheduler does in
the cloud - calls the app's internal cron endpoints.

  every minute   /internal/cron/webhooks       (Whatnot notifications not processed yet)
  every hour     /internal/cron/whatnot-sync   (orders, shipments, shows, financial report)

Run:  python -m app.scheduler   (env: ERP_APP_URL, ERP_CRON_SECRET)
"""
import logging
import os
import time

import httpx

log = logging.getLogger("app.scheduler")


def call(http: httpx.Client, path: str, timeout: float) -> None:
    r = http.post(path, timeout=timeout)
    log.info("%s -> %s %s", path, r.status_code, r.text[:300])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    url, key = os.environ.get("ERP_APP_URL", "http://app:8080"), os.environ["ERP_CRON_SECRET"]
    first_sync_delay = int(os.environ.get("ERP_FIRST_SYNC_DELAY_SECONDS", "120"))
    next_sync = time.monotonic() + first_sync_delay
    with httpx.Client(base_url=url.rstrip("/") + "/api/v1", headers={"X-Cron-Secret": key}) as http:
        while True:
            try:
                call(http, "/internal/cron/webhooks", 120)
                if time.monotonic() >= next_sync:
                    next_sync = time.monotonic() + 3600
                    call(http, "/internal/cron/whatnot-sync", 1800)
            except Exception as exc:  # app restarting, network down...: try again next minute
                log.warning("scheduler call failed: %s", exc)
            time.sleep(60)


if __name__ == "__main__":
    main()
