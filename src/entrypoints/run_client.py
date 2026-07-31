"""``run-client`` entrypoint — run the realtime assistant against s2s.

Blocks until interrupted (Ctrl-C). Make sure the speech-to-speech server is
running first (see README).
"""

from __future__ import annotations

import logging
import signal
import sys

from app import build_client
from config import Settings


def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not settings.llm_api_key:
        # The OpenAI client needs *some* key to construct, even if the s2s
        # server ignores it. Warn loudly so a missing key isn't silent.
        logging.warning(
            "OPENAI_API_KEY is empty — the s2s server's LLM call may fail."
        )

    client = build_client(settings)

    # Graceful shutdown on SIGINT / SIGTERM.
    def _shutdown(*_: object) -> None:
        logging.info("stopping…")
        client.stop()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    client.run()
    sys.exit(0)


__all__ = ["main"]
