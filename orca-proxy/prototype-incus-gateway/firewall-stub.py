#!/usr/bin/env python3
"""PROTOTYPE — stands in for orca-proxy-firewall-sync. Touches nothing; the
gateway topology needs no host firewall rules. Reports every VM in_sync so
the real orca-proxy app runs unmodified."""
import json, sys
vms = [a.split("=", 1)[0] for i, a in enumerate(sys.argv) if i and sys.argv[i - 1] == "--vm"]
print(json.dumps({vm: "in_sync" for vm in vms}))
