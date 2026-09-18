#!/bin/bash
# Swap in a new `claude setup-token` credential without it touching shell history.
#
#   1. run:  claude setup-token          (copy the sk-ant-oat01... value)
#   2. run:  ./update_claude_token.sh    (paste when prompted — input is hidden)
#
# Backs up the env file, rewrites the token line, restarts the bot, verifies auth.

set -euo pipefail
ENV_FILE="$HOME/.moomoo_alerts.env"
KEY="CLAUDE_CODE_OAUTH_TOKEN"
LABEL="com.derrick.moomoo-telegram-bot"

[ -f "$ENV_FILE" ] || { echo "✗ $ENV_FILE not found"; exit 1; }

printf 'Paste the new token (hidden), then Enter: '
read -rs TOKEN
echo

case "$TOKEN" in
  sk-ant-oat01*) ;;
  "") echo "✗ nothing pasted"; exit 1 ;;
  *)  echo "✗ doesn't look like a setup-token (expected sk-ant-oat01...)"; exit 1 ;;
esac

BACKUP="$ENV_FILE.bak.$(date +%Y%m%d%H%M%S)"
cp -p "$ENV_FILE" "$BACKUP"
chmod 600 "$BACKUP"
echo "✓ backup: $BACKUP"

# rewrite in place via python so the token never appears in an argv or history
TOKEN="$TOKEN" ENV_FILE="$ENV_FILE" KEY="$KEY" python3 - <<'PY'
import os, pathlib
p = pathlib.Path(os.environ["ENV_FILE"]); key = os.environ["KEY"]; tok = os.environ["TOKEN"]
lines = p.read_text().splitlines()
out, found = [], False
for ln in lines:
    if ln.strip().startswith(key + "="):
        out.append(f"{key}={tok}"); found = True
    else:
        out.append(ln)
if not found:
    out.append(f"{key}={tok}")
p.write_text("\n".join(out) + "\n")
print(f"✓ {'replaced' if found else 'appended'} {key}")
PY

chmod 600 "$ENV_FILE"
echo "✓ $ENV_FILE mode 600"

echo "→ restarting bot…"
launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null \
  || { launchctl unload "$HOME/Library/LaunchAgents/$LABEL.plist" 2>/dev/null || true
       launchctl load  "$HOME/Library/LaunchAgents/$LABEL.plist"; }
sleep 3

echo "→ verifying tier-2 auth…"
ENV_FILE="$ENV_FILE" python3 - <<'PY'
import os, subprocess, pathlib, sys
cfg = {}
for ln in pathlib.Path(os.environ["ENV_FILE"]).read_text().splitlines():
    ln = ln.strip()
    if ln and not ln.startswith("#") and "=" in ln:
        k, v = ln.split("=", 1); cfg[k.strip()] = v.strip()
env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_BASE_URL"}
env["CLAUDE_CODE_OAUTH_TOKEN"] = cfg["CLAUDE_CODE_OAUTH_TOKEN"]
claude = str(pathlib.Path.home() / ".local/bin/claude")
try:
    p = subprocess.run([claude, "-p", "Reply with exactly: AUTH_OK",
                        "--tools", "", "--model", "sonnet"],
                       capture_output=True, text=True, timeout=90, cwd="/tmp", env=env)
    ok = "AUTH_OK" in (p.stdout or "")
    print("✓ tier-2 chat WORKING" if ok else f"✗ FAILED: {(p.stderr or p.stdout)[:200]}")
    sys.exit(0 if ok else 1)
except subprocess.TimeoutExpired:
    print("✗ timed out"); sys.exit(1)
PY

echo
echo "Done. Send /help to @RICHHHMAN_BOT to confirm, then delete old backups:"
echo "  ls -la $ENV_FILE.bak.*"
