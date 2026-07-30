# s2s-assistant

Voice assistant **client** that talks to a locally running
[`huggingface/speech-to-speech`](https://github.com/huggingface/speech-to-speech)
Realtime server and adds its own **client-side tools** (weather, calculator).

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
│ streaming + barge-in               │     │ session.update{ tools }        │
└────────────────────────────────────┘     │ function_call → dispatch       │
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

Experiment. Weather and calculator tools are real; everything else is out of
scope (see the project plan). No wake word — relies on the s2s server's VAD.
