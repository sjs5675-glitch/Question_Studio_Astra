#!/usr/bin/env python3
"""Image provider adapters for NGD figure cleaning/regeneration."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path

from PIL import Image


IMAGE_PROVIDERS = ("gemini", "codex-cli")


class ImageProviderError(RuntimeError):
    pass


class BaseImageProvider:
    label = "base"

    def clean_image(
        self, ref_path: Path, ar: str, prompt: str
    ) -> tuple[bytes | None, str | None]:
        raise NotImplementedError

    def regenerate_figure(
        self, ref_path: Path, desc: str, ar: str, prompt_template: str
    ) -> tuple[bytes | None, str | None]:
        raise NotImplementedError


class GeminiImageProvider(BaseImageProvider):
    label = "Gemini"

    def __init__(self) -> None:
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise ImageProviderError("GEMINI_API_KEY environment variable is missing")

        from google import genai

        self.client = genai.Client(api_key=api_key)

    def clean_image(
        self, ref_path: Path, ar: str, prompt: str
    ) -> tuple[bytes | None, str | None]:
        from google.genai import types

        ref_image = Image.open(str(ref_path))
        last_error: str | None = None
        for attempt in range(3):
            try:
                response = self.client.models.generate_content(
                    model="gemini-3.1-flash-image-preview",
                    contents=[prompt, ref_image],
                    config=types.GenerateContentConfig(
                        response_modalities=["TEXT", "IMAGE"],
                        image_config=types.ImageConfig(aspect_ratio=ar, image_size="2K"),
                    ),
                )
                for part in response.candidates[0].content.parts:
                    if part.inline_data is not None:
                        return part.inline_data.data, None
                last_error = "no image in response"
                print(f"    attempt {attempt + 1}: no image, retrying...")
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                print(f"    attempt {attempt + 1}: {e}")
                if attempt < 2:
                    time.sleep(3)
        return None, last_error

    def regenerate_figure(
        self, ref_path: Path, desc: str, ar: str, prompt_template: str
    ) -> tuple[bytes | None, str | None]:
        from google.genai import types

        prompt = prompt_template.format(desc=f"{desc} " if desc else "")
        ref_image = Image.open(str(ref_path))
        last_error: str | None = None
        for attempt in range(3):
            try:
                response = self.client.models.generate_content(
                    model="gemini-3.1-flash-image-preview",
                    contents=[prompt, ref_image],
                    config=types.GenerateContentConfig(
                        response_modalities=["TEXT", "IMAGE"],
                        image_config=types.ImageConfig(aspect_ratio=ar, image_size="1K"),
                    ),
                )
                for part in response.candidates[0].content.parts:
                    if part.inline_data is not None:
                        return part.inline_data.data, None
                last_error = "no image in response"
                print(f"    attempt {attempt + 1}: no image, retrying...")
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                print(f"    attempt {attempt + 1}: {e}")
                if attempt < 2:
                    time.sleep(3)
        return None, last_error


class CodexCliImageProvider(BaseImageProvider):
    label = "Codex CLI ImageGen"

    def __init__(self) -> None:
        self.codex_bin = shutil.which("codex")
        if not self.codex_bin:
            raise ImageProviderError("codex CLI is not available on PATH")

    def clean_image(
        self, ref_path: Path, ar: str, prompt: str
    ) -> tuple[bytes | None, str | None]:
        prompt = """
        Use the imagegen skill and the built-in image generation/editing tool.
        Do not use Python, Pillow, SVG, canvas, shell drawing, or code-based redrawing to create the image.
        Treat the attached image as the edit target.

        Edit it into a clean exam-ready PNG:
        - remove handwriting, pen marks, pencil marks, smudges, scan noise, paper texture, gray tint, and shadows
        - preserve all printed Korean text, numbers, equations, tables, circle markers, figures, diagrams, axes, labels, and mathematical symbols
        - keep the layout and composition as close to the source as possible
        - use crisp black ink on a pure white background
        - do not add, remove, rewrite, translate, or reinterpret printed math content
        """
        return self._run_codex_image_tool(ref_path, prompt, timeout_sec=900)

    def regenerate_figure(
        self, ref_path: Path, desc: str, ar: str, prompt_template: str
    ) -> tuple[bytes | None, str | None]:
        prompt = f"""
        Use the imagegen skill and the built-in image generation/editing tool.
        Do not use Python, Pillow, SVG, canvas, shell drawing, or code-based redrawing to create the image.
        Treat the attached image as the edit target/reference crop.

        Extract only the math figure from the crop and output it as a clean exam-ready PNG:
        - remove handwriting, pen marks, smudges, scan artifacts, page margins, problem text, and answer choice markers
        - preserve geometric elements, axes, curves, labels, point names, numbers, angle markers, length markers, and printed annotations that belong to the figure
        - maintain the figure's composition, proportions, and label positions as close to the source as possible
        - use crisp black lines on a pure white background
        - do not simplify, restructure, translate, or invent mathematical content

        Figure description hint: {desc or "(none)"}
        """
        return self._run_codex_image_tool(ref_path, prompt, timeout_sec=900)

    def _run_codex_image_tool(
        self, ref_path: Path, task_prompt: str, timeout_sec: int
    ) -> tuple[bytes | None, str | None]:
        with tempfile.TemporaryDirectory(prefix="ngd-codex-image-") as tmp:
            tmpdir = Path(tmp)
            input_path = tmpdir / f"input{ref_path.suffix or '.png'}"
            output_path = tmpdir / "output.png"
            shutil.copyfile(str(ref_path), str(input_path))

            full_prompt = textwrap.dedent(f"""
                {task_prompt}

                Save-path requirement:
                - The generated image may first appear under $CODEX_HOME/generated_images.
                - After the built-in image tool finishes, copy the selected generated PNG to exactly:
                  {output_path}
                - If the built-in image generation/editing tool is unavailable, write a short explanation to:
                  {tmpdir / "error.txt"}
                  and do not create output.png.
            """).strip()

            cmd = [
                self.codex_bin,
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "danger-full-access",
                "--enable",
                "image_generation",
                "--cd",
                str(tmpdir),
                "--image",
                str(input_path),
                "-",
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    input=full_prompt,
                    text=True,
                    capture_output=True,
                    timeout=timeout_sec,
                )
            except subprocess.TimeoutExpired:
                return None, f"codex image generation timed out after {timeout_sec}s"

            if proc.returncode != 0:
                stderr = proc.stderr.strip()
                stdout = proc.stdout.strip()
                return None, f"codex exited with {proc.returncode}: {stderr or stdout}"

            if not output_path.exists():
                error_path = tmpdir / "error.txt"
                if error_path.exists():
                    return None, error_path.read_text(encoding="utf-8", errors="replace").strip()
                return None, "codex did not create output.png"

            try:
                with Image.open(str(output_path)) as img:
                    img.verify()
                return output_path.read_bytes(), None
            except Exception as e:
                return None, f"codex output is not a valid image: {e}"


def create_image_provider(provider_id: str) -> BaseImageProvider:
    if provider_id == "gemini":
        return GeminiImageProvider()
    if provider_id == "codex-cli":
        return CodexCliImageProvider()
    raise ImageProviderError(f"unknown image provider: {provider_id}")
