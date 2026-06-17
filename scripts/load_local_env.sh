#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# scripts/load_local_env.sh
#
# Loads variables from .env.local into the current shell so that subsequent
# commands (alembic, uvicorn, celery, pytest, ...) see Postgres/Redis on
# `localhost` instead of `host.docker.internal`.
#
# Why this exists
# ---------------
# `.env`        → consumed by pydantic-settings AND by the FastAPI container
#                 (uses `host.docker.internal`, which only resolves INSIDE a
#                 Docker Desktop container).
# `.env.local`  → mirror of `.env` but with `localhost` for the DB/Redis URLs.
#                 Used when running commands directly on the host (no container).
#
# pydantic-settings only auto-loads `.env`, so to use the host-friendly values
# we have to export them manually into the shell environment before running
# the target command. This script does exactly that.
#
# How it works
# ------------
#   set -a       → from here on, every assigned variable is auto-exported
#                  (without it `source` would create plain shell variables, not
#                  environment variables, and child processes wouldn't see them).
#   source .env.local
#                → reads the file line by line as if it were typed; each KEY=VAL
#                  becomes an exported env var.
#   set +a       → restore default behaviour (no auto-export) for safety.
#
# Variables loaded this way OVERRIDE those that pydantic-settings reads from
# `.env` (env vars > .env file in pydantic-settings precedence).
#
# Usage
# -----
# This file MUST be sourced, not executed, otherwise the exported vars die
# with the subshell. Use either of:
#
#   source scripts/load_local_env.sh
#   . scripts/load_local_env.sh        # POSIX-shorthand of `source`
#
# Then run any command in the same shell:
#
#   uv run alembic upgrade head
#   uv run uvicorn app.main:app --reload --port 8080
#   uv run celery -A app.tasks worker --loglevel=info
#   uv run pytest
#
# Or chain everything on one line (vars survive only for that command):
#
#   ( source scripts/load_local_env.sh && uv run alembic upgrade head )
#
# To "unload" the variables, just open a new shell, or `unset` them manually.
# -----------------------------------------------------------------------------

# Guard: warn if the user EXECUTED the script instead of SOURCING it.
# Works in both bash and zsh:
#   - bash: ${BASH_SOURCE[0]} is the script path; $0 is the script when executed
#     but the shell name (e.g. "-bash") when sourced.
#   - zsh:  ${BASH_SOURCE[0]} is unset; we use ZSH_EVAL_CONTEXT, which contains
#     "file" when the script is being sourced.
if [[ -n "${BASH_VERSION:-}" ]]; then
  # bash branch
  if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    echo "⚠️  This script must be SOURCED, not executed."
    echo "    Run:   source scripts/load_local_env.sh"
    exit 1
  fi
  _script_path="${BASH_SOURCE[0]}"
elif [[ -n "${ZSH_VERSION:-}" ]]; then
  # zsh branch
  case "${ZSH_EVAL_CONTEXT:-}" in
    *:file*) : ;;  # sourced — OK
    *)
      echo "⚠️  This script must be SOURCED, not executed."
      echo "    Run:   source scripts/load_local_env.sh"
      exit 1
      ;;
  esac
  # ${(%):-%x} expands to the path of the file currently being sourced in zsh.
  _script_path="${(%):-%x}"
else
  # Unknown shell — best effort
  _script_path="${0}"
fi

# Resolve the repo root from the script's location so the user can source it
# from any working directory (e.g. `source ../scripts/load_local_env.sh`).
_repo_root="$(cd "$(dirname "${_script_path}")/.." && pwd)"
_env_file="${_repo_root}/.env.local"

if [[ ! -f "${_env_file}" ]]; then
  echo "❌ ${_env_file} not found. Create it first (mirror of .env with localhost URLs)."
  return 1 2>/dev/null || exit 1
fi

# Auto-export every variable defined while sourcing the file.
set -a
# shellcheck source=/dev/null
source "${_env_file}"
set +a

# Friendly confirmation — print only safe (non-secret) variables.
echo "✅ Loaded environment from ${_env_file}"
echo "   DATABASE_URL=${DATABASE_URL%%@*}@***"
echo "   REDIS_URL=${REDIS_URL}"
echo "   APP_VERSION=${APP_VERSION}  DEBUG=${DEBUG}"

unset _repo_root _env_file _script_path

