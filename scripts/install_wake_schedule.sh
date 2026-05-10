#!/usr/bin/env bash
set -euo pipefail

WAKE_TIME="${1:-07:55:00}"

sudo pmset repeat wakeorpoweron MTWRFSU "$WAKE_TIME"

echo "Installed repeating wake schedule:"
pmset -g sched
