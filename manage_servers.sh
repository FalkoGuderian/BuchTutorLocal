#!/usr/bin/env bash
# manage_servers.sh — Steuerung fuer den lokalen DocWorm/BuchTutor-Serverstack.
# (Nachfolger von kill_servers.sh — kann jetzt auch starten und Status zeigen.)
#
#   ./manage_servers.sh              zeigt den Zustand aller Dienste (= status)
#   ./manage_servers.sh status       zeigt nur den Zustand, aendert NICHTS
#   ./manage_servers.sh start        startet alle Dienste in Reihenfolge (Hintergrund)
#   ./manage_servers.sh kill         beendet alle aktiven Dienste
#   ./manage_servers.sh restart      kill + start
#
# Optional laesst sich EIN Dienst adressieren:
#   ./manage_servers.sh start llm    nur die LLM-Backends starten
#   ./manage_servers.sh kill tts     nur Piper TTS beenden
#   ./manage_servers.sh status flux  nur den FLUX-Stack pruefen
#
# Dienste (id -> Ports):
#   llm      Gemma :8080 + bge-m3 :8081   (llama-server.exe)
#   tts      Piper TTS :8082              (piper_server.py)
#   flux     FLUX-Shim :8083 + sd :8084   (flux_server.py + sd-server.exe)
#   litellm  Proxy :4000                  (litellm.exe)
#
# Kill-Reihenfolge: erst die abhaengigen Dienste (Piper, Flux, LiteLLM),
# dann die schweren Backends (llama). Start-Reihenfolge: umgekehrt — erst die
# Backends, dann der Proxy obendrauf.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
LOG_DIR="$SCRIPT_DIR/logs"
mkdir -p "$LOG_DIR"

ACTION="${1:-status}"
ONLY="${2:-}"

# --- Dienst-Registry ---------------------------------------------------------
# Felder pro Dienst (parallele Arrays, damit es Bash-3-kompatibel bleibt):
#   IDS       Kurzname
#   LABELS    Anzeigetext
#   HEALTH    Leerzeichen-getrennte Health-URLs (curl -sf)
#   START     Start-Skript (wird im Hintergrund gestartet)
#   EXES      Leerzeichen-getrennte .exe-Imagenamen zum Killen ("" = keine)
#   PYS       Leerzeichen-getrennte python-CommandLine-Matches ("" = keine)
IDS=(     "llm"                        "tts"                 "flux"                       "litellm" )
LABELS=(  "LLM-Backends Gemma+bge-m3"  "Piper TTS"           "FLUX-Bildgenerierung"       "LiteLLM-Proxy" )
PORTS=(   ":8080/:8081"                ":8082"               ":8083/:8084"                ":4000" )
HEALTH=(  "http://127.0.0.1:8080/health http://127.0.0.1:8081/health" \
          "http://127.0.0.1:8082/health" \
          "http://127.0.0.1:8083/health" \
          "http://127.0.0.1:4000/health/liveliness" )
STARTS=(  "run_llm.sh"                 "run_tts.sh"          "run_flux.sh"                "run_litellm.sh" )
EXES=(    "llama-server.exe"           ""                    "sd-server.exe"              "litellm.exe" )
PYS=(     ""                           "piper_server.py"     "flux_server.py"             "" )

# Start-Reihenfolge (Backends zuerst), Kill-Reihenfolge (abhaengige zuerst).
START_ORDER=(0 1 2 3)   # llm, tts, flux, litellm
KILL_ORDER=(1 2 3 0)    # tts, flux, litellm, llm

# --- Hilfsfunktionen ---------------------------------------------------------

idx_for() {  # Name -> Index, oder -1
  local name="$1" i
  for i in "${!IDS[@]}"; do
    [ "${IDS[$i]}" = "$name" ] && { echo "$i"; return 0; }
  done
  echo "-1"
}

exe_running() {  # .exe laeuft?
  tasklist //FI "IMAGENAME eq $1" //NH 2>/dev/null | grep -qi "$1"
}

py_pids() {  # python.exe mit passender CommandLine -> PIDs
  powershell -NoProfile -Command \
    "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | Where-Object { \$_.CommandLine -match '$1' } | ForEach-Object { \$_.ProcessId }" \
    2>/dev/null | tr -d '\r' | grep -E '^[0-9]+$'
}

svc_state() {  # Index -> "up" wenn alle Health-URLs antworten, sonst "down"
  local i="$1" url all_up=1
  for url in ${HEALTH[$i]}; do
    curl -sf -m 3 "$url" >/dev/null 2>&1 || all_up=0
  done
  [ "$all_up" = "1" ] && echo "up" || echo "down"
}

# --- Aktionen ----------------------------------------------------------------

do_status() {
  local i state
  for i in "$@"; do
    state="$(svc_state "$i")"
    if [ "$state" = "up" ]; then
      echo "[up  ] ${LABELS[$i]} (${PORTS[$i]})"
    else
      echo "[down] ${LABELS[$i]} (${PORTS[$i]})"
    fi
  done
}

do_kill_one() {  # Index -> beendet exe + python-Prozesse
  local i="$1" killed=0 im pid pids
  for im in ${EXES[$i]}; do
    if exe_running "$im"; then
      if taskkill //IM "$im" //F >/dev/null 2>&1; then
        echo "        -> beendet $im"; killed=$((killed+1))
      else
        echo "        -> FEHLER beim Beenden von $im"
      fi
    fi
  done
  for match in ${PYS[$i]}; do
    pids="$(py_pids "$match")"
    for pid in $pids; do
      if powershell -NoProfile -Command "Stop-Process -Id $pid -Force" >/dev/null 2>&1; then
        echo "        -> beendet PID $pid ($match)"; killed=$((killed+1))
      else
        echo "        -> FEHLER beim Beenden von PID $pid ($match)"
      fi
    done
  done
  return "$killed"
}

do_kill() {
  local i state
  KILLED=0
  for i in "$@"; do
    state="$(svc_state "$i")"
    if [ "$state" = "up" ]; then
      echo "[laeuft] ${LABELS[$i]} (${PORTS[$i]})"
    else
      echo "[frei ] ${LABELS[$i]} (${PORTS[$i]})"
    fi
    do_kill_one "$i"; KILLED=$((KILLED + $?))
  done
}

do_start() {
  local i script
  for i in "$@"; do
    if [ "$(svc_state "$i")" = "up" ]; then
      echo "[up  ] ${LABELS[$i]} laeuft bereits — uebersprungen"
      continue
    fi
    script="${STARTS[$i]}"
    if [ ! -f "$script" ]; then
      echo "[fehler] Start-Skript fehlt: $script"
      continue
    fi
    echo "[start] ${LABELS[$i]} (${PORTS[$i]}) via $script ..."
    # Im Hintergrund; Ausgabe in eine Tee-Logdatei je Dienst.
    nohup bash "$script" > "$LOG_DIR/${IDS[$i]}.manage.log" 2>&1 &
    echo "        -> PID $! (Log: logs/${IDS[$i]}.manage.log)"
  done
}

# --- Zielmenge bestimmen -----------------------------------------------------

resolve_targets() {  # gibt Indizes in gewuenschter Reihenfolge aus
  local order=("$@")
  if [ -n "$ONLY" ]; then
    local i; i="$(idx_for "$ONLY")"
    if [ "$i" = "-1" ]; then
      echo "[fehler] Unbekannter Dienst: '$ONLY' (bekannt: ${IDS[*]})" >&2
      return 1
    fi
    echo "$i"
  else
    printf '%s\n' "${order[@]}"
  fi
}

# Fuellt das globale Array T mit den Zielindizes; bricht bei Fehler ab.
targets_or_die() {
  local order=("$@") out
  out="$(resolve_targets "${order[@]}")" || exit 1
  mapfile -t T <<< "$out"
}

# --- Hauptlauf ---------------------------------------------------------------

case "$ACTION" in
  status)
    echo "== Lokaler Stack: Zustand =="
    targets_or_die "${START_ORDER[@]}"
    do_status "${T[@]}"
    ;;
  kill|stop)
    echo "== Lokaler Stack: beende aktive Instanzen =="
    targets_or_die "${KILL_ORDER[@]}"
    do_kill "${T[@]}"
    echo
    echo "== Fertig: $KILLED Prozess(e) beendet =="
    ;;
  start)
    echo "== Lokaler Stack: starte Dienste =="
    targets_or_die "${START_ORDER[@]}"
    do_start "${T[@]}"
    echo
    echo "== Gestartet. Zustand pruefen: ./manage_servers.sh status =="
    ;;
  restart)
    echo "== Lokaler Stack: Neustart =="
    targets_or_die "${KILL_ORDER[@]}"
    do_kill "${T[@]}"
    sleep 2
    targets_or_die "${START_ORDER[@]}"
    do_start "${T[@]}"
    echo
    echo "== Neustart angestossen. Zustand: ./manage_servers.sh status =="
    ;;
  -h|--help|help)
    grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *)
    echo "[fehler] Unbekannte Aktion: '$ACTION' (status|start|kill|restart)" >&2
    exit 1
    ;;
esac
