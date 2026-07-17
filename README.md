# BuchTutorLocal — Fully Local OpenAI-Compatible AI Stack

Run the [BuchTutor](https://github.com/FalkoGuderian/BuchTutor) tutoring app
(available on Google Play as
[DocWorm](https://play.google.com/store/apps/details?id=com.profbookworm.app))
entirely offline. This repository provides a local AI backend that mimics the
OpenAI-compatible API surface the app already talks to, so no data leaves the
machine and no cloud key is required for the core features.

A single proxy (`LiteLLM`) exposes one endpoint — `http://localhost:4000/v1`
— and routes each model ID to the right local backend:

```
app.html ──► LiteLLM :4000 ──┬──► llama-server  :8080  Gemma 4 E2B       (Chat + Vision + Audio)
             (native, venv)   ├──► llama-server  :8081  bge-m3            (Embeddings)
                             ├──► piper_server  :8082  Piper             (TTS, DE + EN)
                             ├──► flux_server   :8083  FLUX.2 Klein 4B   (Image generation)
                             │        └──► sd-server :8084 (sd.cpp, internal, weights kept warm)
                             ├──► LM Studio     :1234  Qwen3.6-12B       (Chat fallback, text only)
                             └──► OpenRouter (Cloud, optional, needs OPENROUTER_API_KEY)
```

Status: Chat, Vision, Audio transcription, Embeddings, TTS, and Image generation
are implemented and verified locally. The app must be pointed at this endpoint
(see "Connecting the app" below).

> All paths in this repo are relative and configurable via environment variables.
> No absolute user paths or personal identifiers are required.

---

## 1. What this stack provides

| Capability   | Model                  | Served via            | Port |
|--------------|------------------------|-----------------------|------|
| Chat         | Gemma 4 E2B            | llama-server          | 8080 |
| Vision       | Gemma 4 E2B (mmproj)   | llama-server          | 8080 |
| Audio in     | Gemma 4 E2B (mmproj)   | llama-server          | 8080 |
| Embeddings   | bge-m3                 | llama-server          | 8081 |
| TTS (voice)  | Piper (DE + EN)        | piper_server.py       | 8082 |
| Image gen.   | FLUX.2 Klein 4B        | flux_server.py/sd.cpp | 8083 |
| Chat backup  | Qwen3.6-12B            | LM Studio             | 1234 |
| Cloud (opt.) | Gemini (OpenRouter)    | OpenRouter            | —    |

---

## 2. Quick start

Target platform: **Windows with git-bash** (the scripts use `bash` + `curl` +
`taskkill` + `powershell`). Python 3 is required for the servers and tests.

```bash
# 1) One-time: create the LiteLLM venv and install dependencies
./run_litellm.sh setup

# 2) One-time: install Piper and download the voices (DE + EN, ~60 MB each)
./run_tts.sh setup

# 3) Start the heavy backends (Gemma :8080 + bge-m3 :8081)
./run_llm.sh

# 4) In separate terminals / background: TTS, image gen, and the proxy
./run_tts.sh
./run_flux.sh          # optional — only needed for image generation
./run_litellm.sh       # the proxy on :4000 (binds 127.0.0.1 by default)

# 5) Verify everything end-to-end
python smoke_test.py           # expects 6/6 endpoints OK
python smoke_test.py --image    # also exercises image generation (8/8)
```

Alternatively, drive the whole stack with a single command:

```bash
./manage_servers.sh start      # start all in dependency order (background)
./manage_servers.sh status     # show the state of every service
./manage_servers.sh restart    # kill + start
./manage_servers.sh kill       # stop everything
./manage_servers.sh kill tts   # address a single service (llm|tts|flux|litellm)
```

Or use the web UI:

```bash
python stack_manager.py        # UI at http://127.0.0.1:8800
```

The manager exposes Start/Restart/Kill per service, live logs, health status,
and a LAN toggle (sets `LITELLM_HOST` for the next LiteLLM start so a phone on
the same Wi-Fi can reach the proxy).

---

## 3. Model IDs and where they are loaded

The app and the smoke test talk to LiteLLM using the model IDs below. LiteLLM
maps each ID to a local backend via `litellm_config.yaml`.

| Model ID            | Backend                     | Loaded from (default location)                                   | Capabilities            |
|---------------------|-----------------------------|------------------------------------------------------------------|-------------------------|
| `gemma-4-e2b-fast`  | llama-server `:8080`        | `<LM Studio>/lmstudio-community/gemma-4-E2B-it-GGUF/*.gguf` + `mmproj-gemma-4-E2B-it-BF16.gguf` | Chat, Vision, **Audio** (reasoning off — recommended) |
| `gemma-4-e2b`       | llama-server `:8080`        | same as above                                                    | Chat, Vision, Audio (with reasoning, ~2.8× slower) |
| `bge-m3`            | llama-server `:8081`        | `<LM Studio>/cPilotGod/baai-bge-m3-568m-gguf/bge-m3-Q8_0.gguf`   | Embeddings (native 1024 dim) |
| `baai/bge-m3`       | llama-server `:8081`        | same as above (alias used by the app's index metadata)           | Embeddings (alias) |
| `piper-tts`         | piper_server.py `:8082`     | `voices/` (downloaded by `run_tts.sh setup`)                     | TTS, `voice:"de"` (default) or `"en"` |
| `flux-klein`        | flux_server.py `:8083`      | `<FLUX_MODEL_DIR>/flux-2-klein-4b-Q4_0.gguf` + `Qwen3-4B-Q4_K_M.gguf` + `flux2-vae.safetensors` | Image generation (FLUX.2 Klein 4B) |
| `qwen3.6-12b`       | LM Studio `:1234`           | LM Studio model directory (text-only server)                     | Chat, text only |
| `openrouter-gemini` | OpenRouter (cloud)          | requires `OPENROUTER_API_KEY`                                    | Chat (cloud fallback) |

The API key for the whole stack is a fixed placeholder: `sk-local-llm`. None of
the local backends actually validate it — it only satisfies the OpenAI-compatible
request shape.

### Where the model files come from

The LLM/embedding weights, the FLUX.2 weights, and the Piper voices are **not**
stored in this repository (they are large binaries). Default source locations:

- **Gemma 4 E2B** — `lmstudio-community/gemma-4-E2B-it-GGUF` (the `Q4_K_M` GGUF
  plus the `mmproj-gemma-4-E2B-it-BF16.gguf` multimodal projector, which carries
  both the vision **and** audio towers).
- **bge-m3** — `cPilotGod/baai-bge-m3-568m-gguf` (`bge-m3-Q8_0.gguf`,
  started with `--embedding`).
- **Piper voices** — downloaded automatically by `run_tts.sh setup` from the
  [piper-voices](https://huggingface.co/rhasspy/piper-voices) catalog
  (`de_DE-thorsten-medium`, `en_US-lessac-medium`).
- **FLUX.2 Klein 4B** (three files):
  - `flux-2-klein-4b-Q4_0.gguf` ← [leejet/FLUX.2-klein-4B-GGUF](https://huggingface.co/leejet/FLUX.2-klein-4B-GGUF)
  - `Qwen3-4B-Q4_K_M.gguf` (text encoder) ← [unsloth/Qwen3-4B-GGUF](https://huggingface.co/unsloth/Qwen3-4B-GGUF)
  - `flux2-vae.safetensors` (VAE) ← [Comfy-Org/flux2-dev](https://huggingface.co/Comfy-Org/flux2-dev)
  - The `sd-server.exe` binary is fetched by `./run_flux.sh setup` from the
    [stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp)
    win-cpu release and unpacked into `sdcpp/`.
- **Qwen3.6-12B** — loaded through the LM Studio desktop app (its own runtime).

All of these locations are overridable via environment variables (see
`run_llm.sh` / `run_flux.sh` for `LMS_MODELS`, `FLUX_MODEL_DIR`, `FLUX_SIZE`,
`FLUX_STEPS`, …).

---

## 4. Connecting the app

The app normally calls OpenRouter directly. To point it at this local stack you
need an AI provider profile that sets:

- **Base URL:** `http://127.0.0.1:4000/v1`
- **API key:** `sk-local-llm`
- **Model IDs:** `gemma-4-e2b-fast` (chat), `bge-m3` (embeddings),
  `piper-tts` (TTS), `flux-klein` (image)

On the same machine `127.0.0.1` is enough. For phone access on the same Wi-Fi,
set `LITELLM_HOST=0.0.0.0 ./run_litellm.sh`, restart the proxy, and enter the
PC's LAN IP in the app (e.g. `http://192.168.x.x:4000/v1`).

> Note: if the app is served over HTTPS or as an Android WebView, the local
> endpoint is plain HTTP. The app's Content-Security-Policy must allow
> `connect-src ... http:` for the local endpoint to be reachable (the app
> already permits `https:`, so adding `http:` is consistent and does not add
> exfiltration risk).

---

## 5. Files

| File | Purpose |
|------|---------|
| `run_llm.sh` | Starts the llama-server LLMs Gemma (`:8080`) + bge-m3 (`:8081`). `stop` / `status` as argument. |
| `run_litellm.sh` | Starts LiteLLM (`:4000`). `setup` creates the venv. Binds `127.0.0.1` by default; `LITELLM_HOST=0.0.0.0` for LAN. |
| `run_tts.sh` | Starts the Piper TTS server (`:8082`). `setup` installs Piper + voices. |
| `run_flux.sh` | Starts image generation (`:8083` + internal sd-server `:8084`). `setup` fetches the sd.cpp binary. |
| `piper_server.py` | OpenAI-compatible TTS server (`/v1/audio/speech`), stdlib + Piper. Resamples to 24000 Hz for PCM streaming. |
| `flux_server.py` | OpenAI-Chat shim for image generation (`/v1/chat/completions` → image in `message.images[]`); supervises `sd-server.exe`. |
| `litellm_config.yaml` | **The active model routing config.** Maps model IDs → local backends. |
| `smoke_test.py` | End-to-end test of all endpoints through `:4000`. `--image` also tests image generation. |
| `manage_servers.sh` | Central stack control (CLI): `status` (default), `start`, `kill`/`stop`, `restart`. Optional single service as 2nd arg. |
| `stack_manager.py` | Web UI for stack control on `:8800` (per-service Start/Restart/Kill, live SSE logs, health, LAN toggle). Stdlib only. |
| `stack_manager.html` | UI served by the manager. Preferred over the embedded fallback HTML in the `.py`. |
| `test_local.html` | Single-file test page (base URL / key / model / prompt → `POST /v1/chat/completions`). |
| `gen_audio_fixture.py` | Regenerates the speech fixture (`_audio_30s.wav`) used by the smoke test's audio section. |
| `gen_image_fixture.py` | Regenerates the image fixture (`_apple_red.png`) used by the smoke test's image section. |
| `firewall_open_4000.cmd` | Opens the Windows Firewall for inbound TCP 4000 (run as admin). |
| `firewall_open_8000.cmd` | Opens the Windows Firewall for inbound TCP 8000 (run as admin; for serving `test_local.html` over LAN). |

Not in the repo (gitignored): `.venv/`, `logs/`, `voices/`, `sdcpp/`, generated
fixtures (`_audio_*.wav`, `_apple_*.png`), and `stack_manager_settings.json`.

---

## 6. Notes & known constraints

- **CPU image generation is slow.** FLUX.2 Klein 4B runs on the CPU build of
  sd.cpp (no working Vulkan prebuilt for the integrated GPU). Default size is
  256×256 at 4 steps — a few minutes per image. Size is the main speed lever
  (`FLUX_SIZE=512x512` for better quality, much slower).
- **TTS sample rate.** The app reads the PCM rate from the `Content-Type` and
  falls back to 24000 Hz. LiteLLM overwrites that header, so `piper_server.py`
  resamples Piper's native 22050 Hz to 24000 Hz to keep the speaking rate
  correct.
- **Audio input format.** The app records webm/opus via MediaRecorder; the
  local stack needs WAV (16 or 48 kHz) — a browser-side WAV conversion is
  required before sending audio to llama-server.
- **Reasoning toggle.** Gemma 4 has reasoning on by default, which can consume
  the whole token budget. The `gemma-4-e2b-fast` alias disables it
  (`chat_template_kwargs: {enable_thinking: false}`).
- **Container variant not used here.** LiteLLM runs natively (not in Docker)
  because the container cannot reach the Windows-host backends without firewall
  changes; native loopback needs no exceptions.
- **Cloud-only features not covered:** OAuth key exchange and music generation
  remain OpenRouter-specific.

---

## 7. Requirements

- Windows with git-bash (scripts use `bash`, `curl`, `taskkill`, `powershell`).
- Python 3 + `pip` (for the venv, Piper, and the servers/tests).
- [LM Studio](https://lmstudio.ai/) installed (provides `llama-server.exe` and
  can optionally serve Qwen3.6-12B on `:1234`). The Gemma/bge-m3 weights must
  be downloaded into LM Studio's model folder.
- The Gemma 4 E2B GGUF **and** its `mmproj` file (vision + audio tower).
- Free RAM for the loaded weights (Gemma ~few GB, bge-m3 small, FLUX.2 ~6 GB if
  image generation is used).
