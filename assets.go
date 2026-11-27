package jettyvm

import "embed"

// Embedded runtime assets shipped inside the single static jetty binary.
//
//go:embed VERSION
//go:embed resources/quick-add-catalog.json
//go:embed deploy/gateway/jetty-gateway-up
//go:embed deploy/gateway/jetty-gateway.service
//go:embed deploy/gateway/jetty-gw-agent.service
//go:embed gateway-agent/pyproject.toml
//go:embed gateway-agent/run_addon.py
//go:embed gateway-agent/src/jetty_gw_agent/__init__.py
//go:embed gateway-agent/src/jetty_gw_agent/__main__.py
//go:embed gateway-agent/src/jetty_gw_agent/addon.py
//go:embed gateway-agent/src/jetty_gw_agent/version.py
//go:embed gateway-agent/src/jetty_gw_agent/proto/__init__.py
//go:embed gateway-agent/src/jetty_gw_agent/proto/jetty/__init__.py
//go:embed gateway-agent/src/jetty_gw_agent/proto/jetty/v1/__init__.py
//go:embed gateway-agent/src/jetty_gw_agent/proto/jetty/v1/jetty_pb2.py
//go:embed gateway-agent/src/jetty_gw_agent/proto/jetty/v1/jetty_pb2_grpc.py
//go:embed orca-ssh-setup/SKILL.md
//go:embed orca-ssh-setup/references/antigravity.md
var Assets embed.FS
