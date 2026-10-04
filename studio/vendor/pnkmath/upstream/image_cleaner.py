#!/usr/bin/env python3
"""NGD Image Cleaner — nano-banana로 문제 이미지에서 손글씨/필기 흔적을 제거.

원본 `question_images/q{N}.png`는 보존하고 정리본을 `question_images/cleaned/q{N}.png`로 저장한다.
extractor 정확도와 figure ref 품질을 동시에 끌어올리는 전처리 단계.

CLI usage:
  python3 image_cleaner.py \
    --question-images-dir "inputs/시험지 제작/question_images" \
    --status-out "inputs/시험지 제작/.v3cache/cleaning_status.json" \
    [--question N]       # 단일 문제만 재처리
    [--no-clean]         # nano-banana 호출 없이 원본을 복사만
"""

import argparse
import io
import json
import re
import shutil
import sys
from pathlib import Path

from PIL import Image
from image_provider_adapter import IMAGE_PROVIDERS, ImageProviderError, create_image_provider

# SKILL.md Step 2-0의 cleaning prompt — verbatim.
CLEANING_PROMPT = (
    "This image will be used as a figure embedded in a typeset Korean math exam document. "
    "The output is a digital figure, NOT a scan of paper. "
    "Output a pure white background (#FFFFFF) with no paper texture, no gray tint, no beige, no shadow. "
    "Remove all handwriting, pen marks, pencil strokes, and paper noise. "
    "Keep all printed text, numbers, equations, tables, circle markers, and printed figures/diagrams exactly as in the source. "
    "Crisp black ink on pure white. No anti-aliasing artifacts at borders. "
    "Do not change, add, or remove any numbers, symbols, or mathematical expressions."
)


def aspect_ratio_str(w: int, h: int) -> str:
    r = w / h
    if r > 1.5:
        return "16:9"
    elif r > 1.1:
        return "4:3"
    elif r > 0.9:
        return "1:1"
    elif r > 0.6:
        return "3:4"
    else:
        return "9:16"


def discover_questions(question_images_dir: Path) -> list[int]:
    if not question_images_dir.is_dir():
        return []
    nums: list[int] = []
    pat = re.compile(r"^q(\d+)\.png$")
    for entry in question_images_dir.iterdir():
        m = pat.match(entry.name)
        if m:
            try:
                nums.append(int(m.group(1)))
            except ValueError:
                continue
    return sorted(nums)


def process_question(
    provider,
    n: int,
    question_images_dir: Path,
    cleaned_dir: Path,
    no_clean: bool,
) -> dict:
    padded = f"{n:02d}"
    src = question_images_dir / f"q{padded}.png"
    if not src.exists():
        return {"status": "failed", "error": f"source image missing: {src}"}

    dst = cleaned_dir / f"q{padded}.png"

    if no_clean:
        shutil.copyfile(str(src), str(dst))
        print(f"  [Q{n}] --no-clean: 원본 복사")
        return {"status": "ok", "image": str(dst), "cleaned": False}

    img = Image.open(str(src))
    iw, ih = img.size
    ar = aspect_ratio_str(iw, ih)
    provider_label = getattr(provider, "label", "image provider")
    print(f"  [Q{n}] {provider_label} cleaning 중... (aspect={ar})")

    gen_data, gen_error = provider.clean_image(src, ar, CLEANING_PROMPT)
    if gen_data is None:
        # 실패 시 원본을 복사해 다운스트림이 계속 진행되게 한다.
        shutil.copyfile(str(src), str(dst))
        print(f"  [Q{n}] cleaning 실패 → 원본 fallback: {gen_error}")
        return {
            "status": "failed",
            "image": str(dst),
            "cleaned": False,
            "error": f"{provider_label} cleaning failed: {gen_error} (original copied as fallback)",
        }

    try:
        gen_img = Image.open(io.BytesIO(gen_data)).convert("RGB")
        gen_img.save(str(dst))
    except Exception as e:
        shutil.copyfile(str(src), str(dst))
        return {
            "status": "failed",
            "image": str(dst),
            "cleaned": False,
            "error": f"failed to decode/save generated image: {e}",
        }

    print(f"  [Q{n}] 완료 → {dst}")
    return {"status": "ok", "image": str(dst), "cleaned": True}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="NGD Image Cleaner — nano-banana로 문제 이미지 손글씨 제거"
    )
    parser.add_argument(
        "--question-images-dir",
        required=True,
        help="원본 q{N}.png들이 있는 폴더 (정리본은 그 아래 cleaned/ 에 저장)",
    )
    parser.add_argument(
        "--status-out",
        required=True,
        help="cleaning_status.json 출력 경로",
    )
    parser.add_argument(
        "--question",
        type=int,
        default=None,
        metavar="N",
        help="단일 문제만 처리",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="이미지 provider 호출 없이 원본을 cleaned/ 로 복사만",
    )
    parser.add_argument(
        "--image-provider",
        choices=IMAGE_PROVIDERS,
        default="gemini",
        help="이미지 정리 provider (default: gemini)",
    )
    args = parser.parse_args()

    question_images_dir = Path(args.question_images_dir)
    cleaned_dir = question_images_dir / "cleaned"
    status_out_path = Path(args.status_out)

    if not args.no_clean:
        try:
            provider = create_image_provider(args.image_provider)
        except ImageProviderError as e:
            print(f"ERROR: {e}")
            sys.exit(1)
    else:
        provider = None

    cleaned_dir.mkdir(parents=True, exist_ok=True)
    status_out_path.parent.mkdir(parents=True, exist_ok=True)

    if args.question is not None:
        targets = [args.question]
    else:
        targets = discover_questions(question_images_dir)

    if not targets:
        print("처리할 문제 이미지 없음")
        status_out_path.write_text(
            json.dumps({"status": "done", "questions": {}}, ensure_ascii=False),
            encoding="utf-8",
        )
        return

    print(f"이미지 정리 시작: {len(targets)}개 (no_clean={args.no_clean}, provider={args.image_provider})")
    questions_status: dict[str, dict] = {}
    for n in targets:
        q_result = process_question(
            provider, n, question_images_dir, cleaned_dir, args.no_clean
        )
        questions_status[str(n)] = q_result

    statuses = {v["status"] for v in questions_status.values()}
    if statuses == {"ok"}:
        top_status = "done"
    elif statuses == {"failed"}:
        top_status = "failed"
    else:
        top_status = "partial"

    status_data = {"status": top_status, "questions": questions_status}
    status_out_path.write_text(
        json.dumps(status_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"\n완료: status={top_status}")
    failed_qs = [k for k, v in questions_status.items() if v["status"] == "failed"]
    if failed_qs:
        print(f"실패(원본 fallback): {failed_qs}")
    # cleaning 실패는 hard fail이 아니라 fallback이 있으므로 exit 0.


if __name__ == "__main__":
    main()
