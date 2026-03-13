#!/usr/bin/env python3
"""
PDF → Markdown 변환기 (MLX / Apple M2 최적화)

파이프라인
──────────
1. 모델 자동 다운로드 (HuggingFace Hub, 캐시 재사용)
2. PyMuPDF
   - 각 페이지 → RGB 이미지
   - 삽입된 래스터 이미지(그림·도표·사진) 별도 추출
3. DeepSeek-OCR-2  [mlx_vlm, 8-bit]
   - 페이지 전체 텍스트 OCR
   - 삽입 이미지별 시각 설명  ← OCR 이 놓친 이미지 보완
   → 완료 후 모델 해제
4. Qwen3.5-4B  [mlx_lm, 4-bit]
   - OCR 원시 텍스트 + 이미지 설명 → 마크다운 구조화
   → 완료 후 모델 해제
5. 페이지 구분자(---) 포함 .md 파일 저장

메모리 전략 (M2 16 GB Unified Memory)
  두 대형 모델을 동시에 올리지 않기 위해
  OCR 단계 완료 → Metal 캐시 해제 → Qwen 로드 순서로 처리한다.
"""

from __future__ import annotations

import argparse
import gc
import io
import sys
import tempfile
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image
import mlx.core as mx


# ──────────────────────────────────────────────────────────────
# 모델 ID (mlx-community 공식 MLX 최적화 버전)
# ──────────────────────────────────────────────────────────────
OCR_MODEL_ID  = "mlx-community/DeepSeek-OCR-2-8bit"   # VLM (페이지 OCR)
VLM_MODEL_ID  = "mlx-community/Qwen3.5-4B-MLX-4bit"   # VLM (이미지 설명 + 마크다운)

DEFAULT_DPI   = 150   # M2 16 GB 기준 속도/품질 균형
MAX_TOKENS    = 4096
MIN_IMAGE_PX  = 100   # 삽입 이미지 최소 크기 (아이콘 등 제외)


# ──────────────────────────────────────────────────────────────
# 모델 자동 다운로드
# ──────────────────────────────────────────────────────────────
def ensure_model(repo_id: str) -> None:
    """
    모델이 HuggingFace 캐시에 없으면 다운로드한다.
    이미 캐시된 경우 즉시 반환한다.
    """
    from huggingface_hub import snapshot_download, try_to_load_from_cache
    from huggingface_hub.utils import RepositoryNotFoundError

    # 캐시 존재 여부 빠르게 확인 (config.json 기준)
    cached = try_to_load_from_cache(repo_id, "config.json")
    if cached is not None and cached != "UNAVAILABLE":  # 캐시 히트
        print(f"  ✓ 캐시 사용: {repo_id}")
        return

    print(f"  ↓ 다운로드 시작: {repo_id}")
    print(f"    (첫 실행 시에만 진행됩니다. 이후에는 캐시를 재사용합니다)")
    try:
        snapshot_download(
            repo_id=repo_id,
            ignore_patterns=["*.pt", "*.bin", "*.gguf"],  # safetensors·MLX 파일만
        )
    except RepositoryNotFoundError:
        print(f"  오류: 모델을 찾을 수 없습니다 → {repo_id}", file=sys.stderr)
        raise
    print(f"  ✓ 다운로드 완료: {repo_id}\n")


def ensure_all_models() -> None:
    print("=== 모델 준비 ===")
    ensure_model(OCR_MODEL_ID)
    ensure_model(VLM_MODEL_ID)
    print()


# ──────────────────────────────────────────────────────────────
# PDF 유틸
# ──────────────────────────────────────────────────────────────
def pdf_to_page_images(pdf_path: str, dpi: int) -> list[Image.Image]:
    """각 페이지를 PIL Image(RGB)로 반환."""
    doc = fitz.open(pdf_path)
    scale = dpi / 72
    pages = []
    for page in doc:
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB)
        pages.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
    doc.close()
    return pages


def extract_embedded_images(pdf_path: str) -> dict[int, list[Image.Image]]:
    """
    페이지에 삽입된 래스터 이미지를 추출한다.
    반환: {0-based 페이지 인덱스: [PIL Image, ...]}
    """
    doc = fitz.open(pdf_path)
    result: dict[int, list[Image.Image]] = {}
    for idx, page in enumerate(doc):
        imgs: list[Image.Image] = []
        for info in page.get_images(full=True):
            xref = info[0]
            try:
                base = doc.extract_image(xref)
            except Exception:
                continue
            img = Image.open(io.BytesIO(base["image"])).convert("RGB")
            if max(img.size) >= MIN_IMAGE_PX:
                imgs.append(img)
        if imgs:
            result[idx] = imgs
    doc.close()
    return result


def _save_tmp(img: Image.Image) -> str:
    """PIL Image → 임시 PNG 경로."""
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    img.save(tmp.name, format="PNG")
    tmp.close()
    return tmp.name


def _free_mlx() -> None:
    gc.collect()
    try:
        mx.metal.clear_cache()
    except AttributeError:
        pass


# ──────────────────────────────────────────────────────────────
# Stage 1 : OCR + 이미지 설명  (DeepSeek-OCR-2, mlx_vlm)
# ──────────────────────────────────────────────────────────────
_OCR_PROMPT = (
    "Extract ALL text visible in this document page image exactly as it appears. "
    "Preserve headings, paragraphs, tables, bullet lists, footnotes, and captions. "
    "If a region contains only an image or diagram with no readable text, "
    "write '[IMAGE]' as a placeholder."
)

_IMG_DESC_PROMPT = (
    "Describe this image concisely and accurately in one to three sentences. "
    "Focus on content relevant to a document: charts, graphs, diagrams, photos, etc. "
    "Skip purely decorative elements."
)


def _vlm_generate(model, processor, image_path: str | None, prompt: str,
                  max_tokens: int = MAX_TOKENS) -> str:
    from mlx_vlm import generate as vlm_generate
    return vlm_generate(
        model, processor, image_path, prompt,
        max_tokens=max_tokens, verbose=False,
    ).strip()


def run_ocr_stage(page_images: list[Image.Image]) -> list[str]:
    """
    DeepSeek-OCR-2로 각 페이지의 텍스트를 추출한다.
    완료 후 모델 해제.
    """
    from mlx_vlm import load as vlm_load
    from mlx_vlm.prompt_utils import apply_chat_template
    from mlx_vlm.utils import load_config

    print(f"[1/2] DeepSeek-OCR-2 로드 중...")
    model, processor = vlm_load(OCR_MODEL_ID)
    config = load_config(OCR_MODEL_ID)
    print(f"      로드 완료. {len(page_images)}페이지 OCR 시작.\n")

    raw_texts: list[str] = []
    for i, page_img in enumerate(page_images, start=1):
        print(f"  OCR  [{i:>3}/{len(page_images)}]", end=" ", flush=True)
        path = _save_tmp(page_img)
        prompt = apply_chat_template(config, _OCR_PROMPT, num_images=1)
        raw_texts.append(_vlm_generate(model, processor, path, prompt))
        Path(path).unlink(missing_ok=True)
        print("완료")

    del model, processor
    _free_mlx()
    print("\n      DeepSeek-OCR-2 해제.\n")
    return raw_texts


# ──────────────────────────────────────────────────────────────
# Stage 2 : 이미지 설명 + 마크다운 변환  (Qwen3.5-VL, mlx_vlm)
# ──────────────────────────────────────────────────────────────
_MD_SYSTEM = """\
You are a document formatting expert.
Convert the given raw OCR text (and optional image descriptions) into clean, well-structured Markdown.

Rules:
- Use # ## ### for headings based on visual hierarchy
- Convert tabular data to Markdown tables
- Preserve bullet and numbered lists exactly
- Use **bold** and *italic* where appropriate
- Render image descriptions as blockquotes: > **[Figure N]** <description>
- Use ``` fenced code blocks for code or monospace content
- Do NOT add or invent content absent from the input
- Output ONLY the Markdown — no preamble, no explanation"""


def _describe_embedded_images(
    model, processor, config,
    embedded_imgs: list[Image.Image],
    page_num: int,
) -> list[str]:
    """삽입 이미지 각각을 Qwen3.5-VL로 설명한다."""
    from mlx_vlm import generate as vlm_generate
    from mlx_vlm.prompt_utils import apply_chat_template

    descriptions: list[str] = []
    for j, img in enumerate(embedded_imgs, start=1):
        print(f"    그림  [페이지 {page_num}, 그림 {j}]", end=" ", flush=True)
        path = _save_tmp(img)
        prompt = apply_chat_template(config, _IMG_DESC_PROMPT, num_images=1)
        desc = _vlm_generate(model, processor, path, prompt, max_tokens=512)
        Path(path).unlink(missing_ok=True)
        descriptions.append(desc)
        print("완료")
    return descriptions


def run_vlm_stage(
    raw_texts: list[str],
    embedded: dict[int, list[Image.Image]],
) -> list[str]:
    """
    Qwen3.5-VL로:
      1) 삽입 이미지 시각 설명 (OCR이 놓친 그림·도표 보완)
      2) OCR 텍스트 + 이미지 설명 → 마크다운 구조화
    완료 후 모델 해제.
    """
    from mlx_vlm import load as vlm_load
    from mlx_vlm.prompt_utils import apply_chat_template
    from mlx_vlm.utils import load_config

    print(f"[2/2] Qwen3.5-VL 로드 중...")
    model, processor = vlm_load(VLM_MODEL_ID)
    config = load_config(VLM_MODEL_ID)
    print(f"      로드 완료. {len(raw_texts)}페이지 처리 시작.\n")

    md_pages: list[str] = []
    for i, raw in enumerate(raw_texts, start=1):
        # ── 삽입 이미지 설명 ────────────────────────────────────
        page_imgs = embedded.get(i - 1, [])
        if page_imgs:
            print(f"  이미지 감지 [페이지 {i}] {len(page_imgs)}개")
            descs = _describe_embedded_images(model, processor, config, page_imgs, i)
        else:
            descs = []

        # ── 마크다운 변환 ────────────────────────────────────────
        print(f"  MD   [{i:>3}/{len(raw_texts)}]", end=" ", flush=True)

        figure_block = ""
        if descs:
            lines = [f"> **[Figure {k}]** {d}" for k, d in enumerate(descs, 1)]
            figure_block = "\n\n" + "\n".join(lines)

        user_msg = (
            f"[Page {i}]\n\n"
            f"--- RAW OCR TEXT ---\n{raw}\n--- END ---"
            f"{figure_block}\n\n"
            "Convert to Markdown:"
        )
        # 텍스트 전용 생성 (이미지 없이 프롬프트만)
        prompt = apply_chat_template(config, user_msg, num_images=0)
        result = _vlm_generate(model, processor, None, prompt)
        md_pages.append(result)
        print("완료")

    del model, processor
    _free_mlx()
    print("\n      Qwen3.5-VL 해제.\n")
    return md_pages


# ──────────────────────────────────────────────────────────────
# 메인 파이프라인
# ──────────────────────────────────────────────────────────────
def convert(
    pdf_path: str,
    output_path: str | None = None,
    dpi: int = DEFAULT_DPI,
    skip_qwen: bool = False,
) -> str:
    src = Path(pdf_path)
    if not src.exists():
        raise FileNotFoundError(f"PDF 파일을 찾을 수 없습니다: {src}")
    dst = Path(output_path) if output_path else src.with_suffix(".md")

    # 0. 모델 다운로드 (캐시 있으면 스킵)
    ensure_all_models()

    print("=== 변환 시작 ===")
    print(f"입력: {src}  출력: {dst}  DPI: {dpi}\n")

    # 1. PDF → 이미지
    print("PDF 렌더링 중...")
    page_images = pdf_to_page_images(str(src), dpi=dpi)
    print(f"  {len(page_images)}페이지 완료.\n")

    # 2. 삽입 이미지 탐지
    print("삽입 이미지 탐지 중...")
    embedded = extract_embedded_images(str(src))
    total_emb = sum(len(v) for v in embedded.values())
    if embedded:
        print(f"  {len(embedded)}개 페이지 / 총 {total_emb}개 이미지 발견.\n")
    else:
        print("  삽입 이미지 없음.\n")

    # 3. OCR (DeepSeek-OCR-2)
    raw_texts = run_ocr_stage(page_images)
    del page_images
    _free_mlx()

    # 4. 이미지 설명 + 마크다운 변환 (Qwen3.5-VL)
    if skip_qwen:
        md_pages = raw_texts
    else:
        md_pages = run_vlm_stage(raw_texts, embedded)

    # 5. 저장
    content = "\n\n---\n\n".join(md_pages)
    dst.write_text(content, encoding="utf-8")
    print(f"=== 완료: {dst} ===")
    return str(dst)


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="PDF → Markdown 변환 (MLX / Apple Silicon 최적화)"
    )
    parser.add_argument("pdf", help="변환할 PDF 파일 경로")
    parser.add_argument("-o", "--output", metavar="FILE",
                        help="출력 .md 파일 경로 (기본: 입력파일명.md)")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI,
                        help=f"렌더링 해상도 (기본 {DEFAULT_DPI}, 높을수록 정확)")
    parser.add_argument("--skip-qwen", action="store_true",
                        help="Qwen 마크다운 변환 생략 (OCR 원시 결과 저장)")
    args = parser.parse_args()

    try:
        convert(args.pdf, args.output, dpi=args.dpi, skip_qwen=args.skip_qwen)
    except FileNotFoundError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n중단되었습니다.")
        sys.exit(0)


if __name__ == "__main__":
    main()
