# Voice Agent

Voice assistant **client** that talks to a locally running
[`huggingface/speech-to-speech`](https://github.com/huggingface/speech-to-speech)
Realtime server and adds **client-side wake-word activation** and tools
(weather, calculator).

The s2s server's LLM can be anything OpenAI-compatible: a cloud endpoint
([Quick start](#quick-start), default GLM-5 via z.ai) or a **local** model in
ollama / llama.cpp ([Local LLM mode](#local-llm-mode-offline)). Two launcher
scripts cover both: `./start.sh` (cloud) and `./start_local.sh` (offline).

## Idea

The speech-to-speech project is a low-latency streaming pipeline
(VAD → STT → LLM → TTS) that exposes an OpenAI Realtime-compatible WebSocket.
It relays the LLM's `function_call`s to the client. We keep s2s **untouched**
(never forked) and put all the "intelligence" — tool definitions and execution —
on the client side.

```
speech-to-speech server (off-the-shelf)        our client
┌────────────────────────────────────┐     ┌───────────────────────────────┐
│ VAD → STT → LLM(GLM-5) → TTS       │◄───►│ mic → input_audio_buffer       │
│ ws://localhost:8765/v1/realtime    │ WS  │ response.audio.delta → speaker │
│ streaming + barge-in               │     │ local wake gate → earcon       │
└────────────────────────────────────┘     │ session.update{ tools }        │
                                           │ function_call → dispatch       │
                                           │   • get_weather (Open-Meteo)   │
                                           │   • calculate (safe ast eval)  │
                                           │ → function_call_output         │
                                           └───────────────────────────────┘
```

## Quick start

### 1. Install dependencies

```bash
uv sync                       # the client only
uv sync --extra s2s           # also installs the speech-to-speech SERVER CLI
```

### 2. Configure

```bash
cp .env.example .env   # set OPENAI_API_KEY (for the server's LLM), LANGUAGE
```

`.env` holds everything both processes need: `OPENAI_BASE_URL` /
`OPENAI_API_KEY` / `LLM_MODEL` for the s2s server's LLM, plus `S2S_URL`
(the websocket this client connects to).

### 3. Run

The easiest path — `start.sh` reads `.env`, launches the s2s server, waits for
it, then starts the client:

```bash
./start.sh
```

Speak, ask "какая погода в Москве?" or "сколько будет 12 умножить на 8".

## Wake word

A non-empty `WAKE_WORD_MODEL` enables the local wake gate. While sleeping,
microphone PCM stays on the client and is inspected only by openWakeWord. The
trigger block and all blocks recorded during the earcon are discarded; audio
starts flowing to the Realtime server only after the earcon and its trailing
silence have passed through the speaker queue.

Prepare the configured model once before the first run:

```bash
WAKE_WORD_MODEL=hey_jarvis uv run prepare-wakeword
```

Built-in names are resolved from the installed package resources when present,
or downloaded into `WAKE_WORD_MODEL_DIR` together with the shared feature and
VAD models. A manually downloaded model works by filename without an extension:

```text
.local/openwakeword/hey_findus.onnx
WAKE_WORD_MODEL=hey_findus
```

An absolute or relative `.onnx`/`.tflite` path is also accepted. Resolution is
strict: an unknown name or missing path stops startup instead of silently
falling back to an always-on microphone. Set `WAKE_WORD_MODEL=` to explicitly
restore the original always-on mode.

After each final answer the gate remains open for `FOLLOW_UP_WINDOW_SEC`
(10 seconds by default). This lets the user answer an assistant's clarifying
question or continue the dialogue without repeating the wake word. A new
server-side speech event starts another turn and the window is opened again
after its final playback. Set the value to `0` to require the wake word after
every completed answer.

AEC and barge-in remain on the existing path: in `ACTIVE` and `FOLLOW_UP`
states every microphone block is sent continuously, including while response
audio is playing. Configure `INPUT_DEVICE` with the existing echo-cancelled
source as before.

When the dialogue window closes, a lower descending cue is played. Microphone
forwarding is already disabled at that point, and wake detection resumes only
after the cue and its trailing silence, so the sound cannot wake the assistant
itself. The cue is also used when the post-activation listen timeout or the
hard active timeout expires.

Useful tuning variables:

| Variable                        | Default | Purpose                                    |
| ------------------------------- | ------: | ------------------------------------------ |
| `WAKE_WORD_THRESHOLD`           |    0.35 | detection score threshold                  |
| `WAKE_WORD_GAIN`                |     1.0 | detector-only software input gain          |
| `WAKE_WORD_PATIENCE`            |       1 | consecutive positive 80 ms frames          |
| `WAKE_WORD_VAD_THRESHOLD`       |     0.5 | local speech filter; `0` disables it       |
| `ACTIVATION_LISTEN_TIMEOUT_SEC` |       8 | rearm if no command follows the earcon     |
| `FOLLOW_UP_WINDOW_SEC`          |      10 | dialogue window after final audio playback |
| `MAX_ACTIVE_SEC`                |      90 | hard cap for a stuck activation cycle      |
| `POST_EARCON_SILENCE_MS`        |     120 | output guard before microphone forwarding  |
| `DEACTIVATION_EARCON_START_HZ`  |     660 | beginning of the descending sleep cue      |
| `DEACTIVATION_EARCON_END_HZ`    |     440 | end of the descending sleep cue            |

The full design and test matrix are recorded in
[WAKE_WORD.md](WAKE_WORD.md).

## Local LLM mode (offline)

`start_local.sh` runs the same pipeline but points the s2s server at a **local**
LLM instead of the cloud provider. It supports two backends via the
`LLM_BACKEND` option — **`ollama`** (default) or **`llama.cpp`** — verifying only
the one in use, then launches s2s against its OpenAI-compatible `/v1` endpoint,
waits for it, and starts the client — exactly like `start.sh`.

### ollama (default)

```bash
ollama pull qwen3:4b     # once; must support function-calling
./start_local.sh         # LLM_BACKEND=ollama is the default
```

The script checks ollama is serving (`/api/version`) and that `LOCAL_MODEL` is
pulled (`/api/tags`), then runs s2s with
`--responses_api_base_url http://localhost:11434/v1`.

Requirements:

- **ollama** installed and serving (`ollama serve`).
- A model that does **function-calling** — the assistant is useless without
  tools. Default `qwen3:4b` works (verified). `llama3.2:3b` and the `gemma4:e4b`
  tags are also present but tool support varies — prefer Qwen3.

### Low-VRAM models (≤ 8 GB GPU)

When the GPU is shared with TTS (Qwen3-TTS uses ~4.2 GB), a fully offloaded LLM
won't fit. The `models/` directory contains Modelfiles with a reduced `num_gpu`
(some layers stay on CPU), and `small_models.sh` builds them with a `:small` tag:

```bash
./small_models.sh          # creates gemma4:e2b-small, qwen3:4b-small, llama3.2:3b-small
LOCAL_MODEL=qwen3:4b-small ./start_local.sh
```

### llama.cpp (alternative)

Instead of ollama you can run a bare `llama-server`. This saves CPU and memory
but requires downloading GGUF models manually — for advanced users only.

Run the server yourself (`--jinja` is required for function-calling), then
switch the backend — ollama is not checked in this mode:

```bash
llama-server -m model.gguf --port 8080 --jinja &
LLM_BACKEND=llama.cpp ./start_local.sh
```

The script only verifies the server answers at its `/v1` endpoint.

### Tunables

Set in `.env` or the environment:

| Variable      | Default                  | Purpose                                                   |
| ------------- | ------------------------ | --------------------------------------------------------- |
| `LLM_BACKEND` | `ollama`                 | `ollama` or `llama.cpp` (aliases: `llama-server`)         |
| `LOCAL_MODEL` | `qwen3:4b`               | model name — ollama tag or llama.cpp `--model_name` label |
| `OLLAMA_HOST` | `http://localhost:11434` | ollama base address                                       |
| `LLAMA_HOST`  | `http://localhost:8080`  | llama-server base address                                 |
| `S2S_PORT`    | `8765`                   | s2s server port                                           |
| `S2S_FLAGS`   | —                        | extra s2s flags (`--stt_backend …`, `--tts_backend …`)    |

> Trade-off vs the cloud path: fully private/offline, but higher latency and
> lower quality — a 4B model streamed token-by-token on local hardware lags
> behind GLM-5. The client-side tools (weather, calculator) behave identically.

## Status

Experiment. Client-side wake activation, follow-up dialogue windows, weather
and calculator tools are implemented. The speech-to-speech package remains
unmodified and continues to own server-side VAD, STT, LLM, TTS and interruption
handling.
