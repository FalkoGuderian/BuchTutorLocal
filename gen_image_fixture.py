#!/usr/bin/env python3
"""
Erzeugt local/_apple_red.png - das Bild-Fixture fuer smoke_test.py
(Abschnitt 7, Bildgenerierung via FLUX.2 Klein 4B).

Wie gen_audio_fixture.py (das synthetische Sprache erzeugt) ist das KEIN
echtes Foto, sondern per lokalem flux_server synthetisiert, damit der
Bild-Round-Trip belegt, dass der FLUX-Pfad wirklich durchlaeuft
(die App liest das Bild aus message.images[] zurueck).

Voraussetzung: ./run_flux.sh laeuft (flux_server auf :8083 -> sd-server :8084).

    python gen_image_fixture.py [--out local/_apple_red.png] [--host 127.0.0.1:8083]

Der Prompt ist fest auf "einen roten Apfel" (DE) bzw. "a single red apple"
(EN) gesetzt, damit das Fixture deterministisch benennbar bleibt.
"""
import argparse, base64, json, os, sys, urllib.request, urllib.error

# Platzhalter; echter Key kommt ueber SMOKE_KEY. flux_server prueft den Key
# nicht, ASCII bewusst (urllib Header als latin-1 kodiert).
KEY = os.environ.get("SMOKE_KEY", "redacted:sk-...")

PROMPT = "a single red apple, minimalist, plain white background, centered icon"

# DE-Alias, falls manuell anders getestet wird:
PROMPT_DE = "ein einzelner roter Apfel, minimalistisch, weisser Hintergrund"


def main():
    ap = argparse.ArgumentParser()
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--out", default=os.path.join(here, "_apple_red.png"))
    ap.add_argument("--host", default="127.0.0.1:8083")
    ap.add_argument("--prompt", default=PROMPT, help="Bildprompt (Default: roter Apfel)")
    ap.add_argument("--size", default="256x256",
                    help="WxH; kleiner = schneller (der Haupt-Tempohebel)")
    args = ap.parse_args()

    # Wie die App: /chat/completions mit einem Bildmodell. Das Bild kommt in
    # message.images[] zurueck (extractImageFromResponse, app.html ~36407).
    url = f"http://{args.host}/v1/chat/completions"
    payload = json.dumps({
        "model": "flux-klein",
        "messages": [{"role": "user", "content": args.prompt}],
        "size": args.size,
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + KEY})
    try:
        raw = urllib.request.urlopen(req, timeout=900).read()
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode()[:200]}")
    except Exception as e:
        sys.exit(f"FEHLER: {e}\nLaeuft ./run_flux.sh (flux_server auf :8083)?")

    try:
        d = json.loads(raw)
        msg = d["choices"][0]["message"]
        img_url = (msg.get("images") or [{}])[0].get("image_url", {}).get("url", "")
    except Exception as e:
        sys.exit(f"FEHLER: Antwort nicht im erwarteten Format: {e}\n{raw[:200]}")

    if not img_url.startswith("data:image"):
        sys.exit(f"FEHLER: Kein Bild in message.images[] ({img_url[:60]})")

    png = base64.b64decode(img_url.split(",", 1)[1])
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        sys.exit("FEHLER: Antwort ist kein PNG (Header fehlt).")

    with open(args.out, "wb") as f:
        f.write(png)
    print(f"OK: {args.out}  ({len(png) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
