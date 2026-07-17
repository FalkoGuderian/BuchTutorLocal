#!/usr/bin/env bash
# Startet die lokalen llama-server-LLM-Server fuer den DocWorm-Lokalstack (git-bash).
# (Frueher run_backends.sh; umbenannt, da TTS und Flux ebenfalls Backends sind.)
#
#   ./run_llm.sh          startet Gemma 4 (:8080) + bge-m3 (:8081)
#   ./run_llm.sh stop     beendet alle llama-server-Prozesse
#   ./run_llm.sh status   zeigt den Zustand beider Backends
#
# Genutzt wird die llama-server.exe, die LM Studio bereits mitbringt — es muss
# nichts zusaetzlich installiert werden. LM Studio selbst darf parallel laufen,
# solange genug RAM frei ist (Modelle liegen dann doppelt im Speicher).
#
# WICHTIG (Windows-Firewall): Es gibt Block-Regeln fuer llama-server.exe. Loopback
# (127.0.0.1) filtert die Firewall nicht, darum binden wir bewusst auf 127.0.0.1.
# Das reicht, weil LiteLLM nativ auf demselben Host laeuft. Ein Zugriff aus einem
# Container/LAN wuerde eine Firewall-Freigabe brauchen — siehe README.
set -euo pipefail

RUNTIME="${LLAMA_RUNTIME:-$HOME/.lmstudio/extensions/backends/llama.cpp-win-x86_64-vulkan-avx2-2.24.0}"
SERVER="$RUNTIME/llama-server.exe"
MODELS="${LMS_MODELS:-D:/LM Studio Models}"

GEMMA_DIR="$MODELS/lmstudio-community/gemma-4-E2B-it-GGUF"
GEMMA_MODEL="$GEMMA_DIR/gemma-4-E2B-it-Q4_K_M.gguf"
GEMMA_MMPROJ="$GEMMA_DIR/mmproj-gemma-4-E2B-it-BF16.gguf"   # Vision + Audio Tower
BGE_MODEL="$MODELS/cPilotGod/baai-bge-m3-568m-gguf/bge-m3-Q8_0.gguf"

LOG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/logs"

case "${1:-start}" in
  stop)
    taskkill //IM llama-server.exe //F > /dev/null 2>&1 && echo "[ok] Backends beendet" \
      || echo "[info] Es lief kein llama-server"
    exit 0
    ;;
  status)
    for p in 8080 8081; do
      if curl -sf -m 3 "http://127.0.0.1:$p/health" > /dev/null 2>&1; then
        echo "[ok]   :$p antwortet"
      else
        echo "[--]   :$p tot"
      fi
    done
    exit 0
    ;;
esac

[ -f "$SERVER" ]       || { echo "[fehler] llama-server nicht gefunden: $SERVER"; exit 1; }
[ -f "$GEMMA_MODEL" ]  || { echo "[fehler] Gemma-Modell fehlt: $GEMMA_MODEL"; exit 1; }
[ -f "$GEMMA_MMPROJ" ] || { echo "[fehler] mmproj fehlt (ohne ihn kein Audio/Vision): $GEMMA_MMPROJ"; exit 1; }
[ -f "$BGE_MODEL" ]    || { echo "[fehler] bge-m3 fehlt: $BGE_MODEL"; exit 1; }

mkdir -p "$LOG_DIR"
taskkill //IM llama-server.exe //F > /dev/null 2>&1 || true
sleep 1

echo "[info] Gemma 4 E2B (Chat + Vision + Audio) auf :8080 ..."
# -ngl 99: soviel wie moeglich auf die GPU (Vulkan). llama.cpp offloadet nur so
# viele Layer, wie in den VRAM passen — der Rest bleibt automatisch auf der CPU.
# --mmproj-offload bringt auch den Vision/Audio-Encoder auf die GPU (Default an).
"$SERVER" -m "$GEMMA_MODEL" --mmproj "$GEMMA_MMPROJ" -ngl 99 --mmproj-offload \
  --alias gemma-4-e2b --host 127.0.0.1 --port 8080 -c 8192 \
  > "$LOG_DIR/gemma.log" 2>&1 &

echo "[info] bge-m3 (Embeddings) auf :8081 ..."
"$SERVER" -m "$BGE_MODEL" --embedding \
  --alias bge-m3 --host 127.0.0.1 --port 8081 -c 8192 \
  > "$LOG_DIR/bge-m3.log" 2>&1 &

# Warten bis beide /health liefern (Gemma braucht am laengsten: Modell + mmproj).
# -f ist wichtig: waehrend des Ladens antwortet llama-server mit HTTP 503
# ("Loading model"). Ohne -f wertet curl das als Erfolg und wir melden zu frueh
# "bereit" — der Audio-Encoder ist dann noch gar nicht initialisiert.
for i in $(seq 1 120); do
  if curl -sf -m 2 http://127.0.0.1:8080/health > /dev/null 2>&1 \
  && curl -sf -m 2 http://127.0.0.1:8081/health > /dev/null 2>&1; then
    echo "[ok] Backends bereit nach ${i}s"
    # Beleg, dass der Audio-Encoder wirklich initialisiert wurde:
    grep -q "init_audio" "$LOG_DIR/gemma.log" \
      && echo "[ok] Audio-Encoder aktiv (init_audio im Log)" \
      || echo "[warn] Kein init_audio im Log — laeuft Gemma ohne mmproj?"
    echo
    echo "  Gemma   : http://127.0.0.1:8080/v1   (Logs: logs/gemma.log)"
    echo "  bge-m3  : http://127.0.0.1:8081/v1   (Logs: logs/bge-m3.log)"
    echo "  Weiter  : ./run_litellm.sh"
    exit 0
  fi
  sleep 1
done

echo "[fehler] Backends nicht bereit — letzte Zeilen aus gemma.log:"
tail -5 "$LOG_DIR/gemma.log"
exit 1
