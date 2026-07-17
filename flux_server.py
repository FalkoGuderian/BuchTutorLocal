#!/usr/bin/env python3
"""OpenAI-kompatibler Bildgenerierungs-Server auf Basis von stable-diffusion.cpp.

Gibt dem lokalen DocWorm-Stack ein Bild-Backend (FLUX.2 Klein 4B), ohne dass
app.html geaendert werden muss: Die App erzeugt Bilder ueber
POST /chat/completions mit einem Bildmodell (OpenRouter/Gemini-Stil) und liest
das Bild aus message.images[] (extractImageFromResponse, app.html ~36407).
Genau diese Form liefert dieser Shim.

    python flux_server.py --sd-bin sdcpp/sd-server.exe --model-dir "D:/LM Studio Models/flux2-klein-4b"

Architektur:

    app.html --> LiteLLM :4000 --> flux_server :8083 --> sd-server :8084 (intern)
                 (model flux-klein)  (dieser Shim)        (sd.cpp, Modelle warm)

Warum ein Shim UND sd-server:
  * sd-server (leejet/stable-diffusion.cpp) kennt nur /v1/images/generations
    (Body: prompt/size), nicht /v1/chat/completions. Die App ruft aber
    chat/completions. Der Shim uebersetzt zwischen beiden.
  * sd-server haelt die ~5 GB Gewichte geladen (persistent). sd-cli.exe wuerde
    sie pro Bild neu laden (auf CPU zig Sekunden Overhead je Aufruf).

Bewusst nur Standardbibliothek: kein FastAPI/uvicorn, damit der Server
unabhaengig vom LiteLLM-venv startet und Fehler direkt lesbar sind (wie
piper_server.py).

Antwortform (identisch zu OpenRouter-Bildmodellen, damit die App sie ohne
Aenderung versteht):

    choices[0].message.images = [{"type":"image_url",
                                  "image_url":{"url":"data:image/png;base64,..."}}]
"""
import argparse, base64, json, os, subprocess, sys, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.stdout.reconfigure(encoding="utf-8")

# Wird in main() aus CLI-Args gefuellt.
SD_BASE = "http://127.0.0.1:8084"   # interner sd-server
MODEL_ID = "flux-klein"             # ID, die die App/LiteLLM schickt
# 256 statt 1024: Auf der CPU kostet ein Step Minuten; die Pixelzahl (und damit
# die Zeit) skaliert quadratisch mit der Kantenlaenge. 256x256 ist der bewusste
# Tempo-Default (Qualitaet leidet, FLUX.2 ist auf ~1 MP trainiert). Groesse ist
# der Haupt-Tempohebel -- ueber --size / FLUX_SIZE anhebbar.
DEFAULT_SIZE = "256x256"
GEN_TIMEOUT = 900                   # CPU-Bildgenerierung darf lange dauern
_sd_proc = None                     # sd-server-Kindprozess
_sd_ready = threading.Event()


def _extract_prompt(messages):
    """Prompt-Text aus dem letzten User-Turn ziehen.

    content ist entweder ein String oder eine Liste von Parts
    ({"type":"text","text":...}). Fallback: letzte Nachricht ueberhaupt.
    """
    def text_of(content):
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(p.get("text", "") for p in content
                            if isinstance(p, dict) and p.get("type") == "text")
        return ""
    for m in reversed(messages or []):
        if m.get("role") == "user":
            t = text_of(m.get("content")).strip()
            if t:
                return t
    for m in reversed(messages or []):
        t = text_of(m.get("content")).strip()
        if t:
            return t
    return ""


def _sd_generate(prompt, size):
    """sd-server /v1/images/generations aufrufen, base64-PNG zurueckgeben."""
    body = json.dumps({
        "prompt": prompt,
        "n": 1,
        "size": size,
        "output_format": "png",
    }).encode()
    req = urllib.request.Request(SD_BASE + "/v1/images/generations", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=GEN_TIMEOUT) as r:
        data = json.loads(r.read())
    imgs = data.get("data") or []
    if not imgs or not imgs[0].get("b64_json"):
        raise RuntimeError("sd-server lieferte kein Bild: %s" % str(data)[:200])
    return imgs[0]["b64_json"]


def _chat_response(b64png, model):
    """OpenRouter-formige Chat-Antwort mit dem Bild in message.images[]."""
    ts = int(time.time())
    return {
        "id": "chatcmpl-flux-%d" % ts,
        "object": "chat.completion",
        "created": ts,
        "model": model or MODEL_ID,
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "message": {
                "role": "assistant",
                "content": "",
                "images": [{
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + b64png},
                }],
            },
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Normal: der Client (Browser/curl) hat den langen Bild-Request
            # abgebrochen, bevor die Antwort fertig war. Kein Serverfehler.
            print("[info] Client hat die Verbindung vor der Antwort geschlossen", flush=True)

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    def do_GET(self):
        p = self.path.rstrip("/")
        if p in ("/health", "/v1/health"):
            self._send(200 if _sd_ready.is_set() else 503,
                       {"status": "ok" if _sd_ready.is_set() else "loading",
                        "backend": SD_BASE})
        elif p in ("/v1/models", "/models"):
            self._send(200, {"object": "list", "data": [
                {"id": MODEL_ID, "object": "model", "owned_by": "stable-diffusion.cpp"}]})
        else:
            self._send(404, {"error": {"message": "Unbekannter Pfad: %s" % self.path}})

    def do_POST(self):
        p = self.path.rstrip("/")
        if p in ("/v1/images/generations", "/images/generations"):
            self._passthrough_images()
            return
        if p not in ("/v1/chat/completions", "/chat/completions"):
            self._send(404, {"error": {"message": "Unbekannter Pfad: %s" % self.path}})
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._send(400, {"error": {"message": "Ungueltiges JSON: %s" % e}})
            return

        if not _sd_ready.is_set():
            self._send(503, {"error": {"message": "sd-server laedt noch die Modelle"}})
            return

        prompt = _extract_prompt(req.get("messages"))
        if not prompt:
            self._send(400, {"error": {"message": "Kein Prompt in messages gefunden"}})
            return

        size = req.get("size") or DEFAULT_SIZE
        t0 = time.time()
        try:
            b64 = _sd_generate(prompt, size)
        except Exception as e:
            print("[fehler] Generierung: %s" % e, flush=True)
            self._send(500, {"error": {"message": str(e)}})
            return
        dt = time.time() - t0
        print("[ok] Bild %s in %.1fs (%d Zeichen Prompt)" % (size, dt, len(prompt)), flush=True)
        self._send(200, _chat_response(b64, req.get("model")))

    def _passthrough_images(self):
        """Direkter OpenAI-Images-Pfad (fuer curl/Tests) -> sd-server durchreichen."""
        if not _sd_ready.is_set():
            self._send(503, {"error": {"message": "sd-server laedt noch die Modelle"}})
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            prompt = (req.get("prompt") or "").strip()
            if not prompt:
                self._send(400, {"error": {"message": "'prompt' fehlt"}})
                return
            b64 = _sd_generate(prompt, req.get("size") or DEFAULT_SIZE)
            self._send(200, {"created": int(time.time()), "output_format": "png",
                             "data": [{"b64_json": b64}]})
        except Exception as e:
            self._send(500, {"error": {"message": str(e)}})


def _start_sd_server(sd_bin, diff, vae, llm, sd_port, steps, cfg, extra, log_path):
    """sd-server.exe als Kindprozess starten und auf Bereitschaft warten."""
    global _sd_proc
    cmd = [sd_bin,
           "--diffusion-model", diff,
           "--vae", vae,
           "--llm", llm,
           "--vae-format", "flux2",
           "--scheduler", "flux2",
           "--sampling-method", "euler",
           "--cfg-scale", str(cfg),
           "--steps", str(steps),
           "--diffusion-fa",
           "-l", "127.0.0.1",
           "--listen-port", str(sd_port)]
    if extra:
        cmd += extra.split()
    logf = open(log_path, "w", encoding="utf-8", errors="replace")
    print("[info] Starte sd-server: %s" % " ".join('"%s"' % c if " " in c else c for c in cmd), flush=True)
    _sd_proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT)

    # Auf Modell-Ladung warten. Auf CPU kann das eine Weile dauern; /v1/models
    # antwortet erst, wenn der Server steht.
    def wait():
        for _ in range(600):  # bis 600s
            if _sd_proc.poll() is not None:
                print("[fehler] sd-server beendete sich beim Start (siehe %s)" % log_path, flush=True)
                return
            try:
                with urllib.request.urlopen(SD_BASE + "/v1/models", timeout=2) as r:
                    if r.status == 200:
                        _sd_ready.set()
                        print("[ok] sd-server bereit auf %s" % SD_BASE, flush=True)
                        return
            except Exception:
                pass
            time.sleep(1)
        print("[warn] sd-server nicht innerhalb 600s bereit", flush=True)
    threading.Thread(target=wait, daemon=True).start()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8083)
    ap.add_argument("--sd-bin", required=True, help="Pfad zu sd-server.exe")
    ap.add_argument("--sd-port", type=int, default=8084, help="interner Port fuer sd-server")
    ap.add_argument("--model-dir", required=True, help="Ordner mit Diffusion/Encoder/VAE")
    ap.add_argument("--diffusion", default="flux-2-klein-4b-Q4_0.gguf")
    ap.add_argument("--encoder", default="Qwen3-4B-Q4_K_M.gguf")
    ap.add_argument("--vae", default="flux2-vae.safetensors")
    ap.add_argument("--steps", type=int, default=4)
    ap.add_argument("--cfg", type=float, default=1.0)
    ap.add_argument("--size", default="256x256",
                    help="Standardgroesse WxH; kleiner = schneller (der Haupt-Tempohebel)")
    ap.add_argument("--model-id", default="flux-klein")
    ap.add_argument("--sd-extra", default="", help="zusaetzliche sd-server-Flags")
    a = ap.parse_args()

    SD_BASE = "http://127.0.0.1:%d" % a.sd_port
    MODEL_ID = a.model_id
    DEFAULT_SIZE = a.size

    md = a.model_dir
    diff = os.path.join(md, a.diffusion)
    enc = os.path.join(md, a.encoder)
    vae = os.path.join(md, a.vae)
    for label, path in [("Diffusionsmodell", diff), ("Text-Encoder", enc), ("VAE", vae),
                        ("sd-server", a.sd_bin)]:
        if not os.path.isfile(path):
            print("[fehler] %s fehlt: %s" % (label, path))
            sys.exit(1)

    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    _start_sd_server(a.sd_bin, diff, vae, enc, a.sd_port, a.steps, a.cfg, a.sd_extra,
                     os.path.join(log_dir, "sd-server.log"))

    print("[info] Bild-Modell-ID: %s   Standardgroesse: %s   Steps: %d   CFG: %.1f"
          % (MODEL_ID, DEFAULT_SIZE, a.steps, a.cfg), flush=True)
    print("[info] POST http://%s:%d/v1/chat/completions  (Bild in message.images[])"
          % (a.host, a.port), flush=True)
    try:
        ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
    finally:
        if _sd_proc and _sd_proc.poll() is None:
            _sd_proc.terminate()
