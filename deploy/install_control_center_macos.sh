#!/bin/zsh
# Additive, local installation. Does not stop or alter the baseline paper service.
set -euo pipefail
umask 077
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$(command -v python3)"
LABEL="com.harpcity.traderbrain.control"
STATE="$HOME/.local/state/trader-brain/control-center"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
"$PYTHON" -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
mkdir -p "$STATE" "$HOME/Library/LaunchAgents"
chmod 700 "$STATE"
cd "$REPO"
"$PYTHON" -B -m apps.control_center check
# Running tests and local schema checks never uses broker credentials or sends email.
PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -m unittest discover -s apps/control_center/tests -v
"$PYTHON" - "$REPO" "$STATE" "$PLIST" <<'PY'
import pathlib, plistlib, sys
repo,state,plist=map(pathlib.Path,sys.argv[1:])
content={"Label":"com.harpcity.traderbrain.control",
 "ProgramArguments":[sys.executable,"-B","-m","apps.control_center","serve"],
 "WorkingDirectory":str(repo),"RunAtLoad":True,"KeepAlive":True,"ThrottleInterval":30,
 "StandardOutPath":str(state/"service.out.log"),"StandardErrorPath":str(state/"service.err.log")}
plist.write_bytes(plistlib.dumps(content));plist.chmod(0o600)
PY
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
sleep 3
launchctl print "gui/$(id -u)/$LABEL" | grep -E 'state =|pid =|last exit code'
echo "Control center requested at http://127.0.0.1:8765 (this Mac only)."
echo "Baseline paper service and credentials were not changed. New app entries default to paused."
echo "Phone access requires private HTTPS; no port forwarding or public tunnel was enabled."
echo "Verify the service log before claiming deployment: $STATE/service.out.log"
