"""Entrypoint that bootstraps CA/version from jettyd and execs mitmdump."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from jetty_gw_agent.addon import DEFAULT_CONFDIR, bootstrap_gateway

log = logging.getLogger("jetty.gw_agent")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    confdir = Path(os.environ.get("JETTY_CA_CONFDIR", DEFAULT_CONFDIR))
    try:
        cfg = bootstrap_gateway(confdir=confdir)
    except Exception as exc:
        log.error("gateway bootstrap failed: %s", exc)
        return 1

    listen_host = os.environ.get("JETTY_PROXY_LISTEN_HOST", "0.0.0.0")
    listen_port = os.environ.get("JETTY_PROXY_LISTEN_PORT", "8080")
    addon_script = Path(__file__).resolve().parents[2] / "run_addon.py"
    if not addon_script.is_file():
        addon_script = Path("/opt/jetty-gw-agent/run_addon.py")

    mitmdump = str(Path(sys.executable).with_name("mitmdump"))
    args = [
        mitmdump,
        "--mode",
        "transparent",
        "--listen-host",
        listen_host,
        "--listen-port",
        listen_port,
        "--set",
        f"confdir={confdir}",
        "-s",
        str(addon_script),
    ]
    if cfg.max_intercept_body_bytes > 0:
        os.environ["JETTY_MAX_INTERCEPT_BODY_BYTES"] = str(cfg.max_intercept_body_bytes)
    os.environ["JETTY_SKIP_BOOTSTRAP"] = "1"
    os.execv(mitmdump, args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
