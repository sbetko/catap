#!/bin/bash
# Test script to launch catap via the bundle with proper Python environment

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Set up PATH to include the venv
export PATH="$SCRIPT_DIR/.venv/bin:$PATH"

# Launch via open command
# Note: The app will run asynchronously and output will go to Console.app
open -a "$SCRIPT_DIR/src/catap/catap.app" --args test-bundle

echo "Launched catap.app with test-bundle command"
echo "Check Console.app for output (filter for 'catap' or 'python')"
echo ""
echo "Or check the system log:"
echo "  log stream --predicate 'process == \"python3\"' --level info"
