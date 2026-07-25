#!/usr/bin/env bash
# ============================================================
# start_host.sh - Start the Agent Monitor Host Daemon
# ============================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$PROJECT_ROOT/.venv"
RESTART=false
API_PORT=8765
HOST_ARGS=()

while [ "$#" -gt 0 ]; do
    case "$1" in
        --restart)
            RESTART=true
            shift
            ;;
        --api-port)
            if [ "$#" -lt 2 ]; then
                echo "[ERROR] --api-port requires a port number."
                exit 2
            fi
            API_PORT="$2"
            HOST_ARGS+=("$1" "$2")
            shift 2
            ;;
        --api-port=*)
            API_PORT="${1#*=}"
            HOST_ARGS+=("$1")
            shift
            ;;
        *)
            HOST_ARGS+=("$1")
            shift
            ;;
    esac
done

echo "============================================"
echo "  Starting Agent Monitor Host Daemon"
echo "============================================"

if [ ! -d "$VENV_DIR" ]; then
    echo "[ERROR] Virtual environment not found at $VENV_DIR"
    echo "Please run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

cd "$PROJECT_ROOT"

echo "[INFO] Starting daemon..."
echo "[INFO] - USB Port will be AUTO-DETECTED if not specified."
echo "[INFO] - To enable Bluetooth (BLE), pass the --ble flag."
echo ""
echo "Usage examples:"
echo "  ./bin/start_host.sh                # USB connection with auto port detection"
echo "  ./bin/start_host.sh --ble          # Enable Bluetooth (BLE) connection"
echo "  ./bin/start_host.sh --port /dev/X  # Specify manual USB port"
echo "  ./bin/start_host.sh --restart      # Restart an existing Host on the API port"
echo "============================================"
echo ""

if [ "$RESTART" = true ]; then
    if ! command -v lsof >/dev/null 2>&1; then
        echo "[ERROR] --restart requires 'lsof' to identify the Host safely."
        exit 1
    fi

    LISTENER_PIDS="$(lsof -tiTCP:"$API_PORT" -sTCP:LISTEN 2>/dev/null || true)"
    if [ -z "$LISTENER_PIDS" ]; then
        echo "[INFO] No existing Host is listening on API port $API_PORT."
    else
        for HOST_PID in $LISTENER_PIDS; do
            HOST_COMMAND="$(ps -p "$HOST_PID" -o command= 2>/dev/null || true)"
            case "$HOST_COMMAND" in
                *"-m agent_monitor.main"*)
                    echo "[INFO] Stopping existing Host PID $HOST_PID on API port $API_PORT..."
                    kill -TERM "$HOST_PID"
                    ;;
                *)
                    echo "[ERROR] Port $API_PORT is owned by a non-Agent-Monitor process:"
                    echo "        PID $HOST_PID: $HOST_COMMAND"
                    echo "[ERROR] Refusing to terminate it."
                    exit 1
                    ;;
            esac
        done

        for HOST_PID in $LISTENER_PIDS; do
            ATTEMPTS=0
            while kill -0 "$HOST_PID" 2>/dev/null; do
                if [ "$ATTEMPTS" -ge 50 ]; then
                    echo "[ERROR] Host PID $HOST_PID did not stop within 5 seconds."
                    exit 1
                fi
                sleep 0.1
                ATTEMPTS=$((ATTEMPTS + 1))
            done
        done
        echo "[INFO] Existing Host stopped."
    fi
fi

exec "$VENV_DIR/bin/python" -m agent_monitor.main "${HOST_ARGS[@]}"
