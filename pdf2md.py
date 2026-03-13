#!/usr/bin/env python3
"""
PDF → Markdown 변환기

모드
────
  원격 (기본): LM Studio OpenAI 호환 API (localhost:1234)
               qwen/qwen3.5-35b-a3b
  로컬 (--local): mlx_vlm + mlx-community/Qwen3.5-4B-MLX-4bit
                  Apple Silicon 전용, 인터넷 불필요

파이프라인
──────────
1. PyMuPDF
   - 각 페이지 → RGB 이미지
   - 삽입된 래스터 이미지(그림·도표·사진) 별도 추출
2. LLM (원격 또는 로컬)
   - Stage 1: 페이지 이미지 → 원시 텍스트 OCR (비전 입력)
   - Stage 2: OCR 텍스트 + 이미지 설명 → 마크다운 구조화
3. 페이지 구분자(---) 포함 .md 파일 저장

설정 (원격 모드)
  LM_STUDIO_BASE_URL  환경변수로 엔드포인트 변경 가능 (기본 http://127.0.0.1:1234/v1)
  LM_STUDIO_MODEL     환경변수로 모델 변경 가능 (기본 qwen/qwen3.5-35b-a3b)
"""

from __future__ import annotations

import argparse
import base64
import gc
import io
import os
import sys
import tempfile
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image


# ──────────────────────────────────────────────────────────────
# 사고 과정 제거
# ──────────────────────────────────────────────────────────────
import re as _re

def strip_thinking(text: str) -> str:
    """
    모델이 출력한 사고 과정(Chain-of-Thought)을 제거하고 실제 내용만 반환한다.

    케이스별 처리:
    A. 텍스트가 <think> 또는 "Thinking Process:" 로 시작 + </think> 존재
       → </think> 이후를 실제 내용으로 사용
    B. 텍스트가 <think> 또는 "Thinking Process:" 로 시작 + </think> 없음
       → 전체가 사고 과정, 빈 문자열 반환
    C. 중간에 <think>...</think> 블록이 포함된 경우 (일반 내용에 사고 삽입)
       → 블록만 제거하고 나머지 유지
    """
    stripped = text.lstrip()
    starts_with_thinking = (
        stripped.startswith("<think>") or
        stripped.startswith("Thinking Process:")
    )

    if starts_with_thinking:
        if "</think>" in text:
            # A: </think> 이후가 실제 내용 (마지막 </think> 기준)
            idx = text.rfind("</think>")
            text = text[idx + len("</think>"):].lstrip("\n")
        else:
            # B: 전체가 사고 과정
            text = ""
    else:
        # C: 내용 중간에 삽입된 <think>...</think> 블록 제거
        text = _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL)

    # 남은 고아 태그 정리
    text = text.replace("</think>", "").replace("<think>", "")
    return text.strip()


# ──────────────────────────────────────────────────────────────
# LM Studio 설정
# ──────────────────────────────────────────────────────────────
LM_STUDIO_BASE_URL = os.environ.get("LM_STUDIO_BASE_URL", "http://127.0.0.1:1234/v1")
LM_STUDIO_MODEL    = os.environ.get("LM_STUDIO_MODEL",    "qwen/qwen3.5-35b-a3b")

LOCAL_VLM_MODEL_ID = "mlx-community/Qwen3.5-4B-MLX-4bit"

DEFAULT_DPI   = 150
MAX_TOKENS    = 4096
MIN_IMAGE_PX  = 100


# ──────────────────────────────────────────────────────────────
# OpenAI 클라이언트 (LM Studio 호환)
# ──────────────────────────────────────────────────────────────
def _get_client():
    from openai import OpenAI
    return OpenAI(base_url=LM_STUDIO_BASE_URL, api_key="lm-studio")


def _img_to_b64(img: Image.Image) -> str:
    """PIL Image → PNG base64 문자열."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _chat(client, messages: list[dict], max_tokens: int = MAX_TOKENS) -> str:
    resp = client.chat.completions.create(
        model=LM_STUDIO_MODEL,
        messages=messages,
        max_tokens=max_tokens,
        temperature=0.0,
    )
    return resp.choices[0].message.content.strip()


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


# ──────────────────────────────────────────────────────────────
# Stage 1 : OCR  (비전 입력 → 원시 텍스트)
# ──────────────────────────────────────────────────────────────
_OCR_PROMPT = (
    "You are an OCR engine. Extract all text from this document page image exactly as it appears. "
    "Preserve the original structure including headings, lists, and tables. "
    "Output only the extracted text with no commentary."
)


def run_ocr_stage(client, page_images: list[Image.Image]) -> list[str]:
    """각 페이지 이미지를 LM Studio에 전송해 원시 텍스트를 추출한다."""
    print(f"[1/2] OCR 시작 — {len(page_images)}페이지 / 모델: {LM_STUDIO_MODEL}\n")
    raw_texts: list[str] = []
    for i, img in enumerate(page_images, start=1):
        print(f"  OCR  [{i:>3}/{len(page_images)}]", end=" ", flush=True)
        b64 = _img_to_b64(img)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": _OCR_PROMPT},
                ],
            }
        ]
        raw_texts.append(strip_thinking(_chat(client, messages)))
        print("완료")
    print()
    return raw_texts


# ──────────────────────────────────────────────────────────────
# Stage 2 : 이미지 설명 + 마크다운 변환
# ──────────────────────────────────────────────────────────────
_IMG_DESC_PROMPT = (
    "Describe this image concisely and accurately in one to three sentences. "
    "Focus on content relevant to a document: charts, graphs, diagrams, photos, etc. "
    "Skip purely decorative elements."
)

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


def _describe_image(client, img: Image.Image) -> str:
    b64 = _img_to_b64(img)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": _IMG_DESC_PROMPT},
            ],
        }
    ]
    return _chat(client, messages, max_tokens=512)


def run_vlm_stage(
    client,
    raw_texts: list[str],
    embedded: dict[int, list[Image.Image]],
) -> list[str]:
    """
    LM Studio로:
      1) 삽입 이미지 시각 설명 (OCR이 놓친 그림·도표 보완)
      2) OCR 텍스트 + 이미지 설명 → 마크다운 구조화
    """
    print(f"[2/2] 마크다운 변환 시작 — {len(raw_texts)}페이지\n")
    md_pages: list[str] = []
    for i, raw in enumerate(raw_texts, start=1):
        # ── 삽입 이미지 설명 ────────────────────────────────────
        page_imgs = embedded.get(i - 1, [])
        descs: list[str] = []
        if page_imgs:
            print(f"  이미지 감지 [페이지 {i}] {len(page_imgs)}개")
            for j, img in enumerate(page_imgs, start=1):
                print(f"    그림  [페이지 {i}, 그림 {j}]", end=" ", flush=True)
                descs.append(_describe_image(client, img))
                print("완료")

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
        messages = [
            {"role": "system", "content": _MD_SYSTEM},
            {"role": "user",   "content": user_msg},
        ]
        md_pages.append(strip_thinking(_chat(client, messages)))
        print("완료")

    print()
    return md_pages


# ──────────────────────────────────────────────────────────────
# 로컬 모드 : mlx_vlm + Qwen3.5-4B MLX 4-bit
# ──────────────────────────────────────────────────────────────
def _save_tmp(img: Image.Image) -> str:
    """PIL Image → 임시 PNG 경로 (mlx_vlm은 파일 경로 필요)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    img.save(tmp.name, format="PNG")
    tmp.close()
    return tmp.name


def _mlx_generate(model, processor, image_path: str | None, prompt: str,
                  max_tokens: int = MAX_TOKENS) -> str:
    from mlx_vlm import generate as vlm_generate
    return vlm_generate(
        model, processor,
        prompt=prompt,
        image=image_path,
        max_tokens=max_tokens,
        temperature=0.0,
        verbose=False,
    ).strip()


def run_ocr_stage_local(page_images: list[Image.Image]) -> list[str]:
    """mlx_vlm으로 각 페이지 이미지에서 원시 텍스트를 추출한다."""
    from mlx_vlm import load as vlm_load
    print(f"[1/2] OCR 시작 (로컬) — {len(page_images)}페이지 / {LOCAL_VLM_MODEL_ID}")
    print("      모델 로드 중...")
    model, processor = vlm_load(LOCAL_VLM_MODEL_ID)
    print(f"      로드 완료.\n")

    raw_texts: list[str] = []
    for i, img in enumerate(page_images, start=1):
        print(f"  OCR  [{i:>3}/{len(page_images)}]", end=" ", flush=True)
        path = _save_tmp(img)
        raw_texts.append(strip_thinking(_mlx_generate(model, processor, path, _OCR_PROMPT)))
        Path(path).unlink(missing_ok=True)
        print("완료")

    print()
    return model, processor, raw_texts


def run_vlm_stage_local(
    model, processor,
    raw_texts: list[str],
    embedded: dict[int, list[Image.Image]],
) -> list[str]:
    """mlx_vlm으로 이미지 설명 + 마크다운 변환한다. 완료 후 모델 해제."""
    print(f"[2/2] 마크다운 변환 시작 (로컬) — {len(raw_texts)}페이지\n")
    md_pages: list[str] = []
    for i, raw in enumerate(raw_texts, start=1):
        page_imgs = embedded.get(i - 1, [])
        descs: list[str] = []
        if page_imgs:
            print(f"  이미지 감지 [페이지 {i}] {len(page_imgs)}개")
            for j, img in enumerate(page_imgs, start=1):
                print(f"    그림  [페이지 {i}, 그림 {j}]", end=" ", flush=True)
                path = _save_tmp(img)
                descs.append(_mlx_generate(model, processor, path, _IMG_DESC_PROMPT, max_tokens=512))
                Path(path).unlink(missing_ok=True)
                print("완료")

        print(f"  MD   [{i:>3}/{len(raw_texts)}]", end=" ", flush=True)
        figure_block = ""
        if descs:
            lines = [f"> **[Figure {k}]** {d}" for k, d in enumerate(descs, 1)]
            figure_block = "\n\n" + "\n".join(lines)

        prompt = (
            f"{_MD_SYSTEM}\n\n"
            f"[Page {i}]\n\n"
            f"--- RAW OCR TEXT ---\n{raw}\n--- END ---"
            f"{figure_block}\n\n"
            "Convert to Markdown:"
        )
        md_pages.append(strip_thinking(_mlx_generate(model, processor, None, prompt)))
        print("완료")

    del model, processor
    gc.collect()
    try:
        import mlx.core as mx
        mx.metal.clear_cache()
    except Exception:
        pass
    print("\n      Qwen3.5-4B MLX 해제.\n")
    return md_pages


# ──────────────────────────────────────────────────────────────
# 메인 파이프라인
# ──────────────────────────────────────────────────────────────
def parse_pages(spec: str, total: int) -> list[int]:
    """
    페이지 범위 문자열을 0-based 인덱스 리스트로 변환한다.
    예) "1-3,5,7-9"  →  [0, 1, 2, 4, 6, 7, 8]
    페이지 번호는 1-based, 반환값은 0-based.
    """
    indices: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            lo, hi = int(lo), int(hi)
            if lo < 1 or hi > total or lo > hi:
                raise ValueError(f"잘못된 페이지 범위: {part} (총 {total}페이지)")
            indices.update(range(lo - 1, hi))
        else:
            n = int(part)
            if n < 1 or n > total:
                raise ValueError(f"잘못된 페이지 번호: {n} (총 {total}페이지)")
            indices.add(n - 1)
    return sorted(indices)


def convert(
    pdf_path: str,
    output_path: str | None = None,
    dpi: int = DEFAULT_DPI,
    skip_md: bool = False,
    pages: str | None = None,
    local: bool = False,
) -> str:
    src = Path(pdf_path)
    if not src.exists():
        raise FileNotFoundError(f"PDF 파일을 찾을 수 없습니다: {src}")
    dst = Path(output_path) if output_path else src.with_suffix(".md")

    print("=== 변환 시작 ===")
    print(f"입력: {src}  출력: {dst}  DPI: {dpi}")
    if local:
        print(f"모드: 로컬 (mlx_vlm)  모델: {LOCAL_VLM_MODEL_ID}\n")
    else:
        print(f"모드: 원격 (LM Studio)  엔드포인트: {LM_STUDIO_BASE_URL}  모델: {LM_STUDIO_MODEL}\n")

    client = None if local else _get_client()

    # 1. PDF → 이미지
    print("PDF 렌더링 중...")
    all_page_images = pdf_to_page_images(str(src), dpi=dpi)
    total_pages = len(all_page_images)
    print(f"  전체 {total_pages}페이지.\n")

    # 페이지 선택
    if pages:
        indices = parse_pages(pages, total_pages)
        page_images = [all_page_images[i] for i in indices]
        print(f"  선택된 페이지: {[i+1 for i in indices]} ({len(indices)}페이지)\n")
    else:
        indices = list(range(total_pages))
        page_images = all_page_images
    del all_page_images

    # 2. 삽입 이미지 탐지 (선택된 페이지만 필터)
    print("삽입 이미지 탐지 중...")
    all_embedded = extract_embedded_images(str(src))
    # 0-based 원본 인덱스 → 선택 순서 인덱스로 재매핑
    embedded = {new_i: all_embedded[orig_i]
                for new_i, orig_i in enumerate(indices)
                if orig_i in all_embedded}
    total_emb = sum(len(v) for v in embedded.values())
    if embedded:
        print(f"  {len(embedded)}개 페이지 / 총 {total_emb}개 이미지 발견.\n")
    else:
        print("  삽입 이미지 없음.\n")

    # 3. OCR
    if local:
        mlx_model, mlx_processor, raw_texts = run_ocr_stage_local(page_images)
    else:
        raw_texts = run_ocr_stage(client, page_images)
        mlx_model = mlx_processor = None
    del page_images
    gc.collect()

    # 4. 마크다운 변환
    if skip_md:
        md_pages = raw_texts
    elif local:
        md_pages = run_vlm_stage_local(mlx_model, mlx_processor, raw_texts, embedded)
    else:
        md_pages = run_vlm_stage(client, raw_texts, embedded)

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
        description="PDF → Markdown 변환 (LM Studio / OpenAI 호환 API)"
    )
    parser.add_argument("pdf", help="변환할 PDF 파일 경로")
    parser.add_argument("-o", "--output", metavar="FILE",
                        help="출력 .md 파일 경로 (기본: 입력파일명.md)")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI,
                        help=f"렌더링 해상도 (기본 {DEFAULT_DPI}, 높을수록 정확)")
    parser.add_argument("--skip-md", action="store_true",
                        help="마크다운 변환 생략 (OCR 원시 결과 저장)")
    parser.add_argument("--pages", metavar="RANGE",
                        help="변환할 페이지 범위 (예: 1-3,5,7-9). 기본: 전체")
    parser.add_argument("--local", action="store_true",
                        help=f"로컬 MLX 모드 사용 ({LOCAL_VLM_MODEL_ID}, Apple Silicon 전용)")
    args = parser.parse_args()

    try:
        convert(args.pdf, args.output, dpi=args.dpi, skip_md=args.skip_md,
                pages=args.pages, local=args.local)
    except FileNotFoundError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n중단되었습니다.")
        sys.exit(0)


if __name__ == "__main__":
    main()
