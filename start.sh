#!/usr/bin/env bash
#
# start.sh — поднимает голосовой ассистент целиком:
#   1. читает переменные из .env
#   2. запускает speech-to-speech сервер (порт 8765)
#   3. дожидается его готовности и запускает наш клиент
#
# Ctrl-C корректно останавливает s2s (клиент ловит сигнал сам).
#
# Переменные окружения (можно задать в .env или перед запуском):
#   LLM_MODEL            --model_name           (по умолчанию glm-5, из .env)
#   OPENAI_BASE_URL      --responses_api_base_url (из .env; для z.ai — api.z.ai/...)
#   OPENAI_API_KEY       --responses_api_api_key  (из .env)
#   LOG_LEVEL            --log_level s2s (debug | info | warning | error).
#                          По умолчанию warning, без warmup/httpx-шума.
#   S2S_PORT             — порт сервера (по умолчанию 8765)
#   S2S_HOST             — хост проверки готовности (по умолчанию 127.0.0.1)
#   S2S_BIN              — команда запуска сервера (авто: локальная из venv либо uvx)
#   QWEN3_TTS_BACKEND    — бэкенд Qwen3-TTS: torch (по умолчанию) или ggml.
#                          torch нужен на CUDA 13 (ggml-библиотека собрана под 12).
#   VAD_MIN_SILENCE_MS   --min_silence_ms (по умолчанию 700; дефолт s2s=64 слишком
#                          рвет реплику на микро-паузах)
#   VAD_UNANSWERED_REOPEN_MS --unanswered_reopen_ms (по умолчанию 1500)
#   S2S_FLAGS            — прочие доп. флаги s2s
#   S2S_STARTUP_TIMEOUT  — сколько секунд ждать подъёма сервера (по умолчанию 300)

set -euo pipefail

# ─── каталог скрипта (отсюда видны .env и uv-проект) ─────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ─── 1. .env ────────────────────────────────────────────────────────────────
ENV_FILE="${ENV_FILE:-.env}"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "✗ Файл $ENV_FILE не найден. Скопируйте .env.example -> .env и заполните." >&2
  exit 1
fi

# set -a экспортирует все читаемые переменные, чтобы они попали в окружение
# сервера и клиента; source пропускает закомментированные строки.
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "✗ OPENAI_API_KEY пуст — LLM сервера s2s не заработает. Заполните $ENV_FILE." >&2
  exit 1
fi

# ─── параметры запуска ───────────────────────────────────────────────────────
S2S_HOST="${S2S_HOST:-127.0.0.1}"
S2S_PORT="${S2S_PORT:-8765}"
LOG_LEVEL="${LOG_LEVEL:-warning}"
# Где искать команду сервера. Пользователь может задать S2S_BIN явно.
# По умолчанию: предпочитаем локальный speech-to-speech (если он установлен
# через `uv sync --extra s2s`), иначе uvx (сам доустановит пакет).
if [[ -z "${S2S_BIN:-}" ]]; then
  if local_bin="$(uv run --no-sync which speech-to-speech 2>/dev/null)" && [[ -n "$local_bin" ]]; then
    S2S_BIN="$local_bin"
  else
    S2S_BIN="uvx --from speech-to-speech speech-to-speech"
  fi
fi
# Первый запуск долгий: uvx ставит пакет, затем s2s качает STT/TTS модели.
S2S_STARTUP_TIMEOUT="${S2S_STARTUP_TIMEOUT:-300}"

# Возвращает 0, если TCP-порт уже открыт.
port_open() { (exec 3<>"/dev/tcp/$1/$2") 2>/dev/null && exec 3>&- 3<&-; }

S2S_STARTED=0

cleanup() {
  echo
  if [[ "$S2S_STARTED" -eq 1 ]]; then
    echo "■ Останавливаю speech-to-speech сервер (pid $S2S_PID) …"
    kill "$S2S_PID" 2>/dev/null || true
    wait "$S2S_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# ─── 2. speech-to-speech сервер ──────────────────────────────────────────────
if port_open "$S2S_HOST" "$S2S_PORT"; then
  echo "• Порт $S2S_HOST:$S2S_PORT уже занят — speech-to-speech, видимо, уже запущен. Пропускаю запуск."
else
  echo "▶ Запуск speech-to-speech сервера (log_level=$LOG_LEVEL) …"
  # Флаги для speech-to-speech >=0.2.11:
  #   --llm_backend chat-completions  — OpenAI Chat Completions API (его держит z.ai)
  #   --responses_api_*               — подключение к LLM (поля унаследованы от
  #                                     responses-api класса); из .env
  #   --qwen3_tts_backend torch       — GGML собран под CUDA 12 и падает на 13;
  #                                     torch работает с любой CUDA
  #   --min_silence_ms / --unanswered_reopen_ms — VAD: дефолты s2s (64/7000) рвут
  #                                     реплику на микро-паузах, вызывая отмену и
  #                                     перезапуск LLM; подняты пороги тишины.
  "$S2S_BIN" \
    --llm_backend chat-completions \
    --responses_api_base_url "${OPENAI_BASE_URL}" \
    --responses_api_api_key "${OPENAI_API_KEY}" \
    --model_name "${LLM_MODEL:-glm-5}" \
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

# ─── 3. наш клиент ───────────────────────────────────────────────────────────
echo "▶ Запуск клиента (uv run run-client) …"
set +e
uv run run-client
CLIENT_EXIT=$?
set -e

exit "$CLIENT_EXIT"
