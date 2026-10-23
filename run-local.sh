#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo 'Create .venv and install the dependencies described in README.md first.' >&2
  exit 1
fi
exec .venv/bin/python -m device_sre "$@"

