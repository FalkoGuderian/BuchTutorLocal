#!/usr/bin/env python3
"""OpenAI-kompatibler TTS-Server auf Basis von Piper (lokal, CPU).

Stellt POST /v1/audio/speech bereit — genau den Endpunkt, den app.html (~23653)
und LiteLLM erwarten. Damit bekommt der lokale DocWorm-Stack ein Vorlese-Backend,
ohne C++-Toolchain (qwentts.cpp braeuchte VS Build Tools + CMake + Vulkan SDK).

    python piper_server.py [--port 8082] [--voices-dir voices]

Bewusst nur Standardbibliothek + piper: kein FastAPI/uvicorn, damit der Server
unabhaengig vom LiteLLM-venv-Zustand startet und Fehler direkt lesbar sind.

WICHTIG — was app.html tatsaechlich anfragt (_ttsReadAloudStream, ~23653):

    {"model": ..., "input": ..., "voice": ..., "response_format": "pcm", "stream": true}

Die App liest den Body per resp.body.getReader() und interpretiert ihn als rohes
Int16-PCM (mono). Ein fertiges WAV genuegt hier NICHT.

WARUM 24000 Hz (--rate): Die App liest die Sample-Rate aus dem Content-Type
("audio/pcm;rate=..."), faellt aber auf 24000 zurueck, wenn sie fehlt. Und genau
das passiert hinter LiteLLM: der Proxy ueberschreibt den Content-Type hart mit
"audio/mpeg" — die Rate-Angabe geht verloren (verifiziert 15.07.2026). Piper
liefert nativ 22050 Hz; ungeresampelt liefe die Stimme darum ueber LiteLLM ~9 %
zu schnell. Deshalb resampelt der Server auf 24000 Hz und trifft den Fallback
exakt — direkt UND ueber den Proxy.

Formate: pcm (Stream, fuer die App), wav (Default, fuer curl/Tests), mp3 (ffmpeg).
"""
import argparse, io, json, subprocess, shutil, sys, threading, wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from piper import PiperVoice
from piper.config import SynthesisConfig

sys.stdout.reconfigure(encoding="utf-8")

# OpenAI-Stimmennamen auf die lokale Standardstimme abbilden: DocWorm/LiteLLM
# schicken u. U. "alloy" & Co. Unbekannte Namen landen ebenfalls beim Default,
# damit ein Aufruf nie an der Stimme scheitert.
OPENAI_VOICES = {"alloy", "echo", "fable", "onyx", "nova", "shimmer", "ash", "coral", "sage"}

# Sprach-Kurzformen: DocWorm ist zweisprachig (i18n DE/EN, englische Kurse).
# So kann die App voice:"en" schicken, ohne Piper-Dateinamen zu kennen.
LANG_ALIASES = {
    "de": "de_DE-thorsten-medium", "de_DE": "de_DE-thorsten-medium", "german": "de_DE-thorsten-medium",
    "en": "en_US-lessac-medium", "en_US": "en_US-lessac-medium", "english": "en_US-lessac-medium",
}

_voices = {}
_lock = threading.Lock()
VOICES_DIR = Path("voices")
DEFAULT_VOICE = "de_DE-thorsten-medium"   # Deutsch ist Standard
OUT_RATE = 24000                          # trifft den Fallback von app.html, s. Modulkopf


def available():
    return sorted(p.stem for p in VOICES_DIR.glob("*.onnx"))


def get_voice(name):
    """Laedt eine Stimme lazy und cached sie (ONNX-Session ist teuer).

    Reihenfolge: exakter Dateiname > Sprach-Alias (de/en) > Default (Deutsch).
    """
    name = (name or "").strip()
    if name not in available():
        name = LANG_ALIASES.get(name.lower(), DEFAULT_VOICE if not name or name in OPENAI_VOICES
                                else DEFAULT_VOICE)
    if name not in available():
        name = DEFAULT_VOICE
    with _lock:
        if name not in _voices:
            path = VOICES_DIR / (name + ".onnx")
            if not path.exists():
                raise FileNotFoundError("Stimme fehlt: %s" % path)
            print("[info] Lade Stimme: %s" % name, flush=True)
            _voices[name] = PiperVoice.load(path)
        return _voices[name], name


def to_mp3(wav_bytes):
    """WAV -> MP3 via ffmpeg. None, wenn ffmpeg fehlt."""
    if not shutil.which("ffmpeg"):
        return None
    p = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                        "-f", "mp3", "-b:a", "128k", "pipe:1"],
                       input=wav_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return p.stdout if p.returncode == 0 and p.stdout else None


def syn_config(speed):
    if speed and float(speed) != 1.0:
        # length_scale ist die Dauer pro Phonem: groesser = langsamer.
        return SynthesisConfig(length_scale=1.0 / float(speed))
    return None


def synth(text, voice_name, speed):
    """Fertiges WAV am Stueck (fuer response_format wav/mp3)."""
    voice, used = get_voice(voice_name)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        voice.synthesize_wav(text, w, syn_config=syn_config(speed))
    return buf.getvalue(), used


def resample_int16(samples, src_rate, dst_rate, carry):
    """Lineare Interpolation src_rate -> dst_rate auf Int16-Samples.

    `carry` ist das letzte Sample des Vorgaengerchunks: ohne diesen Anker
    entstuende an jeder Chunk-Grenze ein Sprung (hoerbares Knacken). Reicht hier,
    weil nur hochgesampelt wird (22050 -> 24000): kein Aliasing, kein Tiefpass
    noetig. Rueckgabe: (resampelte Samples, neues carry).
    """
    import numpy as np
    if src_rate == dst_rate or samples.size == 0:
        return samples, (samples[-1] if samples.size else carry)
    src = samples if carry is None else np.concatenate(([carry], samples))
    offset = 0.0 if carry is None else 1.0
    n_out = int(round((src.size - offset) * dst_rate / src_rate))
    if n_out <= 0:
        return np.empty(0, dtype=np.int16), (samples[-1] if samples.size else carry)
    pos = offset + np.arange(n_out) * (src_rate / dst_rate)
    out = np.interp(pos, np.arange(src.size), src.astype(np.float32))
    return np.clip(out, -32768, 32767).astype(np.int16), samples[-1]


def synth_chunks(text, voice_name, speed, dst_rate):
    """Generator: (int16-bytes, sample_rate) pro Chunk — fuer den PCM-Stream.

    Piper synthetisiert satzweise, der erste Chunk kommt also deutlich vor dem
    Ende. Genau davon lebt der onFirstByte-Pfad der App (Time-to-first-audio).
    """
    import numpy as np
    voice, used = get_voice(voice_name)
    cfg = syn_config(speed)
    yield None, used  # erster yield meldet nur die tatsaechlich genutzte Stimme
    carry = None
    for chunk in voice.synthesize(text, syn_config=cfg):
        arr = np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16)
        out, carry = resample_int16(arr, chunk.sample_rate, dst_rate, carry)
        yield out.tobytes(), dst_rate


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        pass  # Zugriffslog unterdruecken; relevante Zeilen loggen wir selbst.

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204, b"", "text/plain")

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", "/v1/health"):
            self._send(200, {"status": "ok", "voices": available()})
        elif self.path.rstrip("/") in ("/v1/models", "/models"):
            self._send(200, {"object": "list", "data": [
                {"id": v, "object": "model", "owned_by": "piper"} for v in available()]})
        else:
            self._send(404, {"error": "Unbekannter Pfad: %s" % self.path})

    def do_POST(self):
        if self.path.rstrip("/") not in ("/v1/audio/speech", "/audio/speech"):
            self._send(404, {"error": "Unbekannter Pfad: %s" % self.path})
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._send(400, {"error": {"message": "Ungueltiges JSON: %s" % e}})
            return

        text = (req.get("input") or "").strip()
        if not text:
            self._send(400, {"error": {"message": "'input' fehlt oder ist leer"}})
            return

        fmt = (req.get("response_format") or "wav").lower()

        # pcm: der Pfad, den app.html nutzt (response_format:"pcm", stream:true).
        # Rohes Int16-PCM, chunked gestreamt, Rate im Content-Type.
        if fmt == "pcm":
            self._stream_pcm(text, req)
            return

        try:
            wav, used = synth(text, req.get("voice"), req.get("speed", 1.0))
        except Exception as e:
            print("[fehler] Synthese: %s" % e, flush=True)
            self._send(500, {"error": {"message": str(e)}})
            return

        print("[ok] %d Zeichen -> %s, %.1f KB (%s)" % (len(text), used, len(wav) / 1024, fmt), flush=True)

        if fmt == "mp3":
            mp3 = to_mp3(wav)
            if mp3:
                self._send(200, mp3, "audio/mpeg")
                return
            # Ohne ffmpeg lieber WAV liefern als den Aufruf scheitern lassen.
            print("[warn] mp3 angefragt, ffmpeg fehlt -> sende wav", flush=True)
        self._send(200, wav, "audio/wav")

    def _stream_pcm(self, text, req):
        try:
            gen = synth_chunks(text, req.get("voice"), req.get("speed", 1.0), OUT_RATE)
            _, used = next(gen)          # Stimme aufloesen, bevor Header rausgehen
            first = next(gen, None)      # ersten Chunk holen -> Rate steht fest
        except Exception as e:
            print("[fehler] Synthese: %s" % e, flush=True)
            self._send(500, {"error": {"message": str(e)}})
            return

        if first is None:
            self._send(500, {"error": {"message": "Kein Audio erzeugt"}})
            return

        data, rate = first
        # Die Rate MUSS mit: app.html liest sie aus dem Content-Type und faellt
        # sonst auf 24000 zurueck -> Piper (22050) klaenge zu schnell.
        self.send_response(200)
        self.send_header("Content-Type", "audio/pcm;rate=%d" % rate)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        total = 0
        try:
            for data, _rate in [(data, rate)] + list(gen):
                if not data:
                    continue
                self.wfile.write(b"%X\r\n" % len(data) + data + b"\r\n")
                self.wfile.flush()
                total += len(data)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Normal: die App bricht den Stream ab, wenn der Nutzer stoppt.
            print("[info] PCM-Stream vom Client abgebrochen", flush=True)
            return
        print("[ok] %d Zeichen -> %s, %.1f KB pcm @%d Hz (%.1fs)"
              % (len(text), used, total / 1024, rate, total / 2 / rate), flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8082)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--voices-dir", default="voices")
    ap.add_argument("--rate", type=int, default=24000,
                    help="Ausgabe-Sample-Rate fuer pcm (Default 24000 = Fallback von app.html; "
                         "0 = nativ 22050 ohne Resampling)")
    a = ap.parse_args()
    VOICES_DIR = Path(a.voices_dir)
    OUT_RATE = a.rate or 22050

    if not available():
        print("[fehler] Keine Stimmen in %s/" % VOICES_DIR)
        print("  Nachholen:  python -m piper.download_voices de_DE-thorsten-medium --download-dir %s" % VOICES_DIR)
        sys.exit(1)

    print("[info] Stimmen: %s" % ", ".join(available()))
    print("[info] Default: %s (Deutsch)" % DEFAULT_VOICE)
    print("[info] Kurzformen: voice=\"de\" | \"en\" | exakter Dateiname")
    print("[info] pcm-Ausgabe: %d Hz%s" % (OUT_RATE, " (resampelt von 22050)" if OUT_RATE != 22050 else " (nativ)"))
    print("[info] POST http://%s:%d/v1/audio/speech" % (a.host, a.port))
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
