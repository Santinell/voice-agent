"""Realtime client — the brain of the assistant.

Connects to a speech-to-speech Realtime server, configures the session with
our client-side tools, streams mic audio in, plays response audio out, and
intercepts the model's ``function_call``s to run our tools and feed the
results back as ``function_call_output``.

Concurrency model (all threads; the openai realtime client is synchronous):
  * main thread      — ``recv()`` event loop (this module)
  * mic thread       — reads the microphone and sends input_audio_buffer.append
  * playback thread  — owned by :class:`AudioPlayer`
"""

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

from ..audio.capture import capture_blocks
from ..audio.playback import AudioPlayer
from ..config import Settings
from ..tools import registry

log = logging.getLogger("s2s_assistant.realtime")

# System prompt — sets the assistant's persona and instructs it to use tools.
# The hard length cap (1-2 sentences) is deliberate: this is a *voice*
# assistant, and long answers mean seconds of silence before the first word
# (observed: ~2000-token replies → 25s+ TTFT). Keep it spoken-length.
_SYSTEM_PROMPT_RU = (
    "Ты голосовой ассистент. Отвечай ОЧЕНЬ коротко — максимум одно-два "
    "предложения, как в живом диалоге. Никакого Markdown, списков и разметки, "
    "никаких длинных пояснений. Если нужен точный ответ (погода, вычисления) — "
    "вызывай соответствующий инструмент, а потом озвучь результат одной фразой "
    "простыми словами на языке пользователя."
)
_SYSTEM_PROMPT_EN = (
    "You are a voice assistant. Reply VERY briefly — at most one or two "
    "sentences, like a live conversation. No Markdown, lists or markup, no long "
    "explanations. When a precise answer is needed (weather, calculations), call "
    "the matching tool and then speak the result in a single plain sentence in "
    "the user's language."
)


@dataclass
class ToolDeps:
    """Concrete :class:`registry.ToolDeps` built from settings."""

    language: str
    geocoding_url: str
    forecast_url: str
    http_client: httpx.Client


@dataclass
class _PendingCall:
    """A function call whose arguments are still streaming in."""

    call_id: str
    name: str
    arguments: str = ""


# Handler signature: (client, connection, server_event) -> None.
_EventHandler = Callable[["RealtimeClient", RealtimeConnection, Any], None]


@dataclass
class RealtimeClient:
    """Owns one realtime session against the s2s server.

    Lifecycle: ``run()`` connects, configures the session, then blocks on the
    recv loop until stopped (``stop()``) or the connection drops.
    """

    settings: Settings
    deps: ToolDeps
    player: AudioPlayer
    # Per-instance mutable state (must NOT be class attributes — see __post_init__).
    _stop: threading.Event = field(init=False)
    _pending: dict[str, _PendingCall] = field(init=False)
    _conn: RealtimeConnection | None = field(init=False, default=None)
    _mic_thread: threading.Thread | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        # threading.Event / dict are mutable — create per instance, not shared.
        self._stop = threading.Event()
        self._pending = {}

    # ── public lifecycle ────────────────────────────────────────────────────

    def run(self) -> None:
        """Connect, configure, and pump events until stopped."""
        client = OpenAI(
            api_key=self.settings.llm_api_key or "unused",
            base_url=self._http_base_url(),
            # The SDK forces wss:// (TLS) unless websocket_base_url is set. Our
            # s2s server is plain ws, so pin it explicitly. SDK also appends
            # "/realtime" to the path, hence we strip the trailing segment.
            websocket_base_url=self._websocket_base_url(),
        )
        log.info("connecting to %s", self.settings.s2s_url)
        with client.beta.realtime.connect(model=self.settings.llm_model) as conn:
            self._conn = conn
            self._configure_session(conn)
            self.player.start()
            self._start_mic(conn)
            try:
                self._recv_loop(conn)
            finally:
                self.player.stop()
                self._stop_mic()
                self._conn = None

    def stop(self) -> None:
        """Signal the run loop to exit."""
        self._stop.set()

    # ── session setup ───────────────────────────────────────────────────────

    def _configure_session(self, conn: RealtimeConnection) -> None:
        """Send ``session.update`` with voice + our tools."""
        instructions = (
            _SYSTEM_PROMPT_RU if self.deps.language == "ru" else _SYSTEM_PROMPT_EN
        )
        tools = registry.realtime_tools(self.deps.language)
        session: dict[str, Any] = {
            # `type` is required by the Realtime session schema (discriminated
            # union). Without it s2s rejects the whole session.update, so tools
            # and the system prompt never reach the LLM — the model then can't
            # call get_weather etc. ("Tools: []" in the server log).
            "type": "realtime",
            "instructions": instructions,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "turn_detection": None,  # s2s server-side VAD drives turns
            "tools": tools,
            "tool_choice": "auto",
        }
        self._send(conn, {"type": "session.update", "session": session})
        log.info("session configured with %d tools", len(tools))

    # ── microphone streaming ────────────────────────────────────────────────

    def _start_mic(self, conn: RealtimeConnection) -> None:
        def _pump() -> None:
            try:
                for block in capture_blocks(
                    sample_rate=self.settings.sample_rate,
                    block_size=self.settings.block_size,
                    channels=self.settings.channels,
                    device=self.settings.input_device,
                ):
                    if self._stop.is_set():
                        break
                    self._send(
                        conn, {"type": "input_audio_buffer.append", "audio": block}
                    )
            except Exception:  # pragma: no cover - hardware-dependent
                log.exception("mic thread failed")

        self._mic_thread = threading.Thread(target=_pump, name="mic", daemon=True)
        self._mic_thread.start()

    def _stop_mic(self) -> None:
        if self._mic_thread is not None:
            self._mic_thread.join(timeout=1.0)
            self._mic_thread = None

    # ── event loop ──────────────────────────────────────────────────────────

    def _recv_loop(self, conn: RealtimeConnection) -> None:
        """Dispatch server events until stopped or the stream ends."""
        while not self._stop.is_set():
            try:
                event = conn.recv()
            except Exception:
                log.exception("recv failed; exiting event loop")
                return
            self._handle(conn, event)

    def _handle(self, conn: RealtimeConnection, event: Any) -> None:
        """Route one server event to its handler by ``type``."""
        handler = _HANDLERS.get(event.type)
        if handler is not None:
            handler(self, conn, event)
        # Unhandled events are intentionally ignored — the protocol is chatty.

    # ── individual handlers ─────────────────────────────────────────────────

    def _on_audio_delta(self, conn: RealtimeConnection, event: Any) -> None:
        # Streamed response audio → speaker.
        self.player.put_delta(event.delta)

    def _on_output_item_added(self, conn: RealtimeConnection, event: Any) -> None:
        # The start of a new response item — register a function call if any.
        item = event.item
        if getattr(item, "type", None) == "function_call":
            self._pending[item.call_id] = _PendingCall(
                call_id=item.call_id, name=item.name
            )

    def _on_fn_args_delta(self, conn: RealtimeConnection, event: Any) -> None:
        # Incremental argument text for a streaming function call.
        call = self._pending.get(event.call_id)
        if call is not None:
            call.arguments += event.delta

    def _on_fn_args_done(self, conn: RealtimeConnection, event: Any) -> None:
        # Final (possibly complete) arguments — finalize and dispatch later.
        call = self._pending.get(event.call_id)
        if call is None:
            # s2s emits only response.function_call_arguments.done for a function
            # call (no response.output_item.added with the tool name), so the
            # name is read from this event. Falling back to "" would yield
            # "Неизвестный инструмент" — exactly the bug this guards against.
            call = _PendingCall(
                call_id=event.call_id,
                name=getattr(event, "name", "") or "",
                arguments=event.arguments,
            )
            self._pending[event.call_id] = call
        else:
            call.arguments = event.arguments or call.arguments
            # The name may have been missing if output_item.added wasn't sent.
            if not call.name and getattr(event, "name", ""):
                call.name = event.name

    def _on_response_done(self, conn: RealtimeConnection, event: Any) -> None:
        # End of a response turn: run any completed function calls in order
        # and let the model continue with their outputs.
        for call in list(self._pending.values()):
            self._execute_tool(conn, call)
        self._pending.clear()

    # ── tool execution ─────────────────────────────────────────────────────

    def _execute_tool(self, conn: RealtimeConnection, call: _PendingCall) -> None:
        """Dispatch one tool call, send its output, then request a response."""
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
        # Ask the model to turn the tool output into a spoken reply.
        self._send(conn, {"type": "response.create"})

    # ── helpers ─────────────────────────────────────────────────────────────

    def _send(self, conn: RealtimeConnection, event: dict[str, Any]) -> None:
        """Send a client event, casting it to the SDK's typed param union.

        We build events as plain dicts for readability; the SDK validates the
        shape server-side. The cast documents intent and keeps pyright happy.
        """
        conn.send(cast(RealtimeClientEventParam, event))

    def _http_base_url(self) -> str:
        """The HTTP base URL for the OpenAI client (derived from ws url).

        ``connect()`` upgrades the model name to the ws endpoint on its own;
        the OpenAI client still needs an HTTP ``base_url`` to bootstrap.
        """
        ws = self.settings.s2s_url
        for scheme in ("ws://", "wss://"):
            if ws.startswith(scheme):
                return "http" + ws[len("ws") :]
        return ws

    def _websocket_base_url(self) -> str:
        """The websocket base URL for the OpenAI realtime client.

        Without this, the SDK forces scheme ``wss://`` (TLS), which fails an
        SSL handshake against our plain-``ws`` s2s server. We also strip the
        trailing ``/realtime`` segment because ``connect()`` re-appends it.
        """
        ws = self.settings.s2s_url
        if ws.endswith("/realtime"):
            ws = ws[: -len("/realtime")]
        return ws


# ── event-type → handler dispatch table ─────────────────────────────────────
#
# Kept as a module-level table (not a big if/elif) so the routing is visible
# at a glance and each handler stays small. Handlers take (self, conn, event).
_HANDLERS: dict[str, _EventHandler] = {
    "response.output_audio.delta": RealtimeClient._on_audio_delta,  # type: ignore[dict-item]
    "response.output_item.added": RealtimeClient._on_output_item_added,  # type: ignore[dict-item]
    "response.function_call_arguments.delta": RealtimeClient._on_fn_args_delta,  # type: ignore[dict-item]
    "response.function_call_arguments.done": RealtimeClient._on_fn_args_done,  # type: ignore[dict-item]
    "response.done": RealtimeClient._on_response_done,  # type: ignore[dict-item]
}


__all__ = ["RealtimeClient", "ToolDeps"]
