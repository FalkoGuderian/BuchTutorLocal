#!/usr/bin/env bash
# Startet den lokalen TTS-Server (Piper) auf :8082 — OpenAI-kompatibel.
#
#   ./run_tts.sh            startet den Server im Vordergrund (Strg+C beendet ihn)
#   ./run_tts.sh setup      installiert Piper + laedt die Stimmen (DE + EN)
#
# Liefert POST /v1/audio/speech — den Endpunkt, den app.html (~23653) und LiteLLM
# erwarten. Deutsch ist Standard; voice="en" schaltet auf Englisch.
#
# WARUM PIPER UND NICHT QWEN3-TTS (Entscheidung 15.07.2026):
# Die Qwen3-TTS-GGUFs unter "D:/LM Studio Models/Serveurperso" sind fuer
# qwentts.cpp gebaut, NICHT fuer llama.cpp — deshalb scheitert llama-server mit
# "key general.file_type has wrong type str but expected type u32" (und die
# LM-Studio-UI ebenso, sie nutzt dieselbe Runtime). Das ist kein kaputtes File:
# qwentts.cpp ist ein eigener C++17/GGML-Port mit eigener GGUF-Konvention.
#
# qwentts.cpp waere durchaus attraktiv — es bringt selbst einen `tts-server` mit
# /v1/audio/speech mit (ein Wrapper waere also NICHT noetig). Es scheitert hier
# nur an der Toolchain: kein CMake, kein Visual-Studio-C++-Workload, kein Vulkan
# SDK auf diesem Rechner (~3-5 GB Nachinstallation), CUDA faellt mangels NVIDIA
# GPU aus. Ausserdem liegt lokal nur die "customvoice"-Variante, die zwingend
# einen Referenzclip zum Klonen braucht; fuer benannte Sprecher waere zusaetzlich
# der "base"-Talker noetig (629 MB).
#
# Wer spaeter doch auf qwentts.cpp wechselt: buildvulkan.cmd bauen, tts-server
# auf :8082 starten — dieser Server hier kann dann ersatzlos entfallen, die
# LiteLLM-Config bleibt unveraendert.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
PY="$SCRIPT_DIR/.venv/Scripts/python.exe"

setup() {
  [ -f "$PY" ] || { echo "[info] Kein venv — erst ./run_litellm.sh setup ausfuehren."; exit 1; }
  echo "[info] Installiere Piper ..."
  "$PY" -m pip install --quiet piper-tts
  mkdir -p voices
  echo "[info] Lade Stimmen (je ~60 MB) ..."
  "$PY" -m piper.download_voices de_DE-thorsten-medium --download-dir voices
  "$PY" -m piper.download_voices en_US-lessac-medium --download-dir voices
  echo "[ok] Setup fertig."
}

if [ "${1:-}" = "setup" ]; then setup; exit 0; fi

[ -f "$PY" ] || { echo "[fehler] venv fehlt — ./run_litellm.sh setup ausfuehren."; exit 1; }
if ! "$PY" -c "import piper" > /dev/null 2>&1 || [ -z "$(ls voices/*.onnx 2>/dev/null)" ]; then
  echo "[info] Piper oder Stimmen fehlen — hole Setup nach."
  setup
fi

echo "[info] TTS auf http://127.0.0.1:8082/v1/audio/speech"
echo "[info] Test: curl -s -X POST http://127.0.0.1:8082/v1/audio/speech \\"
echo "         -H 'Content-Type: application/json' \\"
echo "         -d '{\"input\":\"Guten Tag\",\"voice\":\"de\"}' --output test.wav"
echo

exec "$PY" piper_server.py --host 127.0.0.1 --port 8082 --voices-dir voices
