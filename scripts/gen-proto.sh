#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
export PATH="/usr/local/go/bin:$HOME/go/bin:$PATH"

rm -rf "$ROOT/gen/go"
mkdir -p "$ROOT/gen/go" "$ROOT/gateway-agent/src/jetty_gw_agent/proto/jetty/v1"

protoc \
  -I "$ROOT/proto" \
  --go_out="$ROOT/gen/go" --go_opt=paths=source_relative \
  --connect-go_out="$ROOT/gen/go" --connect-go_opt=paths=source_relative \
  "$ROOT/proto/jetty/v1/jetty.proto"

uv run --project "$ROOT/gateway-agent" python -m grpc_tools.protoc \
  -I "$ROOT/proto" \
  --python_out="$ROOT/gateway-agent/src/jetty_gw_agent/proto" \
  --grpc_python_out="$ROOT/gateway-agent/src/jetty_gw_agent/proto" \
  "$ROOT/proto/jetty/v1/jetty.proto"

touch "$ROOT/gateway-agent/src/jetty_gw_agent/proto/__init__.py"
touch "$ROOT/gateway-agent/src/jetty_gw_agent/proto/jetty/__init__.py"
touch "$ROOT/gateway-agent/src/jetty_gw_agent/proto/jetty/v1/__init__.py"

# Fix relative import in generated grpc stub for package-qualified import.
"$ROOT/gateway-agent/.venv/bin/python" - <<'PY' "$ROOT/gateway-agent/src/jetty_gw_agent/proto/jetty/v1/jetty_pb2_grpc.py"
from pathlib import Path
import sys
p = Path(sys.argv[1])
text = p.read_text()
text = text.replace(
    "from jetty.v1 import jetty_pb2 as jetty_dot_v1_dot_jetty__pb2",
    "from . import jetty_pb2 as jetty_dot_v1_dot_jetty__pb2",
)
p.write_text(text)
PY
