#!/bin/zsh
# Install only the isolated paper service; no broker or legacy task changes.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$(command -v python3)"
ENV_DIR="$HOME/.config/trader-brain"
ENV_FILE="$ENV_DIR/paper-options.env"
LOG_DIR="$HOME/.local/state/trader-brain"
PLIST="$HOME/Library/LaunchAgents/com.harpcity.traderbrain.paper-options.plist"
SCOPE="gui/$(id -u)"
umask 077
mkdir -p "$ENV_DIR" "$LOG_DIR" "$HOME/Library/LaunchAgents"
chmod 700 "$ENV_DIR" "$LOG_DIR"
if [[ ! -f "$ENV_FILE" ]]; then
  cp "$REPO_DIR/config/paper_options_v1.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "Local settings template created. Populate Massive and Gmail settings locally; no service started."
  exit 2
fi
chmod 600 "$ENV_FILE"
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
"$PYTHON" -B "$REPO_DIR/scripts/paper_options_runtime.py" check
"$PYTHON" -B "$REPO_DIR/scripts/paper_options_runtime.py" doctor
# Build an exact-interpreter plist. Never source the secret file as shell code.
"$PYTHON" - "$REPO_DIR" "$PLIST" "$LOG_DIR" <<'PY'
from pathlib import Path
import plistlib
import sys
repo, destination, logs = map(Path, sys.argv[1:])
data = {
    "Label": "com.harpcity.traderbrain.paper-options",
    "ProgramArguments": [sys.executable, "-B", str(repo / "scripts/paper_options_runtime.py"), "loop"],
    "WorkingDirectory": str(repo),
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 30,
    "StandardOutPath": str(logs / "paper-options.out.log"),
    "StandardErrorPath": str(logs / "paper-options.err.log"),
}
destination.write_bytes(plistlib.dumps(data))
destination.chmod(0o600)
PY
launchctl bootout "$SCOPE" "$PLIST" 2>/dev/null || true
launchctl bootstrap "$SCOPE" "$PLIST"
sleep 2
launchctl print "$SCOPE/com.harpcity.traderbrain.paper-options" | grep -E 'state =|pid =|last exit code'
echo "Paper service installation requested. Runtime-version evidence is in $LOG_DIR/paper-options.out.log."
echo "Do not infer market-data freshness or trading readiness from process presence alone."
