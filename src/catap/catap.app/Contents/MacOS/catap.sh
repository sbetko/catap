#!/bin/bash
# catap launcher script with logging

exec >> /tmp/catap_launch.log 2>&1
echo "=== catap launch $(date) ==="
echo "PWD: $(pwd)"
echo "Args: $@"

export CATAP_RUNNING_FROM_BUNDLE=1

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUNDLE_DIR="$(dirname "$(dirname "$SCRIPT_DIR")")"
PROJECT_ROOT="$(dirname "$(dirname "$(dirname "$BUNDLE_DIR")")")"

echo "SCRIPT_DIR: $SCRIPT_DIR"
echo "PROJECT_ROOT: $PROJECT_ROOT"

if [ -d "$PROJECT_ROOT/.venv/bin" ]; then
    export PATH="$PROJECT_ROOT/.venv/bin:$PATH"
    echo "Added venv to PATH"
fi

echo "PATH: $PATH"
echo "Checking python3..."

if command -v python3 &> /dev/null && python3 -c "import catap" 2>/dev/null; then
    echo "Found python3 with catap, executing..."
    exec python3 -m catap "$@"
fi

echo "ERROR: catap not found"
exit 1
