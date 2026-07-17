#!/usr/bin/env python3
"""
Erzeugt local/_audio_30s.wav - das Sprach-Fixture fuer smoke_test.py
(Abschnitt 3, Audio-Transkription).

Das ist KEIN echtes Audio, sondern per Piper-TTS synthetisiert, damit der
TTS->ASR-Round-Trip belegt, dass der Audio-Pfad wirklich durchlaeuft
(Gemma transkribiert die synthetische Stimme sauber zurueck).

Voraussetzung: run_tts.sh laeuft (Piper auf :8082).

    python gen_audio_fixture.py [--out local/_audio_30s.wav] [--host 127.0.0.1:8082]
"""
import argparse, json, os, sys, urllib.request, urllib.error

# Platzhalter; echter Key kommt ueber SMOKE_KEY (lokal: beliebiger ASCII-Wert,
# da Piper den Key nicht prueft). ASCII, weil urllib Header als latin-1 kodiert
# -> ASCII only (urllib encodes headers as latin-1).
KEY = os.environ.get("SMOKE_KEY", "redacted:sk-...")

TEXT = (
    "Bla bla Der lokale KI-Stack erlaubt es, BuchTutor vollstaendig ohne Cloud "
    "auszufuehren. Ein schlanker Proxy uebersetzt die OpenAI-kompatiblen "
    "Aufrufe der App und leitet sie an die lokalen Modelle weiter. "
    "Spracheingaben werden direkt auf dem Rechner transkribiert, Bilder "
    "von einem Vision-Tower ausgewertet und die Sprachausgabe von einer "
    "neuralen Stimme erzeugt. Auf diese Weise verlaesst keine Nutzerdaten "
    "das Geraet, waehrend die Antwortqualitaet fuer die meisten Lehrinhalte "
    "voellig ausreicht. Wer das System testet, hoert schnell den Unterschied "
    "zwischen einer echten Aufnahme und einer synthetisierten Stimme."
)


def main():
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--out", default=os.path.join(here, "_audio_30s.wav"))
    ap.add_argument("--host", default="127.0.0.1:8082")
    ap.add_argument("--voice", default="de")
    args = ap.parse_args()

    url = f"http://{args.host}/v1/audio/speech"
    payload = json.dumps({
        "model": "piper-tts",
        "input": TEXT,
        "voice": args.voice,
        "response_format": "wav",
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + KEY})
    try:
        audio = urllib.request.urlopen(req, timeout=120).read()
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode()[:200]}")
    except Exception as e:
        sys.exit(f"FEHLER: {e}\nLaeuft run_tts.sh (Piper auf :8082)?")

    if audio[:4] != b"RIFF":
        sys.exit("FEHLER: Antwort ist kein WAV (RIFF-Header fehlt).")

    with open(args.out, "wb") as f:
        f.write(audio)
    print(f"OK: {args.out}  ({len(audio) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
