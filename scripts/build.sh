#!/bin/bash
set -e

APP_NAME=$(grep '^name = ' pyproject.toml | cut -d'"' -f2)

docker build -t "$APP_NAME" .
