#!/bin/bash
# Start PostgreSQL and Hindsight API server for work-buddy.
#
# Usage:
#   bash scripts/start-hindsight.sh          # start both
#   bash scripts/start-hindsight.sh status   # check status only
#   bash scripts/start-hindsight.sh stop     # stop both

PGDATA="${HINDSIGHT_PGDATA:-$HOME/hindsight-pgdata}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Helper: run a command inside the conda work-buddy env
conda_run() {
    powershell.exe -Command "conda activate work-buddy; $*" 2>&1
}

status() {
    echo "=== PostgreSQL ==="
    if conda_run pg_ctl -D "$PGDATA" status | grep -q "server is running"; then
        echo "  Running"
    else
        echo "  Stopped"
    fi

    echo "=== Hindsight API ==="
    if curl -s http://localhost:8888/health >/dev/null 2>&1; then
        echo "  Running (healthy)"
    else
        echo "  Not responding"
    fi
}

start_pg() {
    if conda_run pg_ctl -D "$PGDATA" status | grep -q "server is running"; then
        echo "PostgreSQL already running."
    else
        echo "Starting PostgreSQL..."
        conda_run pg_ctl -D "$PGDATA" -l "$PGDATA/logfile" start
        sleep 2
        echo "PostgreSQL started."
    fi
}

start_hindsight() {
    if curl -s http://localhost:8888/health >/dev/null 2>&1; then
        echo "Hindsight already running."
        return
    fi

    echo "Starting Hindsight API server..."
    # Load .env and start in background
    powershell.exe -Command "
        conda activate work-buddy
        [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
        \$env:PYTHONIOENCODING='utf-8'
        Get-Content '$REPO_ROOT/.env' | ForEach-Object {
            if (\$_ -match '^([^#][^=]*)=(.*)$') {
                [Environment]::SetEnvironmentVariable(\$matches[1].Trim(), \$matches[2].Trim(), 'Process')
            }
        }
        Start-Process -NoNewWindow -FilePath 'hindsight-api'
    " >/dev/null 2>&1

    # Wait for it (model loading can take 30-60s on first run)
    for i in $(seq 1 90); do
        if curl -s http://localhost:8888/health >/dev/null 2>&1; then
            echo "Hindsight API started (took ${i}s)."
            return
        fi
        sleep 1
    done
    echo "WARNING: Hindsight did not start within 90s. Check server output."
}

stop_all() {
    echo "Stopping Hindsight..."
    powershell.exe -Command "
        Get-NetTCPConnection -LocalPort 8888 -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id \$_.OwningProcess -Force }
    " 2>/dev/null

    echo "Stopping PostgreSQL..."
    conda_run pg_ctl -D "$PGDATA" stop 2>/dev/null

    echo "Done."
}

case "${1:-start}" in
    status) status ;;
    stop)   stop_all ;;
    start)
        start_pg
        start_hindsight
        status
        ;;
    *)
        echo "Usage: $0 {start|stop|status}"
        exit 1
        ;;
esac
