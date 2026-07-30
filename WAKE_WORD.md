# План реализации wake word на стороне клиента

> Статус: реализованы обе описанные итерации — базовая активация и окно
> продолжения диалога. Документ остается описанием архитектуры, инвариантов и
> тестовой матрицы реализации.

## 1. Цель

Добавить локальную активацию голосового ассистента по ключевой фразе, не изменяя
пакет `speech-to-speech` и его Realtime-протокол.

До активации клиент должен:

- постоянно читать микрофон локально;
- выполнять локальное распознавание wake word;
- не отправлять аудио в WebSocket;
- не запускать серверный VAD, STT или LLM.

После активации клиент должен:

1. проиграть локальный earcon;
2. дождаться фактического завершения earcon;
3. начать отправлять в `speech-to-speech` только следующие микрофонные блоки;
4. сохранить действующие AEC и barge-in;
5. после завершения диалога вернуться в режим ожидания wake word.

Целевая схема:

```text
                         локально
┌────────────┐    ┌───────────────────────┐
│ microphone │──▶│ raw PCM 24 kHz blocks │
└────────────┘    └───────────┬───────────┘
                              │
                     ┌────────▼─────────┐
                     │ activation gate  │
                     └──────┬───────┬───┘
                            │       │
              SLEEPING      │       │ ACTIVE
                            │       │
                 ┌──────────▼──┐    └──────────────▶ WebSocket
                 │ 24 → 16 kHz │                      PCM 24 kHz
                 │ wake word   │
                 └──────────┬──┘
                            │ detected
                            ▼
                       local earcon
                            │ played
                            ▼
                          ACTIVE
```

## 2. Зафиксированные архитектурные решения

### 2.1. Wake word реализуется только в клиенте

Сервер продолжает получать стандартные события:

```json
{
  "type": "input_audio_buffer.append",
  "audio": "<base64 pcm16>"
}
```

Серверу не требуется знать:

- что клиент находится в sleeping-режиме;
- какой wake word используется;
- когда и как проигрывается earcon;
- почему между аудиоблоками был длительный перерыв.

### 2.2. WebSocket остается открытым

Соединение и Realtime-сессия создаются при запуске приложения и остаются
открытыми в sleeping-режиме. На wake word меняется только допуск аудиоблоков к
WebSocket.

Открывать соединение только после wake word не следует: это добавит к каждой
команде подключение, настройку сессии и возможный прогрев моделей.

### 2.3. Используется один поток микрофона

Нельзя одновременно открывать отдельные входные потоки на 16 и 24 кГц. Один
поток захватывает PCM на 24 кГц:

- исходные 24 кГц отправляются серверу после активации;
- копия ресемплируется в 16 кГц для локального wake detector.

### 2.4. Сервер получает аудио только после earcon

В состоянии `EARCON` микрофон продолжает читаться, но его блоки отбрасываются.
Первый `input_audio_buffer.append` разрешается только после callback от
playback-потока, подтверждающего завершение earcon и короткой хвостовой паузы.

Это гарантирует, что сервер не получит:

- сам wake word;
- trigger-блок, на котором сработал detector;
- локальный сигнал активации;
- акустический хвост earcon.

### 2.5. Earcon проигрывается через существующий `AudioPlayer`

Не следует вызывать `sounddevice.play()` параллельно с уже открытым
`sounddevice.OutputStream`. Earcon и аудио ассистента должны использовать один
выходной поток и одну очередь.

Для этого `AudioPlayer` получит поддержку:

- локального PCM;
- completion barrier;
- callback после фактической записи предыдущих элементов в output stream.

### 2.6. Во время активного цикла микрофон не закрывается

После earcon клиент передает все микрофонные блоки, включая блоки во время речи
ассистента. Благодаря уже настроенному AEC сервер продолжает получать голос
пользователя и сохраняет barge-in.

Wake detector в активном состоянии не используется. Повторное произнесение
wake word внутри активного цикла не начинает новую активацию.

### 2.7. Реализация разбивается на две итерации

Итерация 1 работает по принципу «один wake — один диалоговый цикл»:

Базовое поведение:

```text
wake word → earcon → команда → ответ/перебивания → завершение ответа → sleeping
```

Под «одним циклом» понимается не обязательно один server response. Вызов
инструмента может создавать промежуточный `response.done`, затем
`function_call_output` и еще один ответ. В sleeping можно переходить только
после финального ответа и опустошения playback queue.

Итерация 2 добавляет `FOLLOW_UP`: после финального ответа клиент оставляет gate
открытым на ограниченное время. Поэтому ответ на уточняющий вопрос ассистента и
естественное продолжение разговора не требуют повторного wake word. Обе
итерации входят в текущую реализацию; значение `FOLLOW_UP_WINDOW_SEC=0`
возвращает поведение первой итерации.

### 2.8. Ошибки wake word обрабатываются fail-closed

Если `WAKE_WORD_MODEL` непустой, но модель не загрузилась, ресемплер не работает
или earcon невозможно воспроизвести, клиент не должен незаметно переходить в
режим постоянной отправки микрофона.

Допустимы только два явных варианта:

- `WAKE_WORD_MODEL` содержит имя или путь — загрузить модель либо завершить
  запуск с понятной ошибкой;
- `WAKE_WORD_MODEL` пуст — запустить старое always-on поведение.

Отдельный boolean-переключатель не вводится. Пробелы вокруг значения удаляются,
поэтому пустая или состоящая только из пробелов строка означает отсутствие
модели.

Wake word является механизмом активации, но не аутентификацией пользователя и
не разрешением на опасные действия.

## 3. Состояния activation gate

Минимальный набор состояний:

```python
from enum import StrEnum


class ActivationState(StrEnum):
    SLEEPING = "sleeping"
    EARCON = "earcon"
    ACTIVE = "active"
    FOLLOW_UP = "follow_up"
    DEACTIVATING = "deactivating"
    STOPPED = "stopped"
```

Назначение:

| Состояние      | Wake detector | Отправка PCM серверу | Назначение                                  |
| -------------- | ------------: | -------------------: | ------------------------------------------- |
| `SLEEPING`     |       включен |                  нет | ожидание ключевой фразы                     |
| `EARCON`       |      выключен |                  нет | сигнал и post-earcon пауза                  |
| `ACTIVE`       |      выключен |                   да | обычный realtime-диалог и barge-in          |
| `FOLLOW_UP`    |      выключен |                   да | ограниченное окно продолжения разговора     |
| `DEACTIVATING` |      выключен |                  нет | нисходящий сигнал закрытия диалогового окна |
| `STOPPED`      |      выключен |                  нет | остановка приложения                        |

Основные переходы:

```text
startup
   │
   ▼
SLEEPING ──wake detected──▶ EARCON ──earcon drained──▶ ACTIVE
   ▲                                                    │  ▲
   │                                                    │  │ new speech
   │                   FOLLOW_UP ◀──final drained───────┘  │
   │                       └───────────────────────────────┘
   │ follow-up timeout
   └────cue drained──── DEACTIVATING

любое состояние ──shutdown──▶ STOPPED
```

Дополнительные защитные переходы:

- `EARCON → SLEEPING`, если playback завершился с ошибкой;
- `ACTIVE → SLEEPING`, если пользователь не начал говорить за заданный timeout;
- `ACTIVE → SLEEPING`, если превышена максимальная длительность активации;
- `FOLLOW_UP → ACTIVE`, если сервер сообщил о новой речи пользователя;
- `FOLLOW_UP → DEACTIVATING`, если окно завершилось без новой речи;
- `DEACTIVATING → SLEEPING`, когда сигнал закрытия прошел playback barrier.

В `FOLLOW_UP` передача микрофона временно продолжается. Иначе сервер не
сможет обнаружить речь, начавшуюся на границе завершения playback, и переход
`FOLLOW_UP → ACTIVE` будет невозможен. Wake detector при этом остается
выключенным.

В `DEACTIVATING` передача PCM уже закрыта, а wake detector еще выключен.
Отдельный нисходящий сигнал сообщает пользователю, что следующая команда снова
потребует wake word. Wake detector включается только после сигнала и хвостовой
тишины, поэтому cue не может активировать ассистента сам.

## 4. Потоки и синхронизация

В приложении остаются три рабочих потока:

```text
main thread       WebSocket recv и обработка server events
mic thread        capture → wake/gate → WebSocket send
playback thread   earcon и response audio
```

`ActivationGate` вызывается из всех трех потоков, поэтому его состояние должно
быть защищено `threading.Lock` или `threading.RLock`.

Пример публичного интерфейса:

```python
from dataclasses import dataclass
from threading import RLock
from time import monotonic


@dataclass(frozen=True)
class ActivationSnapshot:
    activation_id: int
    interaction_revision: int


class ActivationGate:
    def __init__(
        self,
        *,
        requires_wake: bool,
        listen_timeout_sec: float,
        follow_up_window_sec: float,
        max_active_sec: float,
        clock=monotonic,
    ) -> None: ...

    def begin_earcon(self) -> int | None: ...
    def finish_earcon(self, activation_id: int) -> bool: ...
    def note_user_speech(self) -> None: ...
    def note_response_started(self) -> None: ...
    def snapshot(self) -> ActivationSnapshot: ...
    def finish_response_if_current(
        self, snapshot: ActivationSnapshot
    ) -> bool: ...
    def finish_deactivation(self, activation_id: int) -> bool: ...
    def check_timeouts(self) -> str | None: ...

    def should_forward_audio(self) -> bool:
        with self._lock:
            return self._state in {
                ActivationState.ACTIVE,
                ActivationState.FOLLOW_UP,
            }
```

Callback из playback queue всегда должен содержать `activation_id`. Благодаря
этому запоздавший callback старой активации не сможет открыть новый цикл.

`interaction_revision` изменяется при новой речи пользователя или при создании
нового server response. Completion callback старого ответа перед переходом в
sleeping сравнивает сохраненную revision с текущей. Если пользователь успел
перебить ответ, callback становится no-op.

## 5. Изменение захвата аудио

### 5.1. Разделить capture и wire encoding

Текущий генератор необходимо разделить на две операции:

```python
def capture_pcm_blocks(
    *,
    sample_rate: int,
    block_size: int,
    channels: int = 1,
    device: int | str | None = None,
) -> Iterator[np.ndarray]:
    with sd.InputStream(
        samplerate=sample_rate,
        blocksize=block_size,
        channels=channels,
        dtype="int16",
        device=device,
    ) as stream:
        while True:
            block, overflowed = stream.read(block_size)
            if overflowed:
                # Записать warning/metric, но не ломать поток.
                pass
            yield np.asarray(block, dtype=np.int16).reshape(-1).copy()


def encode_block(samples: np.ndarray) -> str:
    return base64.b64encode(samples.tobytes()).decode("ascii")
```

Копирование блока намеренное: оно отделяет срок жизни массива от внутреннего
буфера PortAudio.

Существующий `capture_blocks()` можно временно оставить как совместимую
обертку:

```python
def capture_blocks(**kwargs: object) -> Iterator[str]:
    for block in capture_pcm_blocks(**kwargs):
        yield encode_block(block)
```

После перевода `RealtimeClient` на raw PCM обертку можно удалить.

### 5.2. Уменьшить размер блока

Рекомендуемый размер — 80 мс:

```text
24 000 Hz × 0.080 sec = 1 920 samples
16 000 Hz × 0.080 sec = 1 280 samples
```

Поэтому новый default:

```env
BLOCK_SIZE=1920
```

Преимущества:

- wake detector получает естественный 80-миллисекундный кадр;
- максимум дополнительной задержки до отправки первого блока снижается с
  200 до 80 мс;
- меньше вероятность, что начало команды окажется в trigger-блоке;
- сервер продолжает получать обычный непрерывный PCM.

Если в нагрузочном тесте количество WebSocket messages окажется слишком
большим, клиент может объединять 2–3 активных raw-блока перед кодированием.
Объединение разрешается только на wire-path и не должно менять cadence wake
detector.

## 6. Ресемплинг 24 → 16 кГц

Нужно создать самостоятельный модуль:

```text
src/audio/resample.py
```

Интерфейс:

```python
from typing import Protocol


class AudioResampler(Protocol):
    def process(self, samples: np.ndarray) -> np.ndarray: ...
    def reset(self) -> None: ...
```

Production-реализация должна использовать stateful resampler. Предпочтителен
`soxr`: он компактен и не требует добавлять весь SciPy в базовый клиент.

Ресемплер должен:

- принимать mono `int16` 24 кГц;
- преобразовывать сигнал в `float32` или `int16` 16 кГц согласно контракту
  detector;
- сохранять внутреннее состояние между блоками;
- не накапливать временной drift;
- сбрасываться при старте приложения и при смене input device.

Даже при блоках 1920/1280 после ресемплера нужен небольшой frame accumulator:

```python
class WakeFrameBuffer:
    def __init__(self, frame_size: int = 1280) -> None:
        self._frame_size = frame_size
        self._pending = np.zeros(0, dtype=np.int16)

    def push(self, samples: np.ndarray) -> list[np.ndarray]:
        combined = np.concatenate((self._pending, samples))
        count = combined.size // self._frame_size
        frames = [
            combined[i * self._frame_size : (i + 1) * self._frame_size]
            for i in range(count)
        ]
        self._pending = combined[count * self._frame_size :]
        return frames

    def reset(self) -> None:
        self._pending = np.zeros(0, dtype=np.int16)
```

Это защищает detector от округлений ресемплера и нестандартных размеров
PortAudio-блоков.

## 7. Wake detector

### 7.1. Расположение

Новый модуль:

```text
src/audio/wakeword.py
```

Каталог для вручную скачанных и custom-моделей:

```text
.local/openwakeword/
```

Каталог добавляется в `.gitignore`.

Это application-owned каталог для всех вручную установленных весов. Реестр
встроенных моделей, например `hey_jarvis`, находится в пакете openWakeWord и
дает adapter-у каноническое имя файла и download URL. Сами веса могут уже
находиться в package resources либо быть заранее скачаны в
`.local/openwakeword`. Клиент должен поддерживать оба варианта.

### 7.2. Контракт

```python
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class WakeDetection:
    detected: bool
    model_name: str | None
    score: float


class WakeDetector(Protocol):
    def process(self, frame: np.ndarray) -> WakeDetection: ...
    def reset(self) -> None: ...
```

Production adapter использует openWakeWord, но остальное приложение зависит
только от этого протокола. В unit tests подставляется scripted fake.

### 7.3. Детекция

Detector должен поддерживать:

- имя или путь модели;
- threshold;
- software gain;
- несколько последовательных положительных кадров (`patience`);
- cooldown;
- встроенный VAD threshold;
- noise suppression;
- диагностический best score без логирования PCM.

Упрощенный алгоритм:

```python
def process(self, frame: np.ndarray) -> WakeDetection:
    amplified = np.clip(
        frame.astype(np.float32) * self._gain,
        -32768,
        32767,
    ).astype(np.int16)

    scores = self._model.predict(amplified)
    name, score = max(scores.items(), key=lambda item: item[1])

    if score >= self._threshold:
        self._consecutive_hits += 1
    else:
        self._consecutive_hits = 0

    now = monotonic()
    detected = (
        self._consecutive_hits >= self._patience
        and now - self._last_detection >= self._cooldown_sec
    )
    if detected:
        self._last_detection = now
        self._consecutive_hits = 0

    return WakeDetection(
        detected=detected,
        model_name=name if detected else None,
        score=float(score),
    )
```

Перед реализацией необходимо проверить точный формат и frame contract версии
openWakeWord, зафиксированной в `uv.lock`.

### 7.4. Разрешение и загрузка моделей

`WAKE_WORD_MODEL` поддерживает три формы:

```dotenv
# Встроенное имя из реестра openWakeWord:
WAKE_WORD_MODEL=hey_jarvis

# Имя вручную скачанной/custom-модели в WAKE_WORD_MODEL_DIR:
WAKE_WORD_MODEL=hey_findus

# Явный путь:
WAKE_WORD_MODEL=/opt/models/custom_wakeword.onnx
```

Приоритет разрешения должен быть строгим и наблюдаемым:

1. Если значение является существующим путем к файлу — использовать этот файл.
2. Иначе пройти по встроенному реестру openWakeWord, получить ожидаемое имя
   файла для выбранного inference framework и проверить этот файл в
   `WAKE_WORD_MODEL_DIR`.
3. Затем выполнить общий поиск по `WAKE_WORD_MODEL_DIR`. Этот этап находит
   custom-модели, которых нет во встроенном реестре.
4. Если локального файла нет, проверить зарегистрированный package resource
   для точного встроенного имени.
5. Если ничего не найдено — завершить запуск с ошибкой и перечислить проверенные
   источники.

Локальная версия проверяется раньше package resource намеренно. Это позволяет
положить обновленную или исправленную версию встроенной модели в
`.local/openwakeword`, не изменяя установленный пакет:

```text
WAKE_WORD_MODEL=hey_jarvis
.local/openwakeword/hey_jarvis_v0.1.onnx
```

Для поиска по короткому имени используется привязка к началу имени файла:

```python
def model_name_pattern(name: str) -> re.Pattern[str]:
    normalized = re.escape(name.replace(" ", "_"))
    return re.compile(rf"^{normalized}(_v|\.)")
```

Поэтому `jarvis` не должен случайно совпадать с `hey_jarvis`. Расширение
добавлять в `WAKE_WORD_MODEL` не требуется:

```text
WAKE_WORD_MODEL=hey_findus

подходящие файлы:
  .local/openwakeword/hey_findus.onnx
  .local/openwakeword/hey_findus_v0.1.onnx
```

Файлы другого inference framework игнорируются. Если найдено несколько
подходящих файлов выбранного framework, resolver возвращает их все без
дубликатов, а openWakeWord загружает их как набор моделей. В startup log должны
быть перечислены все выбранные пути.

Упрощенный resolver:

```python
def resolve_wake_models(
    *,
    configured: str,
    model_dir: Path,
    inference_framework: str,
    builtin_models: Mapping[str, Mapping[str, str]],
) -> list[Path]:
    direct = Path(configured).expanduser()
    if direct.is_file():
        return [direct.resolve()]

    extension = f".{inference_framework}"
    pattern = model_name_pattern(configured)
    resolved: list[Path] = []

    # Phase 1: registered filename, but stored in the application model dir.
    for metadata in builtin_models.values():
        filename = Path(metadata["download_url"]).name
        filename = Path(filename).with_suffix(extension).name
        if pattern.match(filename):
            candidate = model_dir / filename
            if candidate.is_file():
                resolved.append(candidate.resolve())

    # Phase 2: custom/manual files absent from the built-in registry.
    for candidate in model_dir.glob(f"*{extension}"):
        path = candidate.resolve()
        if pattern.match(candidate.name) and path not in resolved:
            resolved.append(path)

    if resolved:
        return resolved

    # Phase 3: exact built-in name from package resources.
    builtin = builtin_models.get(configured)
    if builtin is not None:
        package_path = Path(builtin["model_path"]).with_suffix(extension)
        if package_path.is_file():
            return [package_path.resolve()]

    raise FileNotFoundError(
        f"Wake model {configured!r} was not found as a file, "
        f"in {model_dir}, or in openWakeWord package resources"
    )
```

Если `WAKE_WORD_MODEL` выглядит как путь — содержит `/`, начинается с `.`/`~`
или имеет расширение `.onnx`/`.tflite` — но файла нет, нужно сразу сообщить о
неверном пути. Такое значение нельзя интерпретировать как короткое имя и
продолжать fallback-поиск.

`melspectrogram`, `embedding` и VAD являются общими служебными моделями
openWakeWord. Если совместимые `melspectrogram.<framework>` и
`embedding_model.<framework>` находятся в `WAKE_WORD_MODEL_DIR`, adapter явно
передает их пути в openWakeWord. Иначе используются package resources.
Источник каждой загруженной модели записывается в startup log без содержимого
файлов.

Желательно добавить отдельную команду:

```text
uv run prepare-wakeword
```

Она работает по тем же правилам:

- существующий явный путь только проверяется;
- найденная модель из `.local/openwakeword` только проверяется;
- существующий package resource только проверяется;
- отсутствующая встроенная модель скачивается штатным downloader openWakeWord
  с `target_directory=WAKE_WORD_MODEL_DIR`;
- неизвестное custom-имя не скачивается автоматически и завершается подсказкой
  поместить файл в `WAKE_WORD_MODEL_DIR`.

Команда заранее проверяет:

- wake-word model;
- melspectrogram model;
- embedding model;
- VAD model, если он включен.

Основной `run-client` не должен внезапно начинать длительную загрузку после
запуска аудиоустройств.

## 8. Earcon и playback completion barrier

### 8.1. Новые типы элементов очереди

Очередь `AudioPlayer` должна содержать не `object`, а явные типы:

```python
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class PcmItem:
    data: bytes


@dataclass(frozen=True)
class BarrierItem:
    callback: Callable[[], None]


@dataclass(frozen=True)
class StopItem:
    pass
```

Публичный API:

```python
class AudioPlayer:
    def put_delta(self, b64: str) -> None: ...
    def put_pcm(self, samples: np.ndarray) -> None: ...
    def put_barrier(self, callback: Callable[[], None]) -> None: ...
```

Playback loop:

```python
while True:
    item = self._queue.get()

    if isinstance(item, StopItem):
        return
    if isinstance(item, PcmItem):
        stream.write(np.frombuffer(item.data, dtype=np.int16))
        continue
    if isinstance(item, BarrierItem):
        item.callback()
```

`BarrierItem` выполняется только после всех поставленных перед ним PCM chunks.
Это queue/output-stream barrier, а не аппаратное подтверждение от ЦАП. Для
earcon разница покрывается тишиной, включенной в конец того же PCM item. Ее
длительность должна быть не меньше наблюдаемой output latency устройства.

### 8.2. Генерация earcon

Earcon генерируется локально с sample rate output pipeline, сейчас 16 кГц:

```python
def make_earcon(
    *,
    sample_rate: int,
    frequency_hz: float,
    duration_sec: float,
    volume: float,
    trailing_silence_sec: float,
) -> np.ndarray:
    tone_count = int(sample_rate * duration_sec)
    silence_count = int(sample_rate * trailing_silence_sec)

    phase = np.arange(tone_count, dtype=np.float32) / sample_rate
    tone = np.sin(2 * np.pi * frequency_hz * phase)

    # Короткий fade-in/fade-out устраняет щелчки.
    fade_count = min(int(sample_rate * 0.01), tone_count // 2)
    envelope = np.ones(tone_count, dtype=np.float32)
    envelope[:fade_count] = np.linspace(0.0, 1.0, fade_count)
    envelope[-fade_count:] = np.linspace(1.0, 0.0, fade_count)

    pcm = (tone * envelope * volume * 32767).astype(np.int16)
    silence = np.zeros(silence_count, dtype=np.int16)
    return np.concatenate((pcm, silence))
```

Рекомендуемые начальные значения:

```env
EARCON_FREQUENCY_HZ=880
EARCON_DURATION_MS=120
EARCON_VOLUME=0.25
POST_EARCON_SILENCE_MS=120
```

Хвостовая тишина включается в тот же PCM item. После него в очередь ставится
barrier:

```python
activation_id = gate.begin_earcon()
if activation_id is not None:
    player.put_pcm(earcon)
    player.put_barrier(
        lambda: gate.finish_earcon(
            activation_id,
            timeout_sec=settings.activation_listen_timeout_sec,
        )
    )
```

Первый mic block, прочитанный после смены состояния на `ACTIVE`, отправляется
серверу.

### 8.3. Ошибка playback

При невозможности открыть output device:

- earcon completion не должен переводить gate в `ACTIVE`;
- gate возвращается в `SLEEPING`;
- пишется событие `wake.earcon_failed`;
- аудио не начинает отправляться в Realtime-сессию.

Для этого при аварии playback thread должен выполнить error-callback для
необработанных barriers или уведомить отдельный `on_error`.

## 9. Интеграция в микрофонный цикл

Целевая логика `_start_mic()`:

```python
def _pump() -> None:
    for pcm24 in capture_pcm_blocks(
        sample_rate=settings.sample_rate,
        block_size=settings.block_size,
        channels=settings.channels,
        device=settings.input_device,
    ):
        if self._stop.is_set():
            return

        state = self.activation_gate.state

        if state is ActivationState.SLEEPING:
            pcm16 = self.wake_resampler.process(pcm24)
            for frame in self.wake_frame_buffer.push(pcm16):
                detection = self.wake_detector.process(frame)
                if detection.detected:
                    self._on_wake_detected(detection)
                    break
            continue

        if state is ActivationState.EARCON:
            # Mic остается открытым, но earcon и его эхо серверу не передаются.
            continue

        if state is ActivationState.DEACTIVATING:
            # Диалог уже закрыт; detector включится после локального cue.
            continue

        if state in (ActivationState.ACTIVE, ActivationState.FOLLOW_UP):
            self._send(
                conn,
                {
                    "type": "input_audio_buffer.append",
                    "audio": encode_block(pcm24),
                },
            )
```

После детекции trigger-блок никогда не проходит в ACTIVE-ветку: текущая
итерация завершается через `continue`.

При пустом `WAKE_WORD_MODEL` gate создается сразу в состоянии `ACTIVE`, и клиент
сохраняет существующее always-on поведение. В этом режиме переходы `FOLLOW_UP`
и `SLEEPING` полностью отключены: финальный `response.done` не закрывает
передачу микрофона.

## 10. Интеграция с server events

### 10.1. События речи пользователя

Если сервер отправляет Realtime-события начала и конца речи, добавить handlers:

```text
input_audio_buffer.speech_started
input_audio_buffer.speech_stopped
```

На `speech_started`:

- отметить, что после wake действительно началась команда;
- увеличить `interaction_revision`;
- отменить запланированный переход в sleeping;
- продлить active deadline;
- записать latency от earcon completion до начала речи.

На `speech_stopped`:

- отметить завершение пользовательской реплики;
- отключить короткий listen timeout: команда уже была принята;
- продлить deadline на время обработки ответа.

Точные имена и payload событий необходимо подтвердить по фактическому потоку
сервера. Если он не отправляет эти события клиенту, базовый режим должен
работать по `response.*` events и `MAX_ACTIVE_SEC`. В таком fallback короткий
listen timeout использовать нельзя: иначе медленный STT/LLM может быть ошибочно
принят за отсутствие команды. Любой `response.created`, первый audio delta или
другое достоверное событие обработки пользовательской реплики также отключает
listen timeout.

### 10.2. Function calls не завершают активацию

Текущий `response.done`, содержащий завершенные function calls, является
промежуточным:

```python
def _on_response_done(self, conn: RealtimeConnection, event: Any) -> None:
    calls = list(self._pending.values())
    self._pending.clear()

    if calls:
        for call in calls:
            self._send_tool_output(conn, call)
        self._send(conn, {"type": "response.create"})
        self.activation_gate.note_intermediate_response()
        return

    self._schedule_final_response_completion(event)
```

При нескольких function calls сначала отправляются все outputs, а затем один
`response.create`. Это одновременно упрощает корректное определение финального
ответа.

### 10.3. Финальный ответ

После финального `response.done` все предыдущие audio deltas уже должны
находиться в playback queue. В очередь добавляется barrier:

```python
snapshot = gate.snapshot()

player.put_barrier(
    lambda: gate.finish_response_if_current(snapshot)
)
```

При положительном `FOLLOW_UP_WINDOW_SEC` callback переводит gate в `FOLLOW_UP`.
Во время этого окна микрофон по-прежнему передается серверу, поэтому
пограничный barge-in и ответ на уточняющий вопрос остаются возможными. При
нулевом значении callback переводит gate в `DEACTIVATING`, проигрывает сигнал
закрытия и затем включает `SLEEPING`.

Если во время ответа пользователь использовал barge-in:

- сервер регистрирует новую речь;
- `interaction_revision` увеличивается;
- completion callback старого ответа видит несовпадение revision;
- gate остается `ACTIVE`;
- новый ответ проходит в том же activation cycle.

### 10.4. Защитные timeout

Нужны три независимых timeout:

| Timeout                         | Начальное значение | Назначение                                  |
| ------------------------------- | -----------------: | ------------------------------------------- |
| `ACTIVATION_LISTEN_TIMEOUT_SEC` |                  8 | после earcon пользователь не начал говорить |
| `FOLLOW_UP_WINDOW_SEC`          |                 10 | продолжение диалога без нового wake word    |
| `MAX_ACTIVE_SEC`                |                 90 | аварийное закрытие зависшей активации       |

Timeout нельзя реализовывать через `sleep()` в mic или main thread. Deadline
хранится в `ActivationGate` и проверяется на каждом mic block либо отдельным
короткоживущим timer.

При истечении timeout:

- gate переходит в `DEACTIVATING`, а после сигнала — в `SLEEPING`;
- detector, resampler и frame buffer сбрасываются;
- записывается причина деактивации;
- аудио по-прежнему не отправляется до следующего wake.

## 11. Конфигурация

Добавить в `Settings`:

```python
@dataclass(frozen=True)
class WakeWordSettings:
    model: str | None
    model_dir: Path
    inference_framework: str
    threshold: float
    gain: float
    patience: int
    cooldown_sec: float
    vad_threshold: float
    noise_suppression: bool
    earcon_frequency_hz: float
    earcon_duration_ms: int
    earcon_volume: float
    post_earcon_silence_ms: int
    deactivation_earcon_start_hz: float
    deactivation_earcon_end_hz: float
    deactivation_earcon_duration_ms: int
    deactivation_earcon_volume: float
    post_deactivation_earcon_silence_ms: int
    activation_listen_timeout_sec: float
    follow_up_window_sec: float
    max_active_sec: float
```

`.env.example`:

```dotenv
# A non-empty model enables the local wake-word gate. Microphone audio is not
# sent to the Realtime server until the wake word and earcon have completed.
# Leave empty to preserve the existing always-on microphone mode.
WAKE_WORD_MODEL=hey_jarvis
WAKE_WORD_MODEL_DIR=.local/openwakeword
WAKE_WORD_INFERENCE_FRAMEWORK=onnx
WAKE_WORD_THRESHOLD=0.35
WAKE_WORD_GAIN=1.0
WAKE_WORD_PATIENCE=1
WAKE_WORD_COOLDOWN_SEC=1.5
WAKE_WORD_VAD_THRESHOLD=0.5
WAKE_WORD_NOISE_SUPPRESSION=true

EARCON_FREQUENCY_HZ=880
EARCON_DURATION_MS=120
EARCON_VOLUME=0.25
POST_EARCON_SILENCE_MS=120

DEACTIVATION_EARCON_START_HZ=660
DEACTIVATION_EARCON_END_HZ=440
DEACTIVATION_EARCON_DURATION_MS=180
DEACTIVATION_EARCON_VOLUME=0.2
POST_DEACTIVATION_EARCON_SILENCE_MS=100

ACTIVATION_LISTEN_TIMEOUT_SEC=8
FOLLOW_UP_WINDOW_SEC=10
MAX_ACTIVE_SEC=90

# 80 ms at 24 kHz. Produces one 1280-sample wake frame after resampling to 16 kHz.
BLOCK_SIZE=1920
```

Парсинг конфигурации должен проверять:

- `0 < threshold <= 1`;
- `gain > 0`;
- `patience >= 1`;
- `inference_framework` равен `onnx` или `tflite`;
- положительные timeout;
- `0 <= volume <= 1`;
- существование явного model path;
- `SAMPLE_RATE == 24000` для выбранной реализации wire/resampler;
- `OUTPUT_SAMPLE_RATE == 16000` для текущего playback pipeline.

Секреты и абсолютные пользовательские пути не должны попадать в логи.

## 12. Зависимости

Добавить клиентские зависимости:

```toml
dependencies = [
  # existing dependencies...
  "openwakeword>=0.6.0",
  "soxr>=0.5",
]
```

Если необходимо сохранить минимальную установку клиента без wake word, можно
оформить extra:

```toml
[project.optional-dependencies]
wakeword = [
  "openwakeword>=0.6.0",
  "soxr>=0.5",
]
```

Но тогда launcher и документация обязаны устанавливать этот extra, а
непустой `WAKE_WORD_MODEL` без зависимостей должен завершаться явной ошибкой.

Если `.env.example` поставляет непустой `WAKE_WORD_MODEL`, проще и надежнее
сделать эти пакеты базовыми зависимостями.

После изменения зависимостей обновить `uv.lock`.

## 13. Composition root

Создание компонентов должно остаться централизованным:

```python
def build_client(settings: Settings) -> RealtimeClient:
    player = AudioPlayer(
        sample_rate=settings.output_sample_rate,
        channels=settings.channels,
    )

    requires_wake = settings.wake_word.model is not None

    gate = ActivationGate(
        requires_wake=requires_wake,
        listen_timeout_sec=settings.wake_word.activation_listen_timeout_sec,
        follow_up_window_sec=settings.wake_word.follow_up_window_sec,
        max_active_sec=settings.wake_word.max_active_sec,
    )

    wake_detector = (
        OpenWakeWordDetector.from_settings(settings.wake_word)
        if requires_wake
        else None
    )
    wake_resampler = (
        SoxrWakeResampler(input_rate=24000, output_rate=16000)
        if requires_wake
        else None
    )

    return RealtimeClient(
        settings=settings,
        deps=deps,
        player=player,
        activation_gate=gate,
        wake_detector=wake_detector,
        wake_resampler=wake_resampler,
    )
```

`RealtimeClient` не должен сам читать environment или искать model files.
Значение `WAKE_WORD_MODEL` нормализуется в `Settings.from_env()`:

```python
raw_wake_model = _get("WAKE_WORD_MODEL", "").strip()
wake_model = raw_wake_model or None
```

`ActivationGate` хранит производный признак `requires_wake`, чтобы не выводить
режим только из текущего состояния. Когда модель не задана, методы rearm и
timeout остаются no-op и существующий always-on режим не меняется после первого
ответа.

## 14. Логи и измерения

Добавить структурированные сообщения без записи текста пользователя и PCM:

```text
wake.ready
wake.score_debug
wake.detected
wake.earcon_started
wake.earcon_completed
wake.earcon_failed
wake.activation_started
wake.first_audio_forwarded
wake.user_speech_started
wake.final_response_seen
wake.follow_up_started
wake.deactivated
wake.activation_timeout
wake.max_active_timeout
wake.capture_overflow
```

Поля:

```text
activation_id
state_from
state_to
reason
model_name
wake_score
wake_to_earcon_ms
earcon_to_first_audio_ms
active_duration_ms
interaction_revision
```

Raw audio и полный transcript в wake-логах не сохраняются.

Debug scores должны быть отключены по умолчанию, иначе лог будет получать
событие каждые 80 мс.

## 15. Автоматические тесты

### 15.1. Capture

- raw PCM сохраняет значения и dtype;
- mono block имеет ожидаемую форму;
- `encode_block()` round-trip не меняет samples;
- overflow не завершает generator;
- входной массив копируется и не зависит от следующего PortAudio read.

### 15.2. Resampler и frame buffer

- 1920 samples @ 24 кГц дают 1280 samples @ 16 кГц;
- последовательность блоков не накапливает drift;
- произвольные размеры корректно собираются в кадры по 1280;
- `reset()` удаляет pending samples;
- zero signal остается zero signal;
- синус после ресемплинга сохраняет ожидаемую частоту в допустимой погрешности.

### 15.3. Wake detector

Через wake model:

- существующий явный путь имеет высший приоритет;
- `hey_jarvis` разрешается через встроенный реестр;
- `hey_findus` находит `hey_findus.onnx` в `WAKE_WORD_MODEL_DIR`;
- `jarvis` не совпадает с `hey_jarvis.onnx`;
- ONNX-режим не подхватывает `.tflite` и наоборот;
- одинаковый путь из registry-фазы и glob-фазы не дублируется;
- при отсутствии локального файла встроенная модель находится в package
  resources;
- неизвестное имя дает диагностический `FileNotFoundError`;
- score ниже threshold не активирует;
- score на threshold активирует;
- `patience=2` требует два последовательных кадра;
- отрицательный кадр сбрасывает patience;
- cooldown подавляет повторное срабатывание;
- gain не вызывает int16 overflow;
- `reset()` очищает transient state.

Отдельный integration test с локальной моделью можно пометить как optional,
чтобы обычный unit suite не скачивал веса.

### 15.4. Activation gate

- непустой model создает startup state `SLEEPING`;
- пустой model создает startup state `ACTIVE`;
- только `SLEEPING` может перейти в `EARCON`;
- неверный `activation_id` не завершает earcon;
- только `ACTIVE` разрешает передачу;
- timeout запускает `DEACTIVATING`, а playback barrier завершает `SLEEPING`;
- новый `speech_started` переводит `FOLLOW_UP` обратно в `ACTIVE`;
- callback старой revision не закрывает новую interaction;
- max-active timeout закрывает зависший цикл;
- `STOPPED` является терминальным состоянием.

Для времени использовать injected monotonic clock, а не реальные `sleep()`.

### 15.5. Playback

- PCM items проигрываются в порядке добавления;
- barrier выполняется после предыдущего PCM;
- earcon barrier открывает gate;
- deactivation barrier включает detector только после нисходящего cue;
- response barrier закрывает только актуальный activation cycle;
- ошибка output stream не открывает gate;
- `stop()` корректно завершает очередь и thread.

В unit tests использовать fake output stream.

### 15.6. Realtime client

С fake connection проверить:

1. В `SLEEPING` за несколько mic blocks нет ни одного
   `input_audio_buffer.append`.
2. Trigger block не отправляется.
3. В `EARCON` блоки не отправляются.
4. До выполнения playback barrier блоки не отправляются.
5. Первый блок после barrier отправляется.
6. В `ACTIVE` сохраняется непрерывная отправка во время response audio.
7. Промежуточный function-call response не закрывает gate.
8. После всех tool outputs отправляется ровно один `response.create`.
9. Финальный `response.done` ставит playback barrier.
10. Barge-in инвалидирует completion старого ответа.
11. После финального playback и follow-up timeout отправка прекращается.
12. Следующий wake запускает новый `activation_id`.
13. При пустом `WAKE_WORD_MODEL` сохраняется always-on поведение.

### 15.7. Интеграционный тест без микрофона

Подать заранее подготовленную последовательность PCM:

```text
тишина → wake word → пауза → команда
```

И проверить:

- тишина и wake word не появились в fake WebSocket;
- earcon был поставлен в playback;
- команда начала передаваться только после completion barrier;
- порядок отправленных PCM совпал с исходными post-earcon блоками.

Тестовые аудиофайлы должны быть короткими и не содержать чувствительных данных.

## 16. Ручная проверка

### 16.1. Базовая активация

- 5 минут фонового звука не создают server turns;
- правильный wake word проигрывает один earcon;
- команда после earcon полностью распознается;
- wake word отсутствует в transcript;
- внутри follow-up окна продолжение принимается без нового wake word;
- после истечения follow-up окна снова требуется wake word.

### 16.2. Earcon boundary

Проверить варианты:

- команда сразу после сигнала;
- команда через 500 мс;
- команда через 5 секунд;
- отсутствие команды после сигнала;
- повторный wake word во время earcon.

Ожидание:

- первые фонемы команды не обрезаются;
- earcon не попадает в STT;
- повторный wake во время `EARCON` игнорируется;
- отсутствие команды возвращает gate в sleeping.

### 16.3. AEC и barge-in regression

- перебить длинный ответ в начале, середине и конце;
- убедиться, что старый completion callback не деактивирует новый turn;
- убедиться, что клиент продолжает отправлять mic blocks во время TTS;
- проверить, что речь самого ассистента не создает новый turn;
- проверить несколько последовательных перебиваний в одной активации.

### 16.4. Отказоустойчивость

- отсутствует wake model;
- input device исчез во время работы;
- output device не открывается;
- WebSocket разрывается в `ACTIVE`;
- server response завис;
- playback thread завершился с ошибкой;
- Ctrl-C в каждом состоянии.

Ни один из этих случаев не должен незаметно включать постоянную отправку
микрофона.

## 17. Этапы реализации

### Этап 1. Raw capture без изменения поведения

1. Добавить `capture_pcm_blocks()`.
2. Оставить `encode_block()` отдельной функцией.
3. Перевести mic pump на raw blocks.
4. Пока всегда кодировать и отправлять каждый блок.
5. Обновить capture tests.

Результат: поведение не меняется, но появляется точка для локальной обработки.

### Этап 2. Playback PCM и barriers

1. Типизировать playback queue.
2. Добавить `put_pcm()`.
3. Добавить `put_barrier()`.
4. Добавить генерацию earcon.
5. Покрыть порядок queue items тестами.

Результат: клиент умеет надежно узнать, когда earcon фактически прошел через
output stream.

### Этап 3. Конфигурация и модели

1. Добавить `WakeWordSettings`.
2. Добавить зависимости.
3. Добавить model directory.
4. Реализовать prepare-команду.
5. Добавить fail-closed startup validation.

### Этап 4. Resampler и detector

1. Реализовать stateful 24 → 16 кГц resampler.
2. Реализовать frame accumulator.
3. Реализовать adapter openWakeWord.
4. Добавить fake detector для unit tests.
5. Проверить detector отдельно от Realtime-клиента.

### Этап 5. Activation gate

1. Реализовать потокобезопасную state machine.
2. Подключить detector в mic pump.
3. Добавить переход `SLEEPING → EARCON`.
4. Связать earcon barrier с переходом `EARCON → ACTIVE`.
5. Запретить отправку во всех состояниях, кроме `ACTIVE`.

На этом этапе уже выполняется основное требование: WebSocket получает блоки
только после earcon.

### Этап 6. Завершение activation cycle

1. Различать промежуточный tool response и финальный response.
2. Ставить completion barrier после финального ответа.
3. Реализовать revision guard для barge-in.
4. Добавить follow-up окно и защитные timeout.
5. Сбрасывать detector/resampler при возврате в sleeping.

### Этап 7. Наблюдаемость и tuning

1. Добавить activation events и latency fields.
2. Настроить threshold/gain/patience на реальном микрофоне.
3. Проверить command clipping.
4. Проверить AEC/barge-in regression.
5. Зафиксировать рекомендуемые значения в `.env.example` и README.

### Этап 8. Follow-up окно — вторая итерация

После стабилизации one-wake режима добавить и включить рекомендуемое окно:

```env
FOLLOW_UP_WINDOW_SEC=10
```

При значении больше нуля после финального playback gate остается `ACTIVE` на
указанное время. Новая речь пользователя продлевает окно. Нулевое значение
сохраняет базовое поведение первой итерации: «новый wake word для следующего
цикла».

## 18. Риски и меры

| Риск                                       | Мера                                                                                            |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------- |
| Обрезается начало команды                  | earcon, post-earcon silence, блок 80 мс                                                         |
| Wake word попадает в STT                   | trigger block и все `EARCON` blocks отбрасываются                                               |
| Earcon попадает в STT                      | отправка разрешается только после playback barrier                                              |
| Ассистент активирует сам себя              | detector выключен в `ACTIVE`, AEC остается включенным                                           |
| Старый ответ закрывает новый barge-in turn | `activation_id` и `interaction_revision`                                                        |
| Function call ошибочно завершает цикл      | промежуточный `response.done` не rearm-ит gate                                                  |
| Playback error открывает микрофон          | fail-closed transition в `SLEEPING`                                                             |
| Wake dependency отсутствует                | явная startup error, без always-on fallback                                                     |
| Resampling дает drift                      | stateful resampler и frame accumulator                                                          |
| Mic thread блокируется earcon              | earcon выполняется playback thread, mic продолжает читать                                       |
| Сервер хранит незавершенный input buffer   | timeout и проверка фактических server events; `clear` добавлять только после проверки поддержки |
| Слишком много WebSocket chunks             | сначала 80 мс; при необходимости объединять только wire blocks                                  |

## 19. Критерии готовности

Функция считается готовой, когда выполняются все условия:

- при непустом `WAKE_WORD_MODEL` до wake word отправлено ровно 0 аудиоблоков;
- trigger block не отправляется;
- во время earcon отправлено ровно 0 аудиоблоков;
- первый блок отправляется только после completion barrier earcon;
- wake word и earcon не появляются в распознанной команде;
- начало команды после earcon не обрезается в серии ручных тестов;
- во время активного ответа микрофон продолжает передаваться;
- существующий barge-in проходит regression test;
- tool calls не закрывают активацию до финального ответа;
- после завершения follow-up окна клиент возвращается в sleeping;
- нисходящий cue однозначно сообщает о закрытии окна;
- во время deactivation cue PCM не отправляется, а detector еще выключен;
- ответ на уточняющий вопрос внутри follow-up окна не требует нового wake word;
- второй wake word после закрытия окна запускает новый независимый цикл;
- ошибка модели, resampler или playback не включает always-on режим;
- пустой `WAKE_WORD_MODEL` сохраняет прежнее поведение;
- unit tests, lint и strict type checking проходят;
- пакет `speech-to-speech` и его исходный код не изменены.

## 20. Итоговая реализация

Первая итерация обеспечивает основной privacy boundary:

```text
raw mic capture
    ↓
24 → 16 kHz resampler
    ↓
local openWakeWord detector
    ↓
earcon через AudioPlayer
    ↓ completion barrier
ACTIVE gate
    ↓
существующий Realtime pipeline с AEC и barge-in
    ↓
final playback barrier
    ↓
SLEEPING
```

Не следует одновременно добавлять follow-up UX, новые tools или изменения
server protocol. Сначала нужно надежно обеспечить главную гарантию: ни один
микрофонный блок не попадает в Realtime-сессию до завершения earcon.

После фиксации этой границы вторая итерация добавляет:

```text
final playback barrier
    ↓
FOLLOW_UP (10 секунд по умолчанию)
    ├── новая речь ──▶ ACTIVE ──▶ новый ответ ──▶ FOLLOW_UP
    └── timeout ──▶ DEACTIVATING ──нисходящий cue──▶ SLEEPING
```

Она не меняет пакет `speech-to-speech` и использует тот же непрерывный
AEC/barge-in поток, который уже открыт в активном диалоговом цикле.
