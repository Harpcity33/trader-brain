#!/usr/bin/env bash
# Owner helper: build the Titan full-live release from THIS checkout and install
# it PAUSED on this host. Written by Kiro for the owner to run on their own
# machine.
#
# SAFETY — read this:
#   * This script NEVER activates trading. It contains no `activate` and no
#     `serve` call. The underlying paused installer is itself incapable of
#     loading launchd, starting the runtime, or contacting the broker.
#   * It STOPS after a paused install and prints the read-only readiness steps.
#     Turning trading on is a separate, deliberate command only YOU run later,
#     and only after real P8/P9/P10 evidence passes.
#   * It refuses to run unless prerequisites are present, rather than guessing.
#
# Usage:
#   scripts/owner_build_and_install_paused.sh \
#       --sdk-venv /path/to/ibapi-10.50.2-venv \
#       [--root "$HOME/Library/Application Support/Titan Momentum/full-live"] \
#       [--config config/full_live_ibkr.json] \
#       [--python python3]
#
# Run it from the root of a CLEAN, MERGED checkout of trader-brain.

set -euo pipefail

# ---- defaults ----------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_ROOT="${HOME}/Library/Application Support/Titan Momentum/full-live"
CONFIG="config/full_live_ibkr.json"
PYTHON="python3"
SDK_VENV=""
BUILD_OUT="${HOME}/titan-release"

# ---- parse args --------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --sdk-venv)  SDK_VENV="${2:?}"; shift 2 ;;
    --root)      INSTALL_ROOT="${2:?}"; shift 2 ;;
    --config)    CONFIG="${2:?}"; shift 2 ;;
    --python)    PYTHON="${2:?}"; shift 2 ;;
    --out)       BUILD_OUT="${2:?}"; shift 2 ;;
    -h|--help)   grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; exit 2 ;;
  esac
done

echo "== Titan full-live: build + PAUSED install (never activates) =="
echo "repo:        $REPO_ROOT"
echo "install root:$INSTALL_ROOT"
echo "config:      $CONFIG"
echo

# ---- preflight (refuse rather than guess) ------------------------------------
fail() { echo "STOP: $*" >&2; exit 1; }

command -v "$PYTHON" >/dev/null 2>&1 || fail "python interpreter '$PYTHON' not found (need >=3.11)."
"$PYTHON" - <<'PY' || fail "python must be >= 3.11"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY

[ -n "$SDK_VENV" ] || fail "--sdk-venv is required: an authorized venv containing official ibapi 10.50.2. Without it the paused install blocks by design."
[ -d "$SDK_VENV" ] || fail "--sdk-venv path does not exist: $SDK_VENV"

cd "$REPO_ROOT"
command -v git >/dev/null 2>&1 || fail "git not found."
if [ -n "$(git status --porcelain)" ]; then
  fail "working tree is not clean. Commit or remove changes before building a release (the build refuses a dirty tree)."
fi
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
HEAD_SHA="$(git rev-parse HEAD)"
echo "branch: $BRANCH   HEAD: $HEAD_SHA"
if [ "$BRANCH" != "main" ] && [ "$BRANCH" != "master" ]; then
  echo "NOTE: you are not on main/master. The release will be bound to THIS commit."
  echo "      If PR #9 is not merged yet, prefer building from merged source so"
  echo "      the installed release matches what you reviewed."
  printf "      Continue building from %s? [y/N] " "$BRANCH"
  read -r reply
  case "$reply" in y|Y|yes|YES) ;; *) fail "aborted at your request; merge first, then re-run." ;; esac
fi

# ---- build (offline, deterministic) ------------------------------------------
echo
echo "== Building release =="
mkdir -p "$BUILD_OUT"
BUILD_JSON="$(mktemp "${TMPDIR:-/tmp}/titan-build.XXXXXX.json")"
PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -B scripts/build_full_live_release.py \
  --output-dir "$BUILD_OUT" \
  --config "$CONFIG" > "$BUILD_JSON"
cat "$BUILD_JSON"

# Extract archive path + sha256 without an inline interpreter program.
ARCHIVE="$("$PYTHON" -c 'import json,sys;print(json.load(open(sys.argv[1]))["archive"])' "$BUILD_JSON")"
ARCHIVE_SHA="$("$PYTHON" -c 'import json,sys;print(json.load(open(sys.argv[1]))["archive_sha256"])' "$BUILD_JSON")"
[ -f "$ARCHIVE" ] || fail "build did not produce the expected archive."
echo
echo "archive:     $ARCHIVE"
echo "archive_sha: $ARCHIVE_SHA"

# ---- paused install (cannot start trading or contact the broker) -------------
echo
echo "== Installing PAUSED =="
echo "This writes paused release state under the install root. It does NOT start"
echo "the runtime, load launchd, or contact the broker."
PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -B scripts/install_full_live_paused.py \
  "$ARCHIVE" \
  --root "$INSTALL_ROOT" \
  --trusted-source-root "$REPO_ROOT" \
  --expected-source-revision "$HEAD_SHA" \
  --expected-archive-sha256 "$ARCHIVE_SHA" \
  --ibkr-sdk-venv "$SDK_VENV"

echo
echo "== DONE: installed PAUSED. Trading is NOT on. =="
echo
echo "Next (read-only checks YOU run, then paste the output to Kiro):"
echo "  titan-full-live status    --install-root \"$INSTALL_ROOT\""
echo "  titan-full-live readiness --install-root \"$INSTALL_ROOT\"   # EXPECT it to fail with"
echo "                                                              # SESSION_TRADING_RUNTIME_INTEGRATION_UNAVAILABLE"
echo "  titan-full-live doctor    --install-root \"$INSTALL_ROOT\""
echo
echo "The 'activate' command is intentionally NOT run by this script. It turns on"
echo "live trading, needs a confirmation phrase only you enter, and must wait for"
echo "real P8/P9/P10 evidence to pass. See KIRO_OWNER_RUNBOOK_READINESS.md."
