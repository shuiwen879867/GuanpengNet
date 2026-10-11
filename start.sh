#!/bin/sh
set -eu
cd "$(dirname "$0")"
service="${1:-all}"
if [ "$#" -gt 0 ]; then shift; fi
exec python3 deploy.py "$service" --no-browser "$@"
