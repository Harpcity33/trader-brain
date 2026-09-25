#!/bin/zsh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_DIR="$HOME/.config/trader-brain"
ENV_FILE="$ENV_DIR/paper-options.env"
PLIST_SRC="$REPO_DIR/deploy/com.harpcity.traderbrain.paper-options.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.harpcity.traderbrain.paper-options.plist"

mkdir -p "$ENV_DIR"
chmod 700 "$ENV_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$REPO_DIR/config/paper_options_v1.env.example" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "Created $ENV_FILE. Populate secrets before loading the service."
  echo "No service was started."
  exit 2
fi

if grep -Eq '^(MASSIVE_API_KEY|OPENAI_API_KEY|TB_GMAIL_SENDER|TB_GMAIL_APP_PASSWORD|TB_GMAIL_RECIPIENT)=$' "$ENV_FILE"; then
  echo "Required secrets are still blank in $ENV_FILE. Refusing to start."
  exit 3
fi

if ! grep -q '^TB_REPO_PATH=' "$ENV_FILE" || grep -q '^TB_REPO_PATH=$' "$ENV_FILE"; then
  printf '\nTB_REPO_PATH=%s\n' "$REPO_DIR" >> "$ENV_FILE"
fi

python3 "$REPO_DIR/scripts/paper_options_runtime.py" check

mkdir -p "$HOME/Library/LaunchAgents"
cp "$PLIST_SRC" "$PLIST_DST"

launchctl bootout "gui/$UID" "$PLIST_DST" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$PLIST_DST"
launchctl kickstart -k "gui/$UID/com.harpcity.traderbrain.paper-options"

echo "Trader Brain paper runtime service loaded."
echo "stdout: /tmp/trader-brain-paper-options.out.log"
echo "stderr: /tmp/trader-brain-paper-options.err.log"
