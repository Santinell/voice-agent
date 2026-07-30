#!/usr/bin/env bash
#
# start_local.sh — голосовой ассистент на ЛОКАЛЬНОЙ LLM через ollama.
#
# Полностью офлайн-вариант start.sh: вместо облака z.ai/GLM-5 s2s-сервер
# обращается к локальной модели в ollama (OpenAI-совместимый API на :11434/v1).
# Инструменты (погода, калькулятор) работают — при условии, что модель умеет
# function-calling (по умолчанию qwen3:4b).
#
# Что делает скрипт:
#   1. читает .env (для S2S_URL, LANGUAGE и пр.)
#   2. убеждается, что ollama запущена и нужная модель присутствует
#   3. запускает speech-to-speech сервер с --responses_api_base_url на ollama
#   4. дожидается готовности s2s и запускает наш клиент
#
# Переменные (можно задать в .env или перед запуском):
#   LLM_BACKEND           — "ollama" (по умолчанию) или "llama.cpp"
#   LOCAL_MODEL           — имя модели (по умолчанию gemma4:e4b для ollama;
#                           для llama.cpp это просто метка для --model_name)
#   OLLAMA_HOST           — адрес ollama (по умолчанию http://localhost:11434)
#   LLAMA_HOST            — адрес llama-server (по умолчанию http://localhost:8080)
#   S2S_PORT              — порт s2s-сервера (по умолчанию 8765)
#   S2S_HOST              — хост проверки готовности (по умолчанию 127.0.0.1)
#   S2S_BIN               — команда запуска s2s (авто: локальная из venv либо uvx)
#   S2S_FLAGS             — доп. флаги (--stt_backend, --tts_backend, ...)
#   QWEN3_TTS_BACKEND     — бэкенд Qwen3-TTS: torch (по умолчанию) или ggml.
#                           torch нужен на CUDA 13 (ggml-библиотека собрана под 12).
#   S2S_STARTUP_TIMEOUT   — сколько секунд ждать подъёма s2s (по умолчанию 300)
#   LOG_LEVEL             - Уровень ошибок (debug | info | warning | error).
#                          По умолчанию warning, без warmup/httpx-шума.
#
# Для llama.cpp: поднимите llama-server сами (нужен --jinja для function-calling)
#   llama-server -m model.gguf --port 8080 --jinja
# затем запустите с LLM_BACKEND=llama.cpp. Проверка ollama при этом не делается.

set -euo pipefail

# ─── каталог скрипта ──────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ─── 1. .env ──────────────────────────────────────────────────────────────────
ENV_FILE="${ENV_FILE:-.env}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "✗ Файл $ENV_FILE не найден. Скопируйте .env.example -> .env." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# ─── параметры ────────────────────────────────────────────────────────────────
LLM_BACKEND="${LLM_BACKEND:-ollama}"
LOCAL_MODEL="${LOCAL_MODEL:-gemma4:e2b}"
OLLAMA_HOST="${OLLAMA_HOST:-http://localhost:11434}"
LLAMA_HOST="${LLAMA_HOST:-http://localhost:8080}"
S2S_HOST="${S2S_HOST:-127.0.0.1}"
S2S_PORT="${S2S_PORT:-8765}"
S2S_STARTUP_TIMEOUT="${S2S_STARTUP_TIMEOUT:-300}"
S2S_FLAGS="${S2S_FLAGS:---parakeet_tdt_device cpu}"
LOG_LEVEL="${LOG_LEVEL:-warning}"

case "$LLM_BACKEND" in
  ollama)
    BACKEND_HOST="$OLLAMA_HOST"
    ;;
  llama.cpp|llama_cpp|llamacpp|llama-server|llama_server)
    LLM_BACKEND="llama.cpp"
    BACKEND_HOST="$LLAMA_HOST"
    ;;
  *)
    echo "✗ Неизвестный LLM_BACKEND=«$LLM_BACKEND». Допустимо: ollama | llama.cpp" >&2
    exit 1
    ;;
esac
LLM_BASE_URL="${BACKEND_HOST%/}/v1"

# Где искать s2s: локальный (если установлен через `uv sync --extra s2s`),
# иначе uvx (сам доустановит пакет).
if [[ -z "${S2S_BIN:-}" ]]; then
  if local_bin="$(uv run --no-sync which speech-to-speech 2>/dev/null)" && [[ -n "$local_bin" ]]; then
    S2S_BIN="$local_bin"
  else
    S2S_BIN="uvx --from speech-to-speech speech-to-speech"
  fi
fi

port_open() { (exec 3<>"/dev/tcp/$1/$2") 2>/dev/null && exec 3>&- 3<&-; }

S2S_STARTED=0
S2S_PID=""

cleanup() {
  echo
  if [[ "$S2S_STARTED" -eq 1 && -n "$S2S_PID" ]]; then
    echo "■ Останавливаю speech-to-speech сервер (pid $S2S_PID) …"
    kill "$S2S_PID" 2>/dev/null || true
    wait "$S2S_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# ─── 2. бэкенд LLM: ollama или llama.cpp ────────────────────────────────────────
echo "▶ Проверяю LLM-бэкенд: $LLM_BACKEND ($BACKEND_HOST) …"
if [[ "$LLM_BACKEND" == "ollama" ]]; then
  if ! api_ver="$(curl -fsS --max-time 3 "$OLLAMA_HOST/api/version" 2>/dev/null)"; then
    echo "✗ ollama не отвечает на $OLLAMA_HOST. Запустите её:" >&2
    echo "    ollama serve   # или системный unit" >&2
    exit 1
  fi
  echo "✓ ollama на месте ($api_ver)."
  if ! curl -fsS --max-time 5 "$OLLAMA_HOST/api/tags" 2>/dev/null \
       | grep -q "\"name\":\"${LOCAL_MODEL}\""; then
    echo "✗ Модель «$LOCAL_MODEL» не найдена в ollama. Проверьте 'ollama ls' или:" >&2
    echo "    ollama pull $LOCAL_MODEL" >&2
    echo "  Либо задайте LOCAL_MODEL=<другая>." >&2
    exit 1
  fi
  echo "✓ модель $LOCAL_MODEL доступна."
else  # llama.cpp
  if ! curl -fsS --max-time 3 "$LLM_BASE_URL/models" 2>/dev/null >/dev/null; then
    echo "✗ llama-server не отвечает на $LLM_BASE_URL. Поднимите его:" >&2
    echo "    llama-server -m model.gguf --port 8080 --jinja" >&2
    echo "  (--jinja обязателен для function-calling)" >&2
    exit 1
  fi
  echo "✓ llama-server отвечает на $LLM_BASE_URL."
  echo "  ℹ LOCAL_MODEL=«$LOCAL_MODEL» передаётся как --model_name; убедитесь,"
  echo "    что сервер действительно обслуживает эту модель."
fi

# ─── 3. speech-to-speech сервер → ollama ──────────────────────────────────────
if port_open "$S2S_HOST" "$S2S_PORT"; then
  echo "• Порт $S2S_HOST:$S2S_PORT уже занят — s2s, видимо, уже запущен. Пропускаю запуск."
else
  echo "▶ Запуск speech-to-speech (LLM = $LLM_BACKEND:$LOCAL_MODEL) …"
  # Флаги для speech-to-speech >=0.2.11: бэкенд --llm_backend chat-completions
  # (OpenAI Chat Completions API — поддерживается ollama / llama-server).
  # Натравливаем на локальный эндпоинт; ключ фиктивный — оба сервера его
  # игнорируют, но OpenAI-клиенту нужно хоть что-то.
  #
  # Qwen3-TTS на бэкенде torch (см. комментарий в start.sh): GGML-бэкенд собран
  # под CUDA 12 и падает на CUDA 13. Перекрыть: QWEN3_TTS_BACKEND (ggml|torch).
  "$S2S_BIN" \
    --llm_backend chat-completions \
    --responses_api_base_url "$LLM_BASE_URL" \
    --responses_api_api_key local \
    --model_name "$LOCAL_MODEL" \
    --qwen3_tts_backend "${QWEN3_TTS_BACKEND:-torch}" \
    --min_silence_ms "${VAD_MIN_SILENCE_MS:-700}" \
    --unanswered_reopen_ms "${VAD_UNANSWERED_REOPEN_MS:-1500}" \
    --log_level "$LOG_LEVEL" \
    ${S2S_FLAGS:-} \
    &
  S2S_PID=$!
  S2S_STARTED=1

  echo "⏳ Ожидаю готовности s2s на ${S2S_HOST}:${S2S_PORT} (до ${S2S_STARTUP_TIMEOUT}с) …"
  ready=0
  for _ in $(seq 1 "$S2S_STARTUP_TIMEOUT"); do
    if port_open "$S2S_HOST" "$S2S_PORT"; then ready=1; break; fi
    if ! kill -0 "$S2S_PID" 2>/dev/null; then
      echo "✗ speech-to-speech сервер завершился раньше времени." >&2
      exit 1
    fi
    sleep 1
  done
  if [[ "$ready" -ne 1 ]]; then
    echo "✗ speech-to-speech не поднялся за ${S2S_STARTUP_TIMEOUT}с." >&2
    exit 1
  fi
  echo "✓ speech-to-speech сервер готов."
fi

# ─── 4. наш клиент ────────────────────────────────────────────────────────────
echo "▶ Запуск клиента (uv run run-client) …"
set +e
uv run run-client
CLIENT_EXIT=$?
set -e

exit "$CLIENT_EXIT"
