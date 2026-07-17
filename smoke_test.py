#!/usr/bin/env python3
"""Smoke-Test: prueft die DocWorm-Endpunkte gegen den lokalen LiteLLM-Proxy (:4000).

    python smoke_test.py [--audio PFAD.wav]

Voraussetzung: ./run_llm.sh und ./run_litellm.sh laufen.

Die Tests pruefen INHALTE, nicht nur HTTP 200 — ein Vision-Call mit einem zu
kleinen Bild liefert sonst brav 200, obwohl das Modell gar kein Bild gesehen hat.
"""
import argparse, base64, json, os, struct, sys, time, urllib.request, urllib.error, zlib

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:4000/v1")
KEY = "sk-local-llm"
sys.stdout.reconfigure(encoding="utf-8")
results = []


def call(path, payload, timeout=900):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + KEY})
    t0 = time.time()
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read()), time.time() - t0
    except urllib.error.HTTPError as e:
        return {"__err": "HTTP %d: %s" % (e.code, e.read().decode()[:160])}, time.time() - t0
    except Exception as e:
        return {"__err": str(e)[:160]}, time.time() - t0


def check(label, cond, detail=""):
    print("  %-9s %-12s %s" % ("[OK]" if cond else "[FEHLER]", label, detail))
    results.append(cond)
    return cond


def red_png(size=224):
    """224x224 rotes PNG ohne Pillow. Kleiner als clip.vision.patch_size (16) waere
    wirkungslos — das Modell bekaeme dann gar kein Bild."""
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * size for _ in range(size))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


ap = argparse.ArgumentParser()
ap.add_argument("--audio", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                               "_audio_30s.wav"))
ap.add_argument("--image", action="store_true",
                help="zusaetzlich die Bildgenerierung (flux-klein) testen — dauert auf CPU Minuten")
args = ap.parse_args()

print("\n1) Chat (gemma-4-e2b-fast)")
d, dt = call("/chat/completions", {"model": "gemma-4-e2b-fast", "max_tokens": 40,
                                   "messages": [{"role": "user", "content": "Antworte nur mit: OK"}]})
if "__err" in d:
    check("chat", False, d["__err"])
else:
    txt = d["choices"][0]["message"]["content"].strip()
    check("chat", bool(txt), "%.1fs | %r" % (dt, txt[:50]))

print("\n2) Vision (mmproj-Vision-Tower)")
d, dt = call("/chat/completions", {"model": "gemma-4-e2b-fast", "max_tokens": 30, "temperature": 0.0,
                                   "messages": [{"role": "user", "content": [
                                       {"type": "text", "text": "Welche Farbe hat dieses Bild? Antworte mit einem Wort."},
                                       {"type": "image_url", "image_url": {"url": "data:image/png;base64,"
                                        + base64.b64encode(red_png()).decode()}}]}]})
if "__err" in d:
    check("vision", False, d["__err"])
else:
    txt = d["choices"][0]["message"]["content"].strip()
    # Inhaltspruefung: das Bild ist rot. Alles andere heisst, es kam nichts an.
    check("vision", "rot" in txt.lower() or "red" in txt.lower(), "%.1fs | %r" % (dt, txt[:50]))

print("\n3) Audio-Transkription (input_audio)")
try:
    b64 = base64.b64encode(open(args.audio, "rb").read()).decode()
    d, dt = call("/chat/completions", {"model": "gemma-4-e2b-fast", "max_tokens": 256, "temperature": 0.0,
                                       "messages": [{"role": "user", "content": [
                                           {"type": "text", "text": "Transcribe the following speech segment in its "
                                            "original language. Only output the transcription, with no newlines."},
                                           {"type": "input_audio", "input_audio": {"data": b64, "format": "wav"}}]}]})
    if "__err" in d:
        check("audio", False, d["__err"])
    else:
        txt = (d["choices"][0]["message"]["content"] or "").strip()
        pt = d.get("usage", {}).get("prompt_tokens", 0)
        # 25 Audio-Tokens/Sekunde -> ein 14s-Clip muss deutlich >100 prompt_tokens erzeugen.
        # Ohne Audio waere der Prompt ~20 Tokens: dann wurde der Clip verworfen.
        check("audio", bool(txt) and pt > 100, "%.1fs | %d prompt-tokens" % (dt, pt))
        print("            -> " + txt[:130])
except FileNotFoundError:
    check("audio", False, "Datei nicht gefunden: " + args.audio)

print("\n4) Embeddings (bge-m3)")
d, dt = call("/embeddings", {"model": "bge-m3", "input": "Was ist ein Integral?"})
if "__err" in d:
    check("embeddings", False, d["__err"])
else:
    n = len(d["data"][0]["embedding"])
    check("embeddings", n == 1024, "%.1fs | dim=%d (app.html truncatet auf 384)" % (dt, n))

print("\n5) TTS (/audio/speech, Piper) — WAV")
for lang, text in (("de", "Der Satz des Pythagoras."), ("en", "The tea ceremony.")):
    req = urllib.request.Request(BASE + "/audio/speech",
                                 data=json.dumps({"model": "piper-tts", "input": text, "voice": lang}).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + KEY})
    t0 = time.time()
    try:
        audio = urllib.request.urlopen(req, timeout=120).read()
        dt = time.time() - t0
        # Inhaltspruefung: echtes RIFF/WAVE mit plausibler Laenge, nicht nur HTTP 200.
        # Ein JSON-Fehler waere sonst ebenfalls "erfolgreiche" Bytes.
        import io as _io
        import wave as _wave
        secs = _wave.open(_io.BytesIO(audio)).getnframes() / 22050
        check("tts " + lang, audio[:4] == b"RIFF" and secs > 0.3,
              "%.1fs | %.1fs Audio, %d KB" % (dt, secs, len(audio) / 1024))
    except Exception as e:
        check("tts " + lang, False, str(e)[:110])

print("\n6) TTS als PCM-Stream — der Pfad, den app.html nutzt")
# app.html (_ttsReadAloudStream) schickt response_format:"pcm" + stream:true und
# liest rohes Int16-PCM. Ein WAV waere hier ein stiller Fehler: die RIFF-Header-
# Bytes landeten als Krachen im Audio. Darum genau diesen Pfad testen.
req = urllib.request.Request(BASE + "/audio/speech", data=json.dumps({
    "model": "piper-tts", "input": "Der Satz des Pythagoras beschreibt rechtwinklige Dreiecke.",
    "voice": "de", "response_format": "pcm", "stream": True}).encode(),
    headers={"Content-Type": "application/json", "Authorization": "Bearer " + KEY})
try:
    t0 = time.time()
    raw = urllib.request.urlopen(req, timeout=120).read()
    dt = time.time() - t0
    import array as _array
    a = _array.array("h")
    a.frombytes(raw[:len(raw) // 2 * 2])
    peak = max(max(a), abs(min(a))) if len(a) else 0
    # 24000 Hz ist der Fallback von app.html, wenn der Content-Type keine Rate
    # nennt — und LiteLLM ueberschreibt ihn mit "audio/mpeg". piper_server
    # resampelt deshalb auf genau 24000: dann stimmt das Tempo trotzdem.
    secs = len(a) / 24000
    ok_pcm = raw[:4] != b"RIFF" and peak > 1000 and 2.0 < secs < 5.0
    check("tts pcm", ok_pcm, "%.1fs | %.2fs Audio @24000 Hz, Peak %d, roh=%s"
          % (dt, secs, peak, raw[:4] != b"RIFF"))
except Exception as e:
    check("tts pcm", False, str(e)[:110])

if args.image:
    print("\n7) Bildgenerierung (flux-klein, stable-diffusion.cpp) — der Bildpfad der App")
    # Wie die App: /chat/completions mit einem Bildmodell; das Bild kommt in
    # message.images[] zurueck (extractImageFromResponse, app.html ~36407). Prueft
    # zugleich, dass LiteLLM message.images durchreicht. Auf CPU dauert das Minuten.
    d, dt = call("/chat/completions", {"model": "flux-klein",
                 "messages": [{"role": "user", "content": "a single red apple, minimalist icon"}]})
    if "__err" in d:
        check("bild", False, d["__err"])
    else:
        try:
            msg = d["choices"][0]["message"]
            url = (msg.get("images") or [{}])[0].get("image_url", {}).get("url", "")
            raw = base64.b64decode(url.split(",", 1)[1]) if url.startswith("data:image") else b""
            check("bild", raw[:8] == b"\x89PNG\r\n\x1a\n", "%.0fs | PNG %d KB" % (dt, len(raw) / 1024))
        except Exception as e:
            check("bild", False, str(e)[:110])

print("\n" + "=" * 64)
print("%d/%d Endpunkte OK%s" % (sum(results), len(results),
                                "" if all(results) else "  <-- FEHLGESCHLAGEN"))
sys.exit(0 if all(results) else 1)
