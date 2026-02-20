#!/bin/bash
# Swing Trader — Launch Script
# Starts the Telegram bot + scheduled pipeline

cd "$(dirname "$0")"

# Load environment variables
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
elif [ -f ~/.env ]; then
    export $(grep -v '^#' ~/.env | xargs)
else
    echo "ERROR: No .env file found. Copy .env.example to .env and fill in your API keys."
    exit 1
fi

# Check for virtual environment
if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

echo "Starting Swing Trader..."
python main.py
