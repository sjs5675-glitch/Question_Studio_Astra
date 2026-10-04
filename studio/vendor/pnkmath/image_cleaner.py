"""Adapted from PNKmath/ngd-studio image_cleaner.py at cafa7e70.

The original file is preserved in upstream/. No-clean and error paths never
copy the source to a cleaned output in this Astra adaptation.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Protocol

from PIL import Image, UnidentifiedImageError


class ImageCleanerError(ValueError):
    pass


class ImageProvider(Protocol):
    def clean_image(self, ref_path: Path, ar: str, prompt: str) -> tuple[bytes | None, str | None]: ...


def process_question(provider: ImageProvider, src: Path, dst: Path, ar: str, prompt: str) -> Path:
    """Run PNKmath's source-provider-decode-write flow for one page or crop."""
    if src.resolve() == dst.resolve() or not src.is_file() or dst.exists():
        raise ImageCleanerError("unsafe source or destination for image cleaning")
    gen_data, gen_error = provider.clean_image(src, ar, prompt)
    if gen_data is None:
        raise ImageCleanerError(f"image cleaning failed: {gen_error or 'empty response'}")
    try:
        with Image.open(io.BytesIO(gen_data)) as opened:
            opened.load()
            cleaned = opened.convert("RGB")
        cleaned.save(dst, format="PNG")
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        dst.unlink(missing_ok=True)
        raise ImageCleanerError("cannot decode or save cleaned image") from exc
    return dst

