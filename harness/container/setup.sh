#!/usr/bin/env bash
# Workspace sandbox setup, run inside the container with cwd = <ws>/sandbox.
# Idempotent. Never installs a toolchain: the image is the toolchain, and a
# mismatch between lean-toolchain and the image is reported, not repaired.
set -euo pipefail

if [ -f lean-toolchain ]; then
  want="$(tr -d '[:space:]' < lean-toolchain)"
  have="$(lean --version 2>/dev/null | sed -n 's/.*version \([0-9.]*\).*/\1/p' || true)"
  case "$want" in
    *"$have"*) ;;
    *) echo "lean-toolchain wants $want but the image has $have" >&2; exit 3 ;;
  esac
  if [ ! -f lakefile.toml ] && [ ! -f lakefile.lean ]; then
    cp -r /opt/lean/template/. .
  fi
  if [ ! -d .lake/packages/mathlib ]; then
    lake update mathlib >/dev/null 2>&1 || lake update >/dev/null
    lake exe cache get >/dev/null 2>&1 || true
  fi
  lake build Workspace >/dev/null
  echo "lean sandbox ready: $(lean --version)"
fi

if [ -f pyproject.toml ]; then
  uv sync --frozen >/dev/null
  echo "python sandbox ready: $(uv run python --version)"
fi

if command -v z3 >/dev/null 2>&1; then
  echo "z3 $(z3 --version | awk '{print $3}')"
fi
