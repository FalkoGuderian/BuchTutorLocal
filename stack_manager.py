#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Server-Monitor & Steuerung fuer den lokalen DocWorm/BuchTutor-Stack.

Startet, restartet, killed und ueberwacht die lokalen Server-Komponenten und
streamt deren Logs live an eine eingebettete HTML-Oberflaeche (SSE).

    python stack_manager.py            # startet die Steuerung auf :8800
    python stack_manager.py --port 9xxx

Komponenten (alle auf 127.0.0.1, wie in den run_*.sh definiert):
    llm      Gemma-4-E2B (:8080) + bge-m3 (:8081)  via run_llm.sh
    litellm  Proxy (:4000)                            via run_litellm.sh
    flux     FLUX-Shim (:8083) + sd-server (:8084)   via run_flux.sh
    tts      Piper TTS (:8082)                        via run_tts.sh

Nur Standardbibliothek — keine pip-Installation noetig.
Die Start-Skripte sind Bash (Git-Bash); bash wird automatisch gesucht.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
LOGS_DIR = SCRIPT_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

MANAGER_PORT = 8800

# ---------------------------------------------------------------------------
# Persistente Einstellungen (aktuell nur der LAN-Modus).
#   LAN-Modus AUS -> LiteLLM bindet an 127.0.0.1  (nur lokal, sicherer Default)
#   LAN-Modus AN  -> LiteLLM bindet an 0.0.0.0     (vom Handy im WLAN erreichbar)
# Die Bindung greift erst nach (Re)Start von LiteLLM, weil run_litellm.sh den
# Host beim Prozessstart aus LITELLM_HOST liest (via ensure_env, s.u.). Beim
# Umschalten muss LiteLLM also einmal neu gestartet werden.
# ---------------------------------------------------------------------------
SETTINGS_FILE = SCRIPT_DIR / "stack_manager_settings.json"

def _load_settings():
    # Default aus der Umgebung: wurde der Manager mit LITELLM_HOST=0.0.0.0
    # gestartet, ist LAN-Modus vorbelegt. Die Datei ueberschreibt das.
    s = {"lan_mode": os.environ.get("LITELLM_HOST", "").strip() == "0.0.0.0"}
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "lan_mode" in raw:
            s["lan_mode"] = bool(raw["lan_mode"])
    except Exception:
        pass
    return s

SETTINGS = _load_settings()

def _save_settings():
    try:
        SETTINGS_FILE.write_text(json.dumps(SETTINGS, ensure_ascii=False),
                                 encoding="utf-8")
    except Exception:
        pass

def litellm_host():
    return "0.0.0.0" if SETTINGS.get("lan_mode") else "127.0.0.1"

def lan_ip():
    """Beste LAN-IP dieses Rechners (fuer die Handy-URL in der UI)."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # kein echter Traffic, nur Routing-Lookup
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

# ---------------------------------------------------------------------------
# Server-Definitionen. `logs` sind die Dateien, die die Live-Log-Anzeige liest.
# `tee` ist die Datei, in die der Manager den Stdout des Start-Skripts spiegelt.
# ---------------------------------------------------------------------------
SERVERS = [
    {
        "id": "llm",
        "name": "LLM-Backends (Gemma + bge-m3)",
        "desc": "llama-server: Gemma-4-E2B Chat/Vision/Audio (:8080), bge-m3 Embeddings (:8081)",
        "ports": [8080, 8081],
        "health": [
            ("http://127.0.0.1:8080/health", 200),
            ("http://127.0.0.1:8081/health", 200),
        ],
        "start": "bash run_llm.sh",
        "tee": "logs/llm.log",
        "logs": ["logs/gemma.log", "logs/bge-m3.log", "logs/llm.log"],
        "kill": [("exe", "llama-server.exe")],
    },
    {
        "id": "litellm",
        "name": "LiteLLM-Proxy (:4000)",
        "desc": "OpenAI-kompatibler Proxy -> routet an die Backends. Key: sk-... (siehe litellm_config.yaml)",
        "ports": [4000],
        # /health verlangt einen API-Key -> 401 + Exception-Spam im Log.
        # /health/liveliness ist auth-frei und der korrekte Liveness-Probe.
        "health": [("http://127.0.0.1:4000/health/liveliness", 200)],
        "start": "bash run_litellm.sh",
        "tee": "logs/litellm.log",
        "logs": ["logs/litellm.log"],
        "kill": [("exe", "litellm.exe")],
    },
    {
        "id": "flux",
        "name": "FLUX-Bildgenerierung (:8083/:8084)",
        "desc": "flux_server.py-Shim (:8083) -> sd-server.exe (:8084, stable-diffusion.cpp, FLUX.2 Klein 4B)",
        "ports": [8083, 8084],
        "health": [("http://127.0.0.1:8083/health", 200)],
        "start": "bash run_flux.sh",
        "tee": "logs/flux.log",
        "logs": ["logs/sd-server.log", "logs/flux.log"],
        "kill": [("exe", "sd-server.exe"), ("py", "flux_server.py")],
    },
    {
        "id": "tts",
        "name": "Piper TTS (:8082)",
        "desc": "piper_server.py: OpenAI-kompatibles /v1/audio/speech (DE thorsten / EN lessac)",
        "ports": [8082],
        "health": [("http://127.0.0.1:8082/health", 200)],
        "start": "bash run_tts.sh",
        "tee": "logs/tts.log",
        "logs": ["logs/tts.log"],
        "kill": [("py", "piper_server.py")],
    },
]
SERVER_BY_ID = {s["id"]: s for s in SERVERS}

# ---------------------------------------------------------------------------
# Test-Definitionen pro Server (server-seitig ausgefuehrt -> kein CORS, Key
# bleibt im Backend). Spiegeln smoke_test.py. Jeder Test hat ein `run`, das
# (ok: bool, detail: str) liefert. `input` beschreibt ein optionales Textfeld
# in der UI (Prompt / zu sprechender Text / Suchtext).
# ---------------------------------------------------------------------------
LITELLM_BASE = "http://127.0.0.1:4000/v1"
LITELLM_KEY = None  # wird aus litellm_config.yaml gelesen (master_key)


def _litellm_key():
    global LITELLM_KEY
    if LITELLM_KEY is not None:
        return LITELLM_KEY
    LITELLM_KEY = ""
    try:
        cfg = (SCRIPT_DIR / "litellm_config.yaml").read_text(encoding="utf-8")
        for line in cfg.splitlines():
            s = line.strip()
            if s.startswith("master_key:"):
                LITELLM_KEY = s.split(":", 1)[1].strip().strip('"').strip("'")
                break
    except Exception:
        pass
    return LITELLM_KEY


def _http_json(url, payload, timeout=900, key=None):
    """POST JSON, liefert (obj_or_bytes, elapsed_s). Bei HTTP-Fehler dict mit __err."""
    import urllib.error
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers=headers, method="POST")
    t0 = time.time()
    try:
        raw = urllib.request.urlopen(req, timeout=timeout).read()
        return raw, time.time() - t0
    except urllib.error.HTTPError as e:
        return {"__err": "HTTP %d: %s" % (e.code, e.read().decode("utf-8", "replace")[:200])}, time.time() - t0
    except Exception as e:
        return {"__err": str(e)[:200]}, time.time() - t0


def _red_png(size=224):
    """224x224 rotes PNG ohne Pillow (aus smoke_test.py)."""
    import struct, zlib
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * size for _ in range(size))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# --- Test-Runner (jeweils (ok, detail)) ------------------------------------
def _test_chat(base, key, prompt):
    d, dt = _http_json(base + "/chat/completions",
                       {"model": "gemma-4-e2b-fast", "max_tokens": 128,
                        "messages": [{"role": "user", "content": prompt or "Antworte nur mit: OK"}]},
                       timeout=300, key=key)
    if isinstance(d, dict) and "__err" in d:
        return False, d["__err"]
    try:
        j = json.loads(d)
        txt = (j["choices"][0]["message"]["content"] or "").strip()
        return bool(txt), "%.1fs | %s" % (dt, txt[:300])
    except Exception as e:
        return False, "unerwartete Antwort: %s" % (str(e)[:120])


def _test_vision(base, key, _prompt):
    import base64 as _b64
    # Reasoning explizit aus: Dieser Test geht DIREKT an :8080, nicht ueber LiteLLM,
    # d.h. der enable_thinking=false des -fast-Alias greift hier nicht. Mit Reasoning an
    # verbraucht das Modell das kleine max_tokens-Budget fuers Denken und der Content
    # bleibt leer (''). Wir schicken chat_template_kwargs darum direkt an llama-server.
    d, dt = _http_json(base + "/chat/completions",
                       {"model": "gemma-4-e2b-fast", "max_tokens": 30, "temperature": 0.0,
                        "chat_template_kwargs": {"enable_thinking": False},
                        "messages": [{"role": "user", "content": [
                            {"type": "text", "text": "Welche Farbe hat dieses Bild? Antworte mit einem Wort."},
                            {"type": "image_url", "image_url": {"url": "data:image/png;base64,"
                             + _b64.b64encode(_red_png()).decode()}}]}]},
                       timeout=300, key=key)
    if isinstance(d, dict) and "__err" in d:
        return False, d["__err"]
    try:
        j = json.loads(d)
        txt = (j["choices"][0]["message"]["content"] or "").strip()
        ok = "rot" in txt.lower() or "red" in txt.lower()
        return ok, "%.1fs | %r (erwartet: rot)" % (dt, txt[:60])
    except Exception as e:
        return False, "unerwartete Antwort: %s" % (str(e)[:120])


def _test_embeddings(base, key, text):
    d, dt = _http_json(base + "/embeddings",
                       {"model": "bge-m3", "input": text or "Was ist ein Integral?"},
                       timeout=120, key=key)
    if isinstance(d, dict) and "__err" in d:
        return False, d["__err"]
    try:
        j = json.loads(d)
        n = len(j["data"][0]["embedding"])
        return n == 1024, "%.1fs | dim=%d (App truncatet auf 384)" % (dt, n)
    except Exception as e:
        return False, "unerwartete Antwort: %s" % (str(e)[:120])


def _test_tts(base, key, text):
    d, dt = _http_json(base + "/audio/speech",
                       {"model": "piper-tts", "input": text or "Der Satz des Pythagoras.", "voice": "de"},
                       timeout=120, key=key)
    if isinstance(d, dict) and "__err" in d:
        return False, d["__err"]
    try:
        import io as _io, wave as _wave
        secs = _wave.open(_io.BytesIO(d)).getnframes() / 22050
        ok = d[:4] == b"RIFF" and secs > 0.3
        return ok, "%.1fs | %.1fs Audio, %d KB (WAV)" % (dt, secs, len(d) / 1024)
    except Exception as e:
        return False, "kein gueltiges WAV: %s" % (str(e)[:120])


def _test_image(base, key, prompt):
    import base64 as _b64
    d, dt = _http_json(base + "/chat/completions",
                       {"model": "flux-klein",
                        "messages": [{"role": "user", "content": prompt or "a single red apple, minimalist icon"}]},
                       timeout=900, key=key)
    if isinstance(d, dict) and "__err" in d:
        return False, d["__err"]
    try:
        j = json.loads(d)
        msg = j["choices"][0]["message"]
        url = (msg.get("images") or [{}])[0].get("image_url", {}).get("url", "")
        raw = _b64.b64decode(url.split(",", 1)[1]) if url.startswith("data:image") else b""
        ok = raw[:8] == b"\x89PNG\r\n\x1a\n"
        return ok, "%.0fs | PNG %d KB" % (dt, len(raw) / 1024)
    except Exception as e:
        return False, "kein Bild in Antwort: %s" % (str(e)[:120])


# Pro Server: welche Tests, gegen welche Basis, ob ein Key noetig ist.
# `direct` = Basis-URL fuer den Direkttest am Backend (ohne LiteLLM).
TESTS = {
    "llm": {
        "input": {"label": "Prompt", "value": "Antworte nur mit: OK"},
        "cases": [
            {"id": "chat",   "label": "Chat (Gemma direkt :8080)",   "base": "http://127.0.0.1:8080/v1", "key": None, "fn": "chat"},
            {"id": "vision", "label": "Vision (rotes Testbild)",     "base": "http://127.0.0.1:8080/v1", "key": None, "fn": "vision"},
            {"id": "embed",  "label": "Embeddings (bge-m3 :8081)",   "base": "http://127.0.0.1:8081/v1", "key": None, "fn": "embeddings"},
        ],
    },
    "litellm": {
        "input": {"label": "Prompt", "value": "Sag kurz auf Deutsch: 1+1="},
        "cases": [
            {"id": "chat",   "label": "Chat via Proxy (gemma-4-e2b-fast)", "base": LITELLM_BASE, "key": "master", "fn": "chat"},
            {"id": "embed",  "label": "Embeddings via Proxy (bge-m3)",     "base": LITELLM_BASE, "key": "master", "fn": "embeddings"},
        ],
    },
    "flux": {
        "input": {"label": "Bild-Prompt", "value": "a single red apple, minimalist icon"},
        "cases": [
            {"id": "image", "label": "Bild direkt (flux_server :8083)", "base": "http://127.0.0.1:8083/v1", "key": None, "fn": "image"},
        ],
    },
    "tts": {
        "input": {"label": "Text", "value": "Der Satz des Pythagoras."},
        "cases": [
            {"id": "tts", "label": "Sprachausgabe direkt (:8082)", "base": "http://127.0.0.1:8082/v1", "key": None, "fn": "tts"},
        ],
    },
}

_TEST_FNS = {"chat": _test_chat, "vision": _test_vision,
             "embeddings": _test_embeddings, "tts": _test_tts, "image": _test_image}


def run_test(server_id, case_id, text):
    spec = TESTS.get(server_id)
    if not spec:
        return {"ok": False, "detail": "keine Tests fuer %s" % server_id}
    case = next((c for c in spec["cases"] if c["id"] == case_id), None)
    if not case:
        return {"ok": False, "detail": "unbekannter Test: %s" % case_id}
    key = _litellm_key() if case["key"] == "master" else case["key"]
    try:
        ok, detail = _TEST_FNS[case["fn"]](case["base"], key, text)
    except Exception as e:
        ok, detail = False, "Ausnahme: %s" % (str(e)[:160])
    return {"ok": ok, "detail": detail, "case": case_id}

# Schutz vor gleichzeitigen Start/Stop-Aktionen pro Server.
_action_lock = threading.Lock()

# ---------------------------------------------------------------------------
# bash-Aufloesung (Git-Bash wird gebraucht, weil die Skripte cygpath nutzen).
# ---------------------------------------------------------------------------
def find_bash():
    candidates = [
        "bash",
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\usr\bin\bash.exe"),
    ]
    for c in candidates:
        try:
            p = subprocess.run([c, "-c", "echo ok"], capture_output=True,
                               text=True, timeout=10)
            if p.returncode == 0 and "ok" in p.stdout:
                return c
        except Exception:
            continue
    return None

BASH = find_bash()

# ---------------------------------------------------------------------------
# OPENROUTER_API_KEY in die Umgebung der gestarteten Skripte durchreichen.
# Gemaess Memory: unter git-bash als Machine-Env verfuegbar.
# ---------------------------------------------------------------------------
def ensure_env():
    env = dict(os.environ)
    # LiteLLM-Bindung aus dem LAN-Modus ableiten. run_litellm.sh liest das,
    # die anderen Start-Skripte ignorieren es (harmlos).
    env["LITELLM_HOST"] = litellm_host()
    if not env.get("OPENROUTER_API_KEY"):
        try:
            pw = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "[System.Environment]::GetEnvironmentVariable('OPENROUTER_API_KEY','Machine')"],
                capture_output=True, text=True, timeout=10).stdout.strip()
            if pw:
                env["OPENROUTER_API_KEY"] = pw
        except Exception:
            pass
    return env

# ---------------------------------------------------------------------------
# Health-Check
# ---------------------------------------------------------------------------
def check_health(server):
    """Liefert (state, detail). state: 'up' | 'loading' | 'down'."""
    from urllib.parse import urlparse
    results = []
    for url, ok_code in server["health"]:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=1.5) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception:
            code = None
        port = urlparse(url).port if "://" in url else url
        results.append((str(port), code))
    # Bewertung
    if all(c == ok for _, c in results for ok in [ok_code]):
        # eigentlich: alle == erwartet
        all_ok = all(c == ok_code for (_, c), (_, ok_code) in zip(results, server["health"]))
        if all_ok:
            return "up", results
    any_up = any(c is not None for _, c in results)
    if any_up:
        return "loading", results
    return "down", results

_run_cache = {}  # id -> (ts, bool)
_run_cache_lock = threading.Lock()
RUN_TTL = 3.0  # s


def process_running(server):
    """Ist ueberhaupt ein Prozess da? (unabhaengig vom Health-Port)."""
    now = time.time()
    with _run_cache_lock:
        c = _run_cache.get(server["id"])
        if c and now - c[0] < RUN_TTL:
            return c[1]
    res = _process_running_real(server)
    with _run_cache_lock:
        _run_cache[server["id"]] = (now, res)
    return res


def _process_running_real(server):
    for kind, target in server["kill"]:
        if kind == "exe":
            try:
                out = subprocess.run([tasklist_exe(), "/FI",
                                      "IMAGENAME eq %s" % target, "/NH"],
                                     capture_output=True, text=True,
                                     encoding="cp850", errors="replace",
                                     timeout=10).stdout
                if target.lower() in out.lower():
                    return True
            except Exception:
                pass
        else:
            if py_pids(target):
                return True
    return False

def py_pids(script):
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
             "Where-Object { $_.CommandLine -match '%s' } | "
             "ForEach-Object { $_.ProcessId }" % script],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10).stdout
        return [p for p in out.replace("\r", "").split("\n") if p.strip().isdigit()]
    except Exception:
        return []


def tasklist_exe():
    """tasklist liegt in System32; Pfad explizit, damit es ohne PATH greift."""
    import glob
    cand = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                        "System32", "tasklist.exe")
    return cand if os.path.exists(cand) else "tasklist"

# ---------------------------------------------------------------------------
# Aktionen: start / stop / restart
# ---------------------------------------------------------------------------
def do_start(server):
    if not BASH:
        return False, "bash (Git-Bash) nicht gefunden — Start-Skripte brauchen bash."
    tee_path = LOGS_DIR / os.path.basename(server["tee"])
    tee_f = open(tee_path, "ab", buffering=0)
    # Stdout des Skripts in die Tee-Datei spiegeln (+ Konsole).
    proc = subprocess.Popen(
        [BASH, "-c", server["start"]],
        cwd=str(SCRIPT_DIR),
        stdout=tee_f, stderr=subprocess.STDOUT,
        env=ensure_env(),
        creationflags=0x00000200,  # CREATE_NEW_PROCESS_GROUP
    )
    return True, "Gestartet (PID %s): %s" % (proc.pid, server["start"])

def do_kill(server):
    killed = 0
    msgs = []
    for kind, target in server["kill"]:
        if kind == "exe":
            r = subprocess.run([taskkill_exe(), "/IM", target, "/F"],
                               capture_output=True, text=True,
                               encoding="cp850", errors="replace", timeout=10)
            if r.returncode == 0:
                killed += 1
                msgs.append("beendet %s" % target)
            else:
                msgs.append("kein %s aktiv" % target)
        else:
            pids = py_pids(target)
            for pid in pids:
                r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                    "Stop-Process -Id %s -Force" % pid],
                                   capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=10)
                if r.returncode == 0:
                    killed += 1
                    msgs.append("beendet PID %s (%s)" % (pid, target))
    if killed:
        return True, "; ".join(msgs)
    return True, "nichts zu beenden (" + "; ".join(msgs) + ")"


def taskkill_exe():
    import glob
    cand = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                        "System32", "taskkill.exe")
    return cand if os.path.exists(cand) else "taskkill"

def do_restart(server):
    do_kill(server)
    time.sleep(2)
    return do_start(server)

def handle_action(server_id, action):
    server = SERVER_BY_ID.get(server_id)
    if not server:
        return False, "Unbekannter Server: %s" % server_id
    with _action_lock:
        if action == "start":
            return do_start(server)
        if action == "stop":
            return do_kill(server)
        if action == "restart":
            return do_restart(server)
    return False, "Unbekannte Aktion: %s" % action

def all_status():
    # Health-Checks pro Server parallel, damit ein down-Server (Netz-Timeout)
    # die Gesamtzeit nicht auf Sekunden zieht. process_running ist billig und
    # wird ohnehin nur ueber den Cache aufgerufen.
    states = {}

    def _probe(s):
        states[s["id"]] = check_health(s)

    pool = [threading.Thread(target=_probe, args=(s,), daemon=True) for s in SERVERS]
    for t in pool:
        t.start()
    for t in pool:
        t.join(timeout=3.0)
    out = []
    for s in SERVERS:
        state, detail = states.get(s["id"], ("down", []))
        out.append({
            "id": s["id"],
            "name": s["name"],
            "desc": s["desc"],
            "ports": s["ports"],
            "state": state,
            "running": process_running(s),
            "detail": [{"port": p, "code": c} for p, c in detail],
            "tests": _tests_meta(s["id"]),
        })
    return out


# ---------------------------------------------------------------------------
# Status-Cache: check_health + process_running sind teuer (Netz-Timeouts,
# PowerShell/tasklist-Prozesse). Ohne Cache ruft JEDER SSE-Stream sie jede
# Sekunde auf -> bei mehreren offenen Streams ein PowerShell-Sturm, der den
# Server blockiert (GET /api/status laeuft in Timeouts). Ein gemeinsamer
# Snapshot mit kurzem TTL entkoppelt die Zahl der Clients von der Last.
# ---------------------------------------------------------------------------
_status_cache = {"ts": 0.0, "data": None}
_status_cache_lock = threading.Lock()
STATUS_TTL = 2.0  # s


def all_status_cached():
    now = time.time()
    with _status_cache_lock:
        if _status_cache["data"] is not None and now - _status_cache["ts"] < STATUS_TTL:
            return _status_cache["data"]
        data = all_status()
        _status_cache["data"] = data
        _status_cache["ts"] = time.time()
        return data


def one_status_cached(server_id):
    """Einzelnen Server aus dem gemeinsamen Snapshot ziehen."""
    for s in all_status_cached():
        if s["id"] == server_id:
            return s
    return None


def invalidate_status_cache():
    with _status_cache_lock:
        _status_cache["data"] = None
    with _run_cache_lock:
        _run_cache.clear()


def _tests_meta(server_id):
    """UI-Metadaten der Tests (ohne Key/Basis) fuer die Kartendarstellung."""
    spec = TESTS.get(server_id)
    if not spec:
        return None
    return {
        "input": spec["input"],
        "cases": [{"id": c["id"], "label": c["label"]} for c in spec["cases"]],
    }

# ---------------------------------------------------------------------------
# Log-Streaming (SSE)
# ---------------------------------------------------------------------------
def tail_new(fpath, offset):
    """Liest ab `offset` neue Bytes; liefert (lines, new_offset, truncated?)."""
    try:
        size = fpath.stat().st_size
    except FileNotFoundError:
        return [], 0, False
    if offset > size:  # Datei wurde rotiert/gekuerzt
        offset = 0
    with open(fpath, "rb") as f:
        f.seek(offset)
        data = f.read()
    new_offset = offset + len(data)
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    # letzte, evtl. unvollstaendige Zeile behalten wir nicht als Ganzes
    if lines and lines[-1] == "":
        lines = lines[:-1]
    return lines, new_offset, False

def last_lines(fpath, n=40):
    try:
        with open(fpath, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        return []
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    return lines[-n:]

class ClientGone(Exception):
    """Der Browser hat die (SSE-)Verbindung geschlossen — kein echter Fehler."""

class SSEHandler:
    """Ein SSE-Stream pro Server: sendet Status + neue Log-Zeilen."""

    def __init__(self, server_id, write_fn):
        self.server = SERVER_BY_ID[server_id]
        self.write = write_fn
        self.offsets = {}
        self.Stop = threading.Event()

    def emit(self, obj):
        self.write("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n")

    def run(self):
        # History (letzte Zeilen) senden, dann von aktuellem Ende weitermachen.
        for rel in self.server["logs"]:
            fp = LOGS_DIR.parent / rel if not os.path.isabs(rel) else Path(rel)
            fp = SCRIPT_DIR / rel
            try:
                self.offsets[str(fp)] = fp.stat().st_size
            except FileNotFoundError:
                self.offsets[str(fp)] = 0
            for line in last_lines(fp, 40):
                self.emit({"type": "log", "file": os.path.basename(rel),
                           "line": line, "hist": True})
        while not self.Stop.is_set():
            try:
                # Aus dem gemeinsamen Cache lesen: verhindert, dass jeder Stream
                # sekuendlich eigene Health-/Prozess-Checks (PowerShell!) faehrt.
                st = one_status_cached(self.server["id"]) or {}
                self.emit({"type": "status", "state": st.get("state", "down"),
                           "running": st.get("running", False),
                           "detail": st.get("detail", [])})
                for rel in self.server["logs"]:
                    fp = SCRIPT_DIR / rel
                    key = str(fp)
                    off = self.offsets.get(key, 0)
                    lines, new_off, _ = tail_new(fp, off)
                    self.offsets[key] = new_off
                    for line in lines:
                        if line == "":
                            continue
                        self.emit({"type": "log", "file": os.path.basename(rel),
                                   "line": line, "hist": False})
            except ClientGone:
                self.Stop.set()      # Browser weg -> Stream sauber beenden
                break
            except Exception as e:
                # Ein Emit-Fehler waere selbst wieder ClientGone -> schluckt run()
                try:
                    self.emit({"type": "error", "msg": str(e)})
                except ClientGone:
                    self.Stop.set()
                    break
            self.Stop.wait(1.0)

# ---------------------------------------------------------------------------
# HTTP-Server
# ---------------------------------------------------------------------------
class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """Wie ThreadingHTTPServer, aber ohne Traceback-Spam, wenn der Browser
    eine Verbindung abbricht (typisch bei SSE-Streams / weggeklickten Tabs).
    Diese Fehler passieren beim Lesen der Request-Zeile — also bevor der
    Handler laeuft — und lassen sich daher nur hier abfangen."""

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError,
                            BrokenPipeError)):
            return  # Browser weg — belanglos, still schlucken
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list, str)):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            else:
                body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass  # Browser hat die Verbindung geschlossen — belanglos

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("", "/", "/index.html", "/app", "/manager.html"):
            self._send(200, APP_HTML, "text/html; charset=utf-8")
        elif path == "/api/status":
            self._send(200, all_status_cached())
        elif path == "/api/settings":
            self._send(200, {
                "lan_mode": SETTINGS.get("lan_mode", False),
                "host": litellm_host(),
                "lan_ip": lan_ip(),
            })
        elif path == "/api/stream":
            qs = self._qs()
            sid = qs.get("server", [""])[0]
            if sid not in SERVER_BY_ID:
                self._send(400, {"error": "unknown server"})
                return
            self._stream(sid)
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/api/action":
            try:
                n = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(n) or b"{}")
            except Exception as e:
                self._send(400, {"ok": False, "msg": "bad json: %s" % e})
                return
            ok, msg = handle_action(req.get("server"), req.get("action"))
            invalidate_status_cache()  # nach Start/Stop sofort frisch messen
            self._send(200, {"ok": ok, "msg": msg})
        elif path == "/api/test":
            try:
                n = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(n) or b"{}")
            except Exception as e:
                self._send(400, {"ok": False, "detail": "bad json: %s" % e})
                return
            res = run_test(req.get("server"), req.get("case"), req.get("text", ""))
            self._send(200, res)
        elif path == "/api/settings":
            try:
                n = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(n) or b"{}")
            except Exception as e:
                self._send(400, {"ok": False, "msg": "bad json: %s" % e})
                return
            SETTINGS["lan_mode"] = bool(req.get("lan_mode"))
            _save_settings()
            self._send(200, {
                "ok": True,
                "lan_mode": SETTINGS["lan_mode"],
                "host": litellm_host(),
                "lan_ip": lan_ip(),
            })
        else:
            self._send(404, {"error": "not found"})

    def _qs(self):
        from urllib.parse import parse_qs
        try:
            return parse_qs(self.path.split("?", 1)[1])
        except Exception:
            return {}

    def _stream(self, sid):
        wfile = self.wfile

        def write_fn(data):
            # SSE braucht sofortiges Flush — sonst bleiben die Log-Zeilen
            # im Socket-Puffer haengen und der Browser zeigt nichts.
            if isinstance(data, str):
                data = data.encode("utf-8")
            try:
                wfile.write(data)
                wfile.flush()
            except OSError as e:
                # Browser hat den Stream geschlossen bzw. der Socket wurde
                # bereits geschlossen (BrokenPipe/Reset/Abort, oder WinError
                # 10038 "kein Socket" beim Race mit dem Handler-Teardown).
                # Alle sind OSError-Subklassen -> sauber signalisieren, damit
                # run() den Thread beendet statt einen Traceback zu spucken.
                raise ClientGone() from e

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            wfile.flush()
        except OSError:
            return
        sse = SSEHandler(sid, write_fn)
        t = threading.Thread(target=sse.run, daemon=True)
        t.start()
        try:
            while t.is_alive():
                write_fn(b": ping\n\n")
                time.sleep(15)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, ClientGone):
            pass
        finally:
            sse.Stop.set()

# ---------------------------------------------------------------------------
# HTML-Oberflaeche (eingebettet)
# ---------------------------------------------------------------------------
PAGE_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Local Stack Manager</title>
<style>
  :root { --bg:#0e0e12; --card:#16161d; --card2:#1d1d27; --text:#e6e6ee;
         --muted:#8a8aa0; --acc:#6d4aff; --green:#36d399; --red:#f87272;
         --amber:#fbbd23; --line:#2a2a36; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
  header { padding:1rem 1.25rem; border-bottom:1px solid var(--line);
           display:flex; align-items:center; gap:1rem; flex-wrap:wrap; }
  header h1 { font-size:1.15rem; margin:0; font-weight:650; }
  header .sub { color:var(--muted); font-size:.85rem; }
  .toolbar { margin-left:auto; display:flex; gap:.5rem; flex-wrap:wrap; }
  button { font:inherit; cursor:pointer; border:0; border-radius:8px;
           padding:.5rem .8rem; font-weight:600; color:#fff; background:var(--acc); }
  button.ghost { background:var(--card2); color:var(--text); border:1px solid var(--line); }
  button.danger { background:#3a1d22; color:var(--red); border:1px solid #5a2a30; }
  button:active { transform:translateY(1px); }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(440px,1fr));
          gap:1rem; padding:1.25rem; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:14px;
          overflow:hidden; display:flex; flex-direction:column; }
  .card .head { padding:.85rem 1rem; display:flex; align-items:flex-start; gap:.75rem; }
  .card .title { font-weight:650; font-size:1rem; }
  .card .desc { color:var(--muted); font-size:.78rem; margin-top:.2rem; line-height:1.35; }
  .pill { margin-left:auto; font-size:.72rem; font-weight:700; padding:.25rem .6rem;
          border-radius:999px; white-space:nowrap; }
  .pill.up { background:rgba(54,211,153,.15); color:var(--green); }
  .pill.loading { background:rgba(251,189,35,.15); color:var(--amber); }
  .pill.down { background:rgba(248,114,114,.15); color:var(--red); }
  .ports { font-size:.72rem; color:var(--muted); padding:0 1rem .4rem; }
  .ports code { color:var(--acc); background:var(--card2); padding:.05rem .35rem;
                border-radius:5px; margin-right:.3rem; }
  .actions { display:flex; gap:.5rem; padding:.4rem 1rem .85rem; flex-wrap:wrap; }
  .actions button { flex:1; min-width:80px; padding:.45rem .5rem; font-size:.85rem; }
  .logwrap { border-top:1px solid var(--line); background:#0b0b0f; }
  .logbar { display:flex; align-items:center; gap:.5rem; padding:.4rem .8rem;
            font-size:.72rem; color:var(--muted); border-bottom:1px solid var(--line); }
  .logbar .auto { margin-left:auto; display:flex; align-items:center; gap:.35rem; }
  .log { margin:0; padding:.5rem .8rem; height:240px; overflow:auto;
         font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
         font-size:.74rem; line-height:1.45; white-space:pre-wrap; word-break:break-word; }
  .log .l { color:#c8c8d6; }
  .log .ok { color:var(--green); }
  .log .info { color:#7fb3ff; }
  .log .warn { color:var(--amber); }
  .log .err { color:var(--red); }
  .log .file { color:#6b6b80; }
  footer { padding:.8rem 1.25rem 1.5rem; color:var(--muted); font-size:.74rem; }
  a { color:var(--acc); }
  .lansw { display:flex; align-items:center; gap:.4rem; font-size:.85rem;
           color:var(--muted); cursor:pointer; user-select:none; margin-right:.4rem; }
  .lansw input { accent-color:var(--acc); width:16px; height:16px; cursor:pointer; }
  .lanbanner { margin:0 1rem 1rem; padding:.65rem .9rem; border-radius:8px;
               background:rgba(109,74,255,.12); border:1px solid var(--acc);
               color:var(--text); font-size:.9rem; line-height:1.5; }
  .lanbanner code { background:var(--card2); padding:.05rem .35rem; border-radius:4px; }
</style>
</head>
<body>
<header>
  <div>
    <h1>Local Stack Manager</h1>
    <div class="sub">DocWorm / BuchTutor &mdash; lokaler KI-Stack auf 127.0.0.1</div>
  </div>
  <div class="toolbar">
    <label class="lansw" title="LiteLLM an 0.0.0.0 binden, damit das Handy im WLAN auf :4000 zugreifen kann. Erfordert LiteLLM-Restart.">
      <input type="checkbox" id="lanToggle" onchange="setLan(this.checked)">
      <span>LAN-Zugriff (Handy)</span>
    </label>
    <button class="ghost" onclick="actAll('start')">Alle starten</button>
    <button class="ghost" onclick="actAll('restart')">Alle restart</button>
    <button class="danger" onclick="actAll('stop')">Alle stoppen</button>
  </div>
</header>

<div id="lanBanner" class="lanbanner" style="display:none"></div>

<div class="grid" id="grid"></div>

<footer>
  Start-Reihenfolge empfohlen: <b>LLM-Backends</b> &rarr; <b>LiteLLM</b> &rarr; <b>FLUX</b> / <b>TTS</b>.
  Logs werden live gestreamt (Server-Sent Events). Status pr&uuml;ft die jeweiligen
  <code>/health</code>-Endpunkte. Port der Manager-UI: <span id="mgrport"></span>.
</footer>

<script>
const SERVERS = __SERVERS_JSON__;
const sources = {};   // id -> EventSource
const autoscroll = {}; // id -> bool

function classify(line) {
  const l = line.toLowerCase();
  if (l.includes('[fehler]') || l.includes('[error]') || l.includes('traceback') || l.includes('error:')) return 'err';
  if (l.includes('[warn]') || l.includes('warning')) return 'warn';
  if (l.includes('[ok]') || l.startsWith('ok') || l.includes('bereit') || l.includes('ready')) return 'ok';
  if (l.includes('[info]') || l.startsWith('info')) return 'info';
  return 'l';
}

function buildCards() {
  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  SERVERS.forEach(s => {
    const card = document.createElement('div');
    card.className = 'card';
    card.innerHTML = `
      <div class="head">
        <div>
          <div class="title">${s.name}</div>
          <div class="desc">${s.desc}</div>
        </div>
        <span class="pill down" id="pill-${s.id}">…</span>
      </div>
      <div class="ports" id="ports-${s.id}"></div>
      <div class="actions">
        <button onclick="act('${s.id}','start')">Start</button>
        <button class="ghost" onclick="act('${s.id}','restart')">Restart</button>
        <button class="danger" onclick="act('${s.id}','stop')">Kill</button>
      </div>
      <div class="logwrap">
        <div class="logbar">
          <span>Live-Log</span>
          <span class="auto"><label><input type="checkbox" checked onchange="autoscroll['${s.id}']=this.checked"> auto-scroll</label></span>
        </div>
        <pre class="log" id="log-${s.id}"></pre>
      </div>`;
    grid.appendChild(card);
    autoscroll[s.id] = true;
    openStream(s.id);
  });
  refreshStatus();
  setInterval(refreshStatus, 4000);
}

function openStream(id) {
  if (sources[id]) sources[id].close();
  const es = new EventSource('/api/stream?server=' + id);
  sources[id] = es;
  const logEl = document.getElementById('log-' + id);
  es.onmessage = (e) => {
    let obj; try { obj = JSON.parse(e.data); } catch { return; }
    if (obj.type === 'status') {
      const pill = document.getElementById('pill-' + id);
      pill.className = 'pill ' + obj.state;
      pill.textContent = obj.state === 'up' ? 'ONLINE' : obj.state === 'loading' ? 'LÄDT…' : 'OFFLINE';
      const ports = document.getElementById('ports-' + id);
      if (obj.detail && obj.detail.length) {
        ports.innerHTML = obj.detail.map(d =>
          `<code>:${d.port}</code>${d.code ? 'HTTP '+d.code : 'kein Response'}`).join(' &nbsp; ');
      }
    } else if (obj.type === 'log') {
      const span = document.createElement('span');
      span.className = classify(obj.line);
      const prefix = obj.hist ? '' : '';
      span.textContent = (obj.file ? '' : '') + obj.line + '\\n';
      logEl.appendChild(span);
      // Datei-Tag dezent voranstellen
      if (obj.file) {
        const f = document.createElement('span');
        f.className = 'file';
        f.textContent = '[' + obj.file + '] ';
        span.insertBefore(f, span.firstChild);
      }
      const max = 4000;
      while (logEl.childNodes.length > max) logEl.removeChild(logEl.firstChild);
      if (autoscroll[id]) logEl.scrollTop = logEl.scrollHeight;
    } else if (obj.type === 'error') {
      const span = document.createElement('span');
      span.className = 'err';
      span.textContent = '[stream-error] ' + obj.msg + '\\n';
      logEl.appendChild(span);
    }
  };
  es.onerror = () => { /* automatisch neu verbunden vom Browser */ };
}

async function refreshStatus() {
  try {
    const r = await fetch('/api/status');
    const data = await r.json();
    data.forEach(s => {
      const pill = document.getElementById('pill-' + s.id);
      if (!pill) return;
      pill.className = 'pill ' + s.state;
      pill.textContent = s.state === 'up' ? 'ONLINE' : s.state === 'loading' ? 'LÄDT…' : 'OFFLINE';
      const ports = document.getElementById('ports-' + s.id);
      if (ports && s.detail) {
        ports.innerHTML = s.detail.map(d =>
          `<code>:${d.port}</code>${d.code ? 'HTTP ' + d.code : 'kein Response'}`).join(' &nbsp; ');
      }
    });
  } catch (e) {}
}

async function act(id, action) {
  const btn = event ? event.target : null;
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  try {
    await fetch('/api/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ server: id, action })
    });
  } catch (e) {}
  // SSE neu oeffnen, damit nur die frischen Logs (ab jetzt) angezeigt werden.
  setTimeout(() => openStream(id), 400);
  if (btn) { btn.disabled = false; btn.textContent = action === 'stop' ? 'Kill' : (action === 'restart' ? 'Restart' : 'Start'); }
  setTimeout(refreshStatus, 1500);
}

async function actAll(action) {
  for (const s of SERVERS) {
    await act(s.id, action);
    await new Promise(r => setTimeout(r, action === 'start' ? 1500 : 300));
  }
}

function applyLan(s) {
  const cb = document.getElementById('lanToggle');
  if (cb) cb.checked = !!s.lan_mode;
  const b = document.getElementById('lanBanner');
  if (!b) return;
  if (s.lan_mode) {
    b.style.display = '';
    b.innerHTML = 'LAN-Zugriff <b>aktiv</b> &mdash; LiteLLM bindet an <code>0.0.0.0</code>. '
      + 'Vom Handy im WLAN: <code>http://' + s.lan_ip + ':4000/v1</code>. '
      + '<b>LiteLLM einmal neu starten (Restart)</b>, damit die Bindung greift. '
      + 'Firewall ggf. mit <code>firewall_open_4000.cmd</code> oeffnen.';
  } else {
    b.style.display = 'none';
  }
}

async function loadSettings() {
  try { applyLan(await (await fetch('/api/settings')).json()); } catch (e) {}
}

async function setLan(on) {
  try {
    const r = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lan_mode: on })
    });
    applyLan(await r.json());
  } catch (e) {}
}

document.getElementById('mgrport').textContent = location.port || '8800';
buildCards();
loadSettings();
</script>
</body>
</html>"""

# App-HTML laden: externe Datei bevorzugt, sonst eingebettete PAGE_HTML (Fallback).
APP_HTML_PATH = SCRIPT_DIR / "stack_manager.html"

def load_app_html():
    if APP_HTML_PATH.exists():
        return APP_HTML_PATH.read_text(encoding="utf-8")
    return PAGE_HTML.replace("__SERVERS_JSON__",
                             json.dumps([{"id": s["id"], "name": s["name"], "desc": s["desc"]}
                                         for s in SERVERS], ensure_ascii=False))

APP_HTML = load_app_html()


def main():
    global MANAGER_PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8800)
    a = ap.parse_args()
    MANAGER_PORT = a.port
    if not BASH:
        print("[warn] bash (Git-Bash) nicht gefunden — Start ueber die UI wird fehlschlagen.",
              flush=True)
    print("[info] Local Stack Manager auf http://127.0.0.1:%d" % MANAGER_PORT, flush=True)
    print("[info] Komponenten: " + ", ".join(s["id"] for s in SERVERS), flush=True)
    httpd = QuietThreadingHTTPServer(("127.0.0.1", MANAGER_PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[info] Manager beendet.", flush=True)


if __name__ == "__main__":
    main()
