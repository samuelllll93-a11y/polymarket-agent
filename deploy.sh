#!/usr/bin/env bash
# deploy.sh — Deploy polymarket-agent to London VPS
#
# Usage:
#   ./deploy.sh                  # Full deploy (pull + venv + restart)
#   ./deploy.sh --check          # Health check only
#   ./deploy.sh --logs           # Tail PM2 logs
#   ./deploy.sh --status         # Show PM2 process status
#   ./deploy.sh --restart        # Restart bot without pulling code
#   ./deploy.sh --dry-run-toggle # Toggle DRY_RUN=True/False in .env
#
# Prerequisites:
#   - SSH alias 'apex-vps' configured in ~/.ssh/config pointing to polybot@81.92.219.229
#   - VPS has Python 3.12, git, pm2 installed
#   - ~/polymarket-agent exists on VPS and remote is set

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VPS="apex-vps"
REMOTE_DIR="~/polymarket-agent"
BRANCH="polymarket-session-2"
PM2_APP="polymarket-bot"
PYTHON="python3"
LOG_LINES=50

# Colours
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'  # No Colour

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log()  { echo -e "${BLUE}[deploy]${NC} $*"; }
ok()   { echo -e "${GREEN}[ok]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*"; }
err()  { echo -e "${RED}[error]${NC} $*" >&2; }

check_ssh() {
    if ! ssh -o ConnectTimeout=5 -o BatchMode=yes "$VPS" "echo ok" &>/dev/null; then
        err "Cannot reach VPS via ssh $VPS — check ~/.ssh/config and VPN"
        exit 1
    fi
    ok "SSH connection to $VPS: OK"
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

cmd_check() {
    log "Running health check on $VPS ..."
    check_ssh

    ssh "$VPS" bash <<'REMOTE'
set -e
cd ~/polymarket-agent

echo ""
echo "=== Python version ==="
python3 --version

echo ""
echo "=== Git status ==="
git log --oneline -3

echo ""
echo "=== PM2 status ==="
pm2 list 2>/dev/null || echo "PM2 not running or not installed"

echo ""
echo "=== Recent logs (last 20 lines) ==="
pm2 logs polymarket-bot --lines 20 --nostream 2>/dev/null || \
    tail -20 logs/pm2-out.log 2>/dev/null || echo "No logs found"

echo ""
echo "=== Disk / memory ==="
df -h ~ | tail -1
free -h | head -2
REMOTE
}

cmd_logs() {
    log "Streaming PM2 logs from $VPS (Ctrl+C to stop) ..."
    check_ssh
    ssh -t "$VPS" "pm2 logs $PM2_APP --lines $LOG_LINES"
}

cmd_status() {
    log "PM2 status on $VPS ..."
    check_ssh
    ssh "$VPS" "pm2 list; pm2 show $PM2_APP 2>/dev/null || true"
}

cmd_restart() {
    log "Restarting $PM2_APP on $VPS (no code pull) ..."
    check_ssh
    ssh "$VPS" "pm2 restart $PM2_APP && sleep 3 && pm2 list"
    ok "Bot restarted"
}

cmd_dry_run_toggle() {
    log "Checking current DRY_RUN value on $VPS ..."
    check_ssh
    CURRENT=$(ssh "$VPS" "grep -E '^DRY_RUN' $REMOTE_DIR/.env 2>/dev/null | cut -d= -f2 || echo 'True'")
    log "Current DRY_RUN=$CURRENT"
    if [[ "$CURRENT" == "True" ]]; then
        NEW="False"
    else
        NEW="True"
    fi
    warn "About to set DRY_RUN=$NEW on $VPS — this will restart the bot."
    read -p "Confirm? [y/N] " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        ssh "$VPS" "sed -i 's/^DRY_RUN=.*/DRY_RUN=$NEW/' $REMOTE_DIR/.env && pm2 restart $PM2_APP"
        ok "DRY_RUN set to $NEW and bot restarted"
    else
        log "Aborted."
    fi
}

cmd_deploy() {
    log "Starting full deploy to $VPS ..."
    check_ssh

    ssh "$VPS" bash <<REMOTE
set -e
echo ""
echo "=== [1/5] Pull latest code ==="
cd $REMOTE_DIR
git fetch origin
git checkout $BRANCH
git pull origin $BRANCH
git log --oneline -3

echo ""
echo "=== [2/5] Create/update virtualenv ==="
if [ ! -d venv ]; then
    $PYTHON -m venv venv
    echo "Virtualenv created"
fi
source venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt
echo "Dependencies installed"

echo ""
echo "=== [3/5] Create logs dir ==="
mkdir -p logs data
echo "Directories OK"

echo ""
echo "=== [4/5] Verify config ==="
$PYTHON -c "import config; r = config.validate_config(); print('Config valid:', r['valid']); [print(' -', w) for w in r.get('warnings', [])]"

echo ""
echo "=== [5/5] Restart bot via PM2 ==="
if pm2 list | grep -q "$PM2_APP"; then
    pm2 restart $PM2_APP
    echo "Bot restarted"
else
    pm2 start ecosystem.config.js
    echo "Bot started via PM2"
fi

sleep 3
pm2 list
echo ""
pm2 logs $PM2_APP --lines 15 --nostream || true
REMOTE

    ok "Deploy complete!"
    log "Run './deploy.sh --check' to verify"
}

cmd_help() {
    echo "Usage: ./deploy.sh [OPTION]"
    echo ""
    echo "Options:"
    echo "  (no args)            Full deploy: pull code, install deps, restart"
    echo "  --check              Health check: SSH, git, PM2 status, recent logs"
    echo "  --logs               Stream PM2 logs in real time"
    echo "  --status             Show PM2 process list and details"
    echo "  --restart            Restart bot without pulling code"
    echo "  --dry-run-toggle     Toggle DRY_RUN=True/False in .env"
    echo "  --help               Show this help"
    echo ""
    echo "VPS: $VPS  |  Dir: $REMOTE_DIR  |  Branch: $BRANCH"
}

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

COMMAND="${1:---deploy}"

case "$COMMAND" in
    --check)          cmd_check ;;
    --logs)           cmd_logs ;;
    --status)         cmd_status ;;
    --restart)        cmd_restart ;;
    --dry-run-toggle) cmd_dry_run_toggle ;;
    --help|-h)        cmd_help ;;
    --deploy|"")      cmd_deploy ;;
    *)
        err "Unknown option: $COMMAND"
        cmd_help
        exit 1
        ;;
esac
