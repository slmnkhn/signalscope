#!/bin/sh
# Copies the app to your server, installs dependencies and restarts the service.
# Set SIGNALSCOPE_HOST (e.g. ubuntu@203.0.113.10) and SIGNALSCOPE_KEY (path to your SSH key),
# or put them in deploy/.env.local, which git ignores.
set -e
cd "$(dirname "$0")/.."
[ -f deploy/.env.local ] && . deploy/.env.local
: "${SIGNALSCOPE_HOST:?Set SIGNALSCOPE_HOST, e.g. ubuntu@203.0.113.10}"
: "${SIGNALSCOPE_KEY:?Set SIGNALSCOPE_KEY to the path of your SSH private key}"
rsync -az --delete --exclude .git --exclude .venv --exclude __pycache__ --exclude 'deploy/.env.local' \
  -e "ssh -i $SIGNALSCOPE_KEY" ./ "$SIGNALSCOPE_HOST:signalscope/"
ssh -i "$SIGNALSCOPE_KEY" "$SIGNALSCOPE_HOST" 'cd ~/signalscope && [ -d .venv ] || python3 -m venv .venv; .venv/bin/pip install -q -r requirements.txt && sudo cp deploy/signalscope.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now signalscope >/dev/null 2>&1; sudo systemctl restart signalscope; sleep 2; systemctl is-active signalscope'
