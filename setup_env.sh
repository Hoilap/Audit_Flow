#!/usr/bin/env bash
set -euo pipefail
echo "Creating Python virtualenv .venv..."
python3 -m venv .venv
echo "Activating and upgrading pip..."
source .venv/bin/activate
python -m pip install --upgrade pip
echo "Installing Python requirements..."
pip install -r requirements.txt
if [ -f desktop/package.json ]; then
  echo "Installing Node dependencies for desktop..."
  (cd desktop && npm install)
fi
echo "Done. To activate venv later: source .venv/bin/activate"
