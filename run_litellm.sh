#!/usr/bin/env bash
# Startet den LiteLLM-Proxy (:4000) als lokalen OpenRouter-Ersatz fuer DocWorm.
#
#   ./run_litellm.sh           startet den Proxy im Vordergrund (Strg+C beendet ihn)
#   ./run_litellm.sh setup     legt das venv an und installiert LiteLLM
#
# LiteLLM laeuft bewusst NATIV (nicht im Container): so erreicht es die
# llama-server-Backends ueber 127.0.0.1 — ohne host.docker.internal, ohne
# WSL-Netzwerkgrenze und ohne Firewall-Freigabe fuer llama-server.exe.
# Voraussetzung: ./run_llm.sh laeuft.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
# litellm.exe ist ein Windows-Binary: Pfade muessen Windows-Format haben,
# sonst landet der MSYS-Pfad /c/Users/... beim Proxy und die Config fehlt.
SCRIPT_DIR_W="$(cygpath -w "$SCRIPT_DIR")"
PY="$SCRIPT_DIR_W\\.venv\\Scripts\\python.exe"
LITELLM="$SCRIPT_DIR_W\\.venv\\Scripts\\litellm.exe"

setup() {
  echo "[info] Lege venv an ..."
  python -m venv .venv
  # --only-binary=litellm ist PFLICHT: neuere LiteLLM-Versionen enthalten einen
  # Rust-Teil, dessen Build auf Windows an link.exe scheitert (MSVC-Buildtools
  # waeren noetig). Das fertige Wheel umgeht den Build komplett.
  echo "[info] Installiere LiteLLM (nur Wheels, kein Rust-Build) ..."
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet --only-binary=litellm "litellm[proxy]"
  "$PY" -m pip show litellm | grep -E "^(Name|Version):"
  echo "[ok] Setup fertig."
}

if [ "${1:-}" = "setup" ]; then setup; exit 0; fi
[ -f "$LITELLM" ] || { echo "[info] Kein venv gefunden — hole Setup nach."; setup; }

if [ -z "${OPENROUTER_API_KEY:-}" ]; then
  echo "[warn] OPENROUTER_API_KEY nicht gesetzt — das Cloud-Modell openrouter-gemini bleibt inaktiv."
fi

# -f: llama-server meldet waehrend des Ladens HTTP 503, das ist noch nicht bereit.
if ! curl -sf -m 3 http://127.0.0.1:8080/health > /dev/null 2>&1; then
  echo "[warn] Gemma auf :8080 antwortet nicht (oder laedt noch)."
  echo "[warn] Zuerst ./run_llm.sh starten und warten, bis es 'Backends bereit' meldet."
fi

echo "[info] LiteLLM auf http://127.0.0.1:4000/v1  (Key: sk-local-llm)"
echo "[info] Test: python smoke_test.py"
echo

# PYTHONIOENCODING ist PFLICHT: LiteLLMs Start-Banner enthaelt Unicode-Zeichen,
# an denen die cp1252-Konsole von Windows mit UnicodeEncodeError abstuerzt,
# bevor der Server ueberhaupt lauscht.
# Binding-Host: Der ganze Stack ist auf Loopback vereinheitlicht (Backends, Piper,
# Flux und LiteLLM). App (Desktop) und smoke_test.py defaulten ebenfalls auf 127.0.0.1
# -> kein IP-Mismatch. Fuer Handy-Zugriff aus dem LAN LITELLM_HOST=0.0.0.0 setzen und
# in der App die PC-LAN-IP eintragen (z.B. http://192.168.x.x:4000/v1). Firewall:
# firewall_open_4000.cmd. Default bleibt Loopback (nicht im LAN exponiert).
LITELLM_HOST="${LITELLM_HOST:-127.0.0.1}"
echo "[info] Binde an Host $LITELLM_HOST (fuer Handy: LITELLM_HOST=0.0.0.0 ./run_litellm.sh)"
PYTHONIOENCODING=utf-8 exec "$LITELLM" \
  --config "$SCRIPT_DIR_W\\litellm_config.yaml" \
  --host "$LITELLM_HOST" --port 4000
