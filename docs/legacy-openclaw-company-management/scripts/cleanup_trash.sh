#!/usr/bin/env bash
set -euo pipefail

WORKSPACE="${OPENCLAW_WORKSPACE:-${HOME}/openclaw/workspace-main}"
DAYS="${OPENCLAW_CLEANUP_MTIME_DAYS:-7}"

for dir in "$WORKSPACE/tmp" "$WORKSPACE/logs"; do
  mkdir -p "$dir"
  find "$dir" -type f \( \
    -name '*.bak' -o \
    -name '*.patch' -o \
    -name '*.rej' -o \
    -name '*.orig' -o \
    -name '*failed*' -o \
    -name '*debug*' \
  \) -mtime +"$DAYS" -print -delete
done
