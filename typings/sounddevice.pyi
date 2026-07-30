"""Minimal type stubs for the bits of ``sounddevice`` we use.

We only touch ``InputStream``, ``OutputStream``, ``query_devices`` and the
``read``/``write`` methods, so the stubs cover just those. This satisfies
pyright strict mode (the real package ships no py.typed / stubs).
"""

from collections.abc import Callable
from typing import IO, Any

import numpy as np
from numpy import dtype, generic, ndarray

# sounddevice accepts any numpy scalar/array; model it loosely to stay strict-safe.
_ArrayLike = ndarray[Any, dtype[Any]]

_AudioCallback = Callable[..., None]


class InputStream:
    def __init__(self, *args: Any, **kwargs: Any) -> None: ...
    def __enter__(self) -> "InputStream": ...
    def __exit__(self, *exc: Any) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...
    def read(
        self, frames: int
    ) -> tuple[ndarray[tuple[int, int], dtype[np.int16]], bool]: ...


class OutputStream:
    def __init__(self, *args: Any, **kwargs: Any) -> None: ...
    def __enter__(self) -> "OutputStream": ...
    def __exit__(self, *exc: Any) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...
    def write(
        self, data: ndarray[tuple[int, int] | tuple[int], dtype[np.int16]]
    ) -> bool: ...


def query_devices(
    device: int | str | None = None, kind: str | None = None
) -> Any: ...


def play(
    data: _ArrayLike,
    samplerate: float | None = None,
    mapping: list[int] | None = None,
    blocking: bool = False,
    loop: bool = False,
) -> None: ...


def wait(self: Any) -> None: ...


def rec(
    frames: int | None = None,
    samplerate: float | None = None,
    channels: int | None = None,
    dtype: Any = None,
    out: _ArrayLike | None = None,
    mapping: list[int] | None = None,
    blocking: bool = False,
) -> _ArrayLike: ...
