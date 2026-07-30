"""Realtime voice client with local wake-word and follow-up gating."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, cast

import httpx
from openai import OpenAI
from openai.resources.beta.realtime.realtime import RealtimeConnection
from openai.types.beta.realtime import RealtimeClientEventParam
from websockets.exceptions import ConnectionClosed

from activation import ActivationGate, ActivationSnapshot, ActivationState
from audio.capture import PcmBlock, capture_pcm_blocks, encode_block
from audio.playback import AudioOutput, make_chime
from audio.resample import AudioResampler, WakeFrameBuffer
from audio.wakeword import WakeDetection, WakeDetector
from config import Settings
from tools import registry
from tools.scheduling import ScheduledEvent, Scheduler, fire_message

log = logging.getLogger("voice-agent.realtime")

_SYSTEM_PROMPT_RU = (
    "Ты голосовой ассистент. Отвечай ОЧЕНЬ коротко — максимум одно-два "
    "предложения, как в живом диалоге. Никакого Markdown, списков и разметки, "
    "никаких длинных пояснений. Если нужен точный ответ (погода, вычисления) — "
    "вызывай соответствующий инструмент, а потом озвучь результат одной фразой "
    "простыми словами на языке пользователя. "
    "Напоминания, будильники и таймеры выполняй ТОЛЬКО через инструменты "
    "set_reminder, set_alarm, set_timer — сам ты ничего не запоминаешь, и без "
    "вызова инструмента напоминание не поставится и не прозвенит. Не отвечай "
    "«напомню/засёк» словами без вызова инструмента. Сначала вызови инструмент, "
    "затем коротко подтверди время из его ответа. Время всегда в 24-часовом "
    "формате."
)
_SYSTEM_PROMPT_EN = (
    "You are a voice assistant. Reply VERY briefly — at most one or two "
    "sentences, like a live conversation. No Markdown, lists or markup, no long "
    "explanations. When a precise answer is needed (weather, calculations), call "
    "the matching tool and then speak the result in a single plain sentence in "
    "the user's language. "
    "Reminders, alarms and timers MUST be done only via the set_reminder, "
    "set_alarm, set_timer tools — you remember nothing yourself, and without "
    "calling the tool nothing is scheduled and nothing will ring. Never reply "
    "'I'll remind you / starting a timer' in words without calling the tool. "
    "Call the tool first, then briefly confirm the time taken from its reply. "
    "Times are always in 24-hour format."
)


def _always_on_gate() -> ActivationGate:
    return ActivationGate(
        requires_wake=False,
        listen_timeout_sec=8.0,
        follow_up_window_sec=0.0,
        max_active_sec=90.0,
    )


@dataclass
class ToolDeps:
    language: str
    geocoding_url: str
    forecast_url: str
    http_client: httpx.Client
    scheduler: Scheduler


@dataclass
class WakeWordRuntime:
    """Audio components used only while a wake model is configured."""

    detector: WakeDetector
    resampler: AudioResampler
    frames: WakeFrameBuffer
    earcon: PcmBlock
    deactivation_earcon: PcmBlock

    def reset(self) -> None:
        self.detector.reset()
        self.resampler.reset()
        self.frames.reset()


@dataclass
class _PendingCall:
    call_id: str
    name: str
    arguments: str = ""


_EventHandler = Callable[["RealtimeClient", RealtimeConnection, Any], None]


@dataclass
class RealtimeClient:
    """Own one Realtime session and its client-side activation lifecycle."""

    settings: Settings
    deps: ToolDeps
    player: AudioOutput
    activation_gate: ActivationGate = field(default_factory=_always_on_gate)
    wake_runtime: WakeWordRuntime | None = None
    _stop: threading.Event = field(init=False)
    _pending: dict[str, _PendingCall] = field(init=False)
    _conn: RealtimeConnection | None = field(init=False, default=None)
    _mic_thread: threading.Thread | None = field(init=False, default=None)
    _last_mic_state: ActivationState = field(init=False)

    def __post_init__(self) -> None:
        self._stop = threading.Event()
        self._pending = {}
        self._last_mic_state = self.activation_gate.state
        if self.activation_gate.requires_wake and self.wake_runtime is None:
            raise ValueError("wake_runtime is required when WAKE_WORD_MODEL is set")

    def run(self) -> None:
        client = OpenAI(
            api_key=self.settings.llm_api_key or "unused",
            base_url=self._http_base_url(),
            websocket_base_url=self._websocket_base_url(),
        )
        log.info("connecting to %s", self.settings.s2s_url)
        with client.beta.realtime.connect(model=self.settings.llm_model) as conn:
            self._conn = conn
            self._configure_session(conn)
            self.player.start()
            self._start_mic(conn)
            self.deps.scheduler.on_fire = self._fire_scheduled
            self.deps.scheduler.start()
            try:
                self._recv_loop(conn)
            finally:
                self.deps.scheduler.stop()
                self.activation_gate.stop()
                self.player.stop()
                self._stop_mic()
                self._conn = None

    def stop(self) -> None:
        self._stop.set()
        self.activation_gate.stop()

    def _configure_session(self, conn: RealtimeConnection) -> None:
        instructions = _SYSTEM_PROMPT_RU if self.deps.language == "ru" else _SYSTEM_PROMPT_EN
        tools = registry.realtime_tools(self.deps.language)
        session: dict[str, Any] = {
            "type": "realtime",
            "instructions": instructions,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "turn_detection": None,  # speech-to-speech owns server-side VAD.
            "tools": tools,
            "tool_choice": "auto",
        }
        self._send(conn, {"type": "session.update", "session": session})
        log.info(
            "session configured with %d tools; wake=%s",
            len(tools),
            self.activation_gate.requires_wake,
        )

    # ── microphone / wake gate ─────────────────────────────────────────────

    def _start_mic(self, conn: RealtimeConnection) -> None:
        def _pump() -> None:
            try:
                for block in capture_pcm_blocks(
                    sample_rate=self.settings.sample_rate,
                    block_size=self.settings.block_size,
                    channels=self.settings.channels,
                    device=self.settings.input_device,
                ):
                    if self._stop.is_set():
                        break
                    self._process_mic_block(conn, block)
            except Exception:  # pragma: no cover - hardware-dependent
                log.exception("mic thread failed")

        self._mic_thread = threading.Thread(target=_pump, name="mic", daemon=True)
        self._mic_thread.start()

    def _process_mic_block(self, conn: RealtimeConnection, block: PcmBlock) -> None:
        reason = self.activation_gate.check_timeouts()
        if reason is not None:
            log.info("wake deactivated: %s", reason)
            self._queue_deactivation_earcon()

        state = self.activation_gate.state
        if (
            state is ActivationState.SLEEPING
            and self._last_mic_state is not ActivationState.SLEEPING
            and self.wake_runtime is not None
        ):
            self.wake_runtime.reset()
        self._last_mic_state = state

        if state is ActivationState.SLEEPING:
            if self.wake_runtime is not None:
                wake_audio = self.wake_runtime.resampler.process(block)
                for frame in self.wake_runtime.frames.push(wake_audio):
                    detection = self.wake_runtime.detector.process(frame)
                    if detection.detected:
                        self._on_wake_detected(detection)
                        break
            return

        if state is ActivationState.EARCON:
            return

        if state is ActivationState.DEACTIVATING:
            return

        if self.activation_gate.should_forward_audio():
            self._send(
                conn,
                {
                    "type": "input_audio_buffer.append",
                    "audio": encode_block(block),
                },
            )

    def _on_wake_detected(self, detection: WakeDetection) -> None:
        runtime = self.wake_runtime
        if runtime is None:
            return
        activation_id = self.activation_gate.begin_earcon()
        if activation_id is None:
            return
        log.info(
            "wake detected: model=%s score=%.3f activation_id=%d",
            detection.model_name,
            detection.score,
            activation_id,
        )
        self.player.put_pcm(runtime.earcon)
        self.player.put_barrier(lambda: self._finish_earcon(activation_id))

    def _finish_earcon(self, activation_id: int) -> None:
        if self.activation_gate.finish_earcon(activation_id):
            log.info("wake active after earcon: activation_id=%d", activation_id)

    def _queue_deactivation_earcon(self) -> None:
        runtime = self.wake_runtime
        if runtime is None or self.activation_gate.state is not ActivationState.DEACTIVATING:
            return
        activation_id = self.activation_gate.snapshot().activation_id
        self.player.put_pcm(runtime.deactivation_earcon)
        self.player.put_barrier(lambda: self._finish_deactivation(activation_id))

    def _finish_deactivation(self, activation_id: int) -> None:
        if self.activation_gate.finish_deactivation(activation_id):
            log.info(
                "wake sleeping after deactivation cue: activation_id=%d",
                activation_id,
            )

    def _stop_mic(self) -> None:
        if self._mic_thread is not None:
            self._mic_thread.join(timeout=1.0)
            self._mic_thread = None

    # ── receive loop / server lifecycle ────────────────────────────────────

    def _recv_loop(self, conn: RealtimeConnection) -> None:
        while not self._stop.is_set():
            try:
                event = conn.recv()
            except ConnectionClosed as exc:
                # Normal lifecycle: the start script kills the s2s server on
                # Ctrl+C, which closes this socket. Not an error.
                log.info("connection closed (%s); exiting event loop", exc)
                return
            except Exception:
                if self._stop.is_set():
                    log.info("recv interrupted during shutdown; exiting event loop")
                else:
                    log.exception("recv failed; exiting event loop")
                return
            self._handle(conn, event)

    def _handle(self, conn: RealtimeConnection, event: Any) -> None:
        handler = _HANDLERS.get(event.type)
        if handler is not None:
            handler(self, conn, event)

    def _on_speech_started(self, conn: RealtimeConnection, event: Any) -> None:
        del conn, event
        self.activation_gate.note_user_speech()

    def _on_speech_stopped(self, conn: RealtimeConnection, event: Any) -> None:
        del conn, event
        self.activation_gate.note_user_speech()

    def _on_response_created(self, conn: RealtimeConnection, event: Any) -> None:
        del conn, event
        self.activation_gate.note_response_started()

    def _on_audio_delta(self, conn: RealtimeConnection, event: Any) -> None:
        del conn
        self.activation_gate.note_response_activity()
        self.player.put_delta(event.delta)

    def _on_output_item_added(self, conn: RealtimeConnection, event: Any) -> None:
        del conn
        item = event.item
        if getattr(item, "type", None) == "function_call":
            self._pending[item.call_id] = _PendingCall(call_id=item.call_id, name=item.name)

    def _on_fn_args_delta(self, conn: RealtimeConnection, event: Any) -> None:
        del conn
        call = self._pending.get(event.call_id)
        if call is not None:
            call.arguments += event.delta

    def _on_fn_args_done(self, conn: RealtimeConnection, event: Any) -> None:
        del conn
        call = self._pending.get(event.call_id)
        if call is None:
            call = _PendingCall(
                call_id=event.call_id,
                name=getattr(event, "name", "") or "",
                arguments=event.arguments,
            )
            self._pending[event.call_id] = call
        else:
            call.arguments = event.arguments or call.arguments
            if not call.name and getattr(event, "name", ""):
                call.name = event.name

    def _on_response_done(self, conn: RealtimeConnection, event: Any) -> None:
        calls = list(self._pending.values())
        self._pending.clear()
        if calls:
            for call in calls:
                self._send_tool_output(conn, call)
            # All outputs belong to one continuation response.
            self._send(conn, {"type": "response.create"})
            return

        if _response_status(event) in {"cancelled", "failed", "incomplete"}:
            return
        self.activation_gate.note_response_activity()
        if not self.activation_gate.requires_wake:
            return
        snapshot = self.activation_gate.snapshot()
        self.player.put_barrier(lambda: self._finish_response_playback(snapshot))

    def _finish_response_playback(self, snapshot: ActivationSnapshot) -> None:
        if self.activation_gate.finish_response_if_current(snapshot):
            log.info(
                "response playback drained; state=%s",
                self.activation_gate.state.value,
            )
            self._queue_deactivation_earcon()

    # ── tools ──────────────────────────────────────────────────────────────

    def _send_tool_output(self, conn: RealtimeConnection, call: _PendingCall) -> None:
        log.info("tool call %s args=%s", call.name, call.arguments)
        result = registry.dispatch(call.name, call.arguments, self.deps)
        log.info("tool %s -> %s", call.name, result)
        self._send(
            conn,
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": result,
                },
            },
        )

    # Kept as a compatibility helper for focused tests/callers.
    def _execute_tool(self, conn: RealtimeConnection, call: _PendingCall) -> None:
        self._send_tool_output(conn, call)
        self._send(conn, {"type": "response.create"})

    # ── scheduled-event firing (timer / alarm / reminder) ──────────────────

    def _fire_scheduled(self, event: ScheduledEvent) -> None:
        """Play an attention chime, then inject the event text for TTS.

        Runs on the scheduler thread; it only enqueues into the (thread-safe)
        player queue. The barrier sends the conversation item after the chime
        finishes, so the spoken reminder never overlaps the cue.
        """
        chime = make_chime(event.kind, sample_rate=self.settings.output_sample_rate)
        self.player.put_pcm(chime)
        self.player.put_barrier(lambda: self._send_scheduled(event))

    def _send_scheduled(self, event: ScheduledEvent) -> None:
        """Inject the fired event as a user message and request a response."""
        conn = self._conn
        if conn is None:
            return
        text = fire_message(event, self.deps.language, self.deps.scheduler.tz)
        log.info("scheduled fire %s -> %s", event.kind, text)
        self._send(
            conn,
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            },
        )
        self._send(conn, {"type": "response.create"})

    # ── protocol helpers ───────────────────────────────────────────────────

    def _send(self, conn: RealtimeConnection, event: dict[str, Any]) -> None:
        conn.send(cast(RealtimeClientEventParam, event))

    def _http_base_url(self) -> str:
        ws = self.settings.s2s_url
        for scheme in ("ws://", "wss://"):
            if ws.startswith(scheme):
                return "http" + ws[len("ws") :]
        return ws

    def _websocket_base_url(self) -> str:
        ws = self.settings.s2s_url
        if ws.endswith("/realtime"):
            ws = ws[: -len("/realtime")]
        return ws


def _response_status(event: Any) -> str | None:
    response = cast(object | None, getattr(event, "response", None))
    value: object | None
    if isinstance(response, dict):
        value = cast(dict[str, object], response).get("status")
    else:
        value = cast(object | None, getattr(response, "status", None))
    return str(value) if value is not None else None


_HANDLERS: dict[str, _EventHandler] = {
    "input_audio_buffer.speech_started": RealtimeClient._on_speech_started,  # type: ignore[dict-item]
    "input_audio_buffer.speech_stopped": RealtimeClient._on_speech_stopped,  # type: ignore[dict-item]
    "response.created": RealtimeClient._on_response_created,  # type: ignore[dict-item]
    "response.output_audio.delta": RealtimeClient._on_audio_delta,  # type: ignore[dict-item]
    "response.output_item.added": RealtimeClient._on_output_item_added,  # type: ignore[dict-item]
    "response.function_call_arguments.delta": RealtimeClient._on_fn_args_delta,  # type: ignore[dict-item]
    "response.function_call_arguments.done": RealtimeClient._on_fn_args_done,  # type: ignore[dict-item]
    "response.done": RealtimeClient._on_response_done,  # type: ignore[dict-item]
}


__all__ = ["RealtimeClient", "ToolDeps", "WakeWordRuntime"]
