#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH=''
export PYTHONHOME=''
export VIRTUAL_ENV=''
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
exec ./venv/bin/python -m pytest "$@"
