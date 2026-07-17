#!/usr/bin/env bash
# Startet die lokale Bildgenerierung (FLUX.2 Klein 4B via stable-diffusion.cpp)
# als OpenAI-kompatibles Backend fuer den DocWorm-Lokalstack (git-bash).
#
#   ./run_flux.sh            startet flux_server (:8083) + sd-server (:8084) im Vordergrund
#   ./run_flux.sh setup      laedt sd.cpp-Binary + Modelle nach, falls etwas fehlt
#   ./run_flux.sh stop       beendet sd-server + flux_server
#   ./run_flux.sh status     zeigt den Zustand
#
# flux_server.py ist ein duenner Shim: Die App erzeugt Bilder ueber
# POST /chat/completions mit einem Bildmodell (OpenRouter/Gemini-Stil) und liest
# das Bild aus message.images[]. sd-server (leejet/stable-diffusion.cpp) kennt nur
# /v1/images/generations — der Shim uebersetzt und haelt die ~5 GB Gewichte warm.
#
# Kette:  app.html -> LiteLLM :4000 (Modell "flux-klein") -> flux_server :8083
#         -> sd-server :8084 (intern)
#
# CPU-Build bewusst gewaehlt: Fuer die AMD-iGPU gibt es kein aktuelles Windows-
# Vulkan-Prebuilt (nur CPU/CUDA/ROCm), ROCm laeuft nicht auf iGPUs, und Vulkan
# selbst zu bauen braeuchte das Vulkan SDK (~3-5 GB) + MSVC. FLUX.2 Klein 4B bei
# 4 Steps ist auf der CPU handhabbar. Vulkan spaeter nachruestbar (README).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

SD_BIN="$SCRIPT_DIR/sdcpp/sd-server.exe"
MODEL_DIR="${FLUX_MODEL_DIR:-D:/LM Studio Models/flux2-klein-4b}"
DIFFUSION="flux-2-klein-4b-Q4_0.gguf"
ENCODER="Qwen3-4B-Q4_K_M.gguf"
VAE="flux2-vae.safetensors"
SIZE="${FLUX_SIZE:-256x256}"
STEPS="${FLUX_STEPS:-4}"

SD_ZIP_URL="https://github.com/leejet/stable-diffusion.cpp/releases/download/master-778-c00a9e9/sd-master-c00a9e9-bin-win-cpu-x64.zip"

# Python: venv bevorzugt (Konsistenz), sonst System-Python. flux_server.py nutzt
# nur die Standardbibliothek — es braucht das LiteLLM-venv NICHT.
PY="$SCRIPT_DIR/.venv/Scripts/python.exe"
[ -f "$PY" ] || PY="python"

case "${1:-start}" in
  stop)
    taskkill //IM sd-server.exe //F > /dev/null 2>&1 && echo "[ok] sd-server beendet" \
      || echo "[info] Kein sd-server aktiv"
    powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | Where-Object { \$_.CommandLine -match 'flux_server\.py' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force }" > /dev/null 2>&1 \
      && echo "[ok] flux_server beendet" || true
    exit 0
    ;;
  status)
    if curl -sf -m 3 "http://127.0.0.1:8083/health" > /dev/null 2>&1; then
      echo "[ok]   flux_server :8083 antwortet (bereit)"
    elif curl -s -m 3 "http://127.0.0.1:8083/health" > /dev/null 2>&1; then
      echo "[..]   flux_server :8083 laeuft, laedt noch die Modelle"
    else
      echo "[--]   flux_server :8083 tot"
    fi
    curl -sf -m 3 "http://127.0.0.1:8084/v1/models" > /dev/null 2>&1 \
      && echo "[ok]   sd-server   :8084 antwortet" \
      || echo "[--]   sd-server   :8084 tot/ladend"
    exit 0
    ;;
  setup)
    if [ ! -f "$SD_BIN" ]; then
      echo "[info] Lade sd.cpp win-cpu Binary ..."
      mkdir -p "$SCRIPT_DIR/sdcpp"
      curl -sL -o "$SCRIPT_DIR/sdcpp/sdcpp.zip" "$SD_ZIP_URL"
      "$PY" -c "import zipfile;zipfile.ZipFile(r'$SCRIPT_DIR/sdcpp/sdcpp.zip').extractall(r'$SCRIPT_DIR/sdcpp')"
      rm -f "$SCRIPT_DIR/sdcpp/sdcpp.zip"
      echo "[ok] sd.cpp entpackt nach sdcpp/"
    fi
    echo "[info] Modelle erwartet in: $MODEL_DIR"
    echo "  Fehlende Dateien manuell laden (huggingface):"
    echo "    $DIFFUSION  <- leejet/FLUX.2-klein-4B-GGUF"
    echo "    $ENCODER    <- unsloth/Qwen3-4B-GGUF"
    echo "    $VAE        <- Comfy-Org/flux2-dev (split_files/vae)"
    echo "[ok] Setup-Check fertig."
    exit 0
    ;;
esac

[ -f "$SD_BIN" ] || { echo "[fehler] sd-server fehlt: $SD_BIN  ->  ./run_flux.sh setup"; exit 1; }
for f in "$DIFFUSION" "$ENCODER" "$VAE"; do
  [ -f "$MODEL_DIR/$f" ] || { echo "[fehler] Modell fehlt: $MODEL_DIR/$f  ->  ./run_flux.sh setup"; exit 1; }
done

echo "[info] Bildgenerierung auf http://127.0.0.1:8083/v1/chat/completions (Modell-ID flux-klein)"
echo "[info] Groesse $SIZE, $STEPS Steps. Erstes Bild dauert extra (Modell-Ladung)."
echo "[info] Test: curl -s -X POST http://127.0.0.1:8083/v1/images/generations \\"
echo "         -H 'Content-Type: application/json' -d '{\"prompt\":\"a red apple\"}' | python -c 'import sys,json;print(len(json.load(sys.stdin)[\"data\"][0][\"b64_json\"]),\"b64-Zeichen\")'"
echo

exec "$PY" flux_server.py \
  --host 127.0.0.1 --port 8083 \
  --sd-bin "$SD_BIN" --sd-port 8084 \
  --model-dir "$MODEL_DIR" \
  --diffusion "$DIFFUSION" --encoder "$ENCODER" --vae "$VAE" \
  --steps "$STEPS" --size "$SIZE" --model-id flux-klein
