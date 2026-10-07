#!/usr/bin/env bash
# Medicare VoiceOps — one-time setup (macOS / Linux). Needs Python 3.11/3.12 and Node.js 20+.
set -e
cd "$(dirname "$0")"
cd backend
PY=${PYTHON_BIN:-python3.11}
command -v "$PY" >/dev/null 2>&1 || PY=python3
[ -d venv ] || "$PY" -m venv venv
source venv/bin/activate
pip install --upgrade pip >/dev/null
pip install -r requirements.txt
python -m nltk.downloader punkt_tab
cd ../frontend
npm install
npm run build
echo "Setup complete. Run the database script (README step 2), then: source backend/venv/bin/activate && python start.py"
