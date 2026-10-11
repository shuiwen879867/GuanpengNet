#!/bin/sh
# Install system prerequisites on Debian or Ubuntu, then start the selected service.
set -eu
cd "$(dirname "$0")"
service="${1:-all}"
case "$service" in web|bot|all) ;; *) echo "Usage: sh install-linux.sh [web|bot|all]" >&2; exit 1 ;; esac
if command -v apt-get >/dev/null 2>&1; then
    if [ "$(id -u)" -eq 0 ]; then
        privilege=""
    elif command -v sudo >/dev/null 2>&1; then
        privilege="sudo"
    else
        echo "Please install Python 3, python3-venv, Chromium and Chinese fonts using your package manager." >&2
        exit 1
    fi
    $privilege apt-get update
    if [ "$service" = web ]; then
        $privilege apt-get install -y python3 python3-venv python3-pip
    else
        browser_package="chromium"
        if ! apt-cache show chromium >/dev/null 2>&1; then
            browser_package="chromium-browser"
        fi
        $privilege apt-get install -y python3 python3-venv python3-pip "$browser_package" fonts-noto-cjk build-essential
    fi
else
    echo "Automatic prerequisite installation supports Debian/Ubuntu. Checking your existing Python installation."
fi
exec sh start.sh "$service"
