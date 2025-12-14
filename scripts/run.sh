#!/bin/bash
set -e

APP_NAME=$(grep '^name = ' pyproject.toml | cut -d'"' -f2)

# Check for required files and directories
if [ ! -f ".env" ]; then
    echo "Warning: .env file not found in the project root."
fi

if [ ! -f "questions.json" ]; then
    echo "Warning: questions.json file not found in the project root."
fi

if [ ! -d "assets" ]; then
    echo "Warning: assets directory not found in the project root."
fi

docker run --rm --name "$APP_NAME" \
  -v "$(pwd)/.env:/app/.env" \
  -v "$(pwd)/questions.json:/app/questions.json" \
  -v "$(pwd)/assets:/app/assets" \
  "$APP_NAME"
