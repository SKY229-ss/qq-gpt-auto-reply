#!/bin/sh
set -eu
qq_project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$qq_project_dir"
if [ ! -x "$qq_project_dir/.venv/bin/python" ]; then
    echo "Set up the project's .venv first. Follow the installation steps in README.md."
    printf 'Press Enter to close. '
    read -r qq_dismiss
    exit 1
fi
exec "$qq_project_dir/.venv/bin/python" "$qq_project_dir/desktop.py"
