#!/usr/bin/env bash
#
# setup-aec.sh — настроить программное эхозаглушение (AEC) через PipeWire/
# PulseAudio, чтобы голосовой ассистент не слышал собственный голос из колонок
# и не перебивал сам себя.
#
# Что делает:
#   1. находит дефолтный микрофон (source) и колонки (sink)
#   2. загружает module-echo-cancel, который создаёт ВИРТУАЛЬНЫЙ микрофон
#      «echo-cancelled»: реальный сигнал микрофона минус то, что играет колонка
#      (берётся из sink monitor)
#   3. показывает, какую строку добавить в .env, чтобы клиент читал именно
#      этот виртуальный источник (INPUT_DEVICE=echo-cancelled)
#
# Запуск:  ./setup-aec.sh
# Откат:   ./setup-aec.sh --remove
#
# Модуль живёт только до перезагрузки сервера PipeWire. Чтобы сохранить надолго,
# см. подсказку в конце (pulseaudio config / pipewire context.modules).

set -euo pipefail

AEC_SOURCE_NAME="echo-cancelled"
AEC_SINK_NAME="echo-canceled-playback"   # так модуль называет свой playback-sink
MODULE_NAME="module-echo-cancel"

# ─── зависимости ──────────────────────────────────────────────────────────────
if ! command -v pactl >/dev/null 2>&1; then
  echo "✗ Нет pactl (PulseAudio/PipeWire CLI). Установите pipewire-pulse." >&2
  exit 1
fi

SERVER_NAME="$(pactl info 2>/dev/null | awk -F': ' '/Server Name/{print $2}')"
echo "• Звуковой сервер: ${SERVER_NAME:-неизвестно}"

# ─── --remove ─────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--remove" ]]; then
  echo "▶ Снимаю AEC…"
  id="$(pactl list short modules 2>/dev/null | awk -v m="$MODULE_NAME" '$2==m{print $1}' | head -1 || true)"
  if [[ -n "$id" ]]; then
    # Перед выгрузкой модуля вернуть дефолты на реальные устройства, иначе они
    # будут указывать на исчезнувшие виртуальные source/sink.
    real_src="$(pactl list short sources 2>/dev/null | awk -v s="$AEC_SOURCE_NAME" '$2!=s && $2!~/\.monitor$/ {print $2; exit}')"
    real_sink="$(pactl list short sinks 2>/dev/null | awk -v s="$AEC_SINK_NAME" '$2!=s {print $2; exit}')"
    [[ -n "$real_src" ]] && pactl set-default-source "$real_src" 2>/dev/null || true
    [[ -n "$real_sink" ]] && pactl set-default-sink "$real_sink" 2>/dev/null || true
    pactl unload-module "$id"
    echo "✓ Модуль $MODULE_NAME (id=$id) выгружен."
    echo "  Дефолтный source: $(pactl get-default-source)"
    echo "  Дефолтный sink:   $(pactl get-default-sink)"
  else
    echo "• Модуль $MODULE_NAME не был загружен — ничего делать не нужно."
  fi
  exit 0
fi

# ─── уже загружен? ────────────────────────────────────────────────────────────
if existing="$(pactl list short modules 2>/dev/null | awk -v m="$MODULE_NAME" '$2==m{print $1}' | head -1 || true)" && [[ -n "$existing" ]]; then
  echo "✓ AEC уже загружен (module id=$existing)."
  echo "  Дефолтный source: $(pactl get-default-source)"
  exit 0
fi

# ─── находим дефолтные устройство ─────────────────────────────────────────────
DEFAULT_SOURCE="$(pactl get-default-source 2>/dev/null || true)"
DEFAULT_SINK="$(pactl get-default-sink 2>/dev/null || true)"

if [[ -z "$DEFAULT_SOURCE" || -z "$DEFAULT_SINK" ]]; then
  echo "✗ Не удалось определить дефолтные source/sink." >&2
  echo "  Задайте их через pactl set-default-source / set-default-sink." >&2
  exit 1
fi
echo "• Микрофон (source):  $DEFAULT_SOURCE"
echo "• Колонки  (sink):    $DEFAULT_SINK"

# Полное имя source-монитора колонок нужно модулю как reference-сигнал эха.
SINK_MONITOR="${DEFAULT_SINK}.monitor"

# ─── загрузка module-echo-cancel ──────────────────────────────────────────────
echo "▶ Загружаю $MODULE_NAME…"
# source_master = реальный микрофон, sink_master = колонки.
# Модуль вычитает эхо: на выходе source_name — чистый сигнал микрофона,
# sink_name — промежуточный sink (в него играет приложение, модуль гонит на
# реальные колонки). Метод webrtc хорошо подходит для голоса.
pactl load-module "$MODULE_NAME" \
  source_name="$AEC_SOURCE_NAME" \
  sink_name="$AEC_SINK_NAME" \
  source_master="$DEFAULT_SOURCE" \
  sink_master="$DEFAULT_SINK" \
  aec_method=webrtc \
  aec_args="analog_gain_control=1\ digital_gain_control=1" \
  2>&1 | sed 's/^/  /' || {
    echo "✗ Не удалось загрузить $MODULE_NAME." >&2
    exit 1
  }

# Чтобы эхо гасилось, вывод (TTS) должен идти в AEC-sink, а не прямо в колонки.
# Делаем его дефолтным sink-ом — тогда приложение пишет в него автоматически.
pactl set-default-sink "$AEC_SINK_NAME"
# И делаем AEC-source дефолтным источником: PortAudio (sounddevice) обращается к
# PulseAudio/PipeWire только через абстрактное "default", а не по source-имени,
# поэтому INPUT_DEVICE должен быть пустым — клиент возьмёт именно этот источник.
pactl set-default-source "$AEC_SOURCE_NAME"

echo
echo "✓ AEC настроен."
echo "  Виртуальный микрофон (source):  $AEC_SOURCE_NAME  (теперь дефолтный)"
echo "  Виртуальный вывод  (sink):      $AEC_SINK_NAME  (теперь дефолтный)"
echo
echo "▶ .env: INPUT_DEVICE можно оставить пустым — клиент берёт системный"
echo "  дефолтный источник, а это теперь $AEC_SOURCE_NAME."
echo
echo "  Проверить, что источник виден:    pactl get-default-source"
echo "  Снять AEC:                        ./setup-aec.sh --remove"
echo
echo "ℹ Модуль живёт до рестарта PipeWire. Чтобы сделать постоянным,"
echo "  перенесите load-module строку в /etc/pulse/default.pa (PulseAudio)"
echo "  или в pipewire context.modules (создав ~/.config/pipewire/pipewire.conf.d/aec.conf)."
