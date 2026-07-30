"""Minimal soxr declarations used by the streaming wake-word resampler."""

from typing import Any

import numpy as np


class ResampleStream:
    def __init__(
        self,
        in_rate: float,
        out_rate: float,
        num_channels: int,
        *,
        dtype: str = ...,
        quality: str = ...,
    ) -> None: ...

    def resample_chunk(
        self,
        x: np.ndarray[Any, Any],
        *,
        last: bool = ...,
    ) -> np.ndarray[Any, Any]: ...
