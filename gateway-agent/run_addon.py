"""Script loaded by `mitmdump -s run_addon.py`."""

import os

from jetty_gw_agent.addon import JettyGatewayAddon

_skip = os.environ.get("JETTY_SKIP_BOOTSTRAP") == "1"
_addon = JettyGatewayAddon(skip_bootstrap=_skip)
if os.environ.get("JETTY_MAX_INTERCEPT_BODY_BYTES"):
    _addon._max_body_bytes = int(os.environ["JETTY_MAX_INTERCEPT_BODY_BYTES"])

addons = [_addon]
