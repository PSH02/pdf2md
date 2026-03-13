# pdf2md

PDF 파일을 Markdown으로 변환하는 CLI 도구입니다.
Apple Silicon(M1/M2/M3)에 최적화된 MLX 모델을 사용합니다.

---

## 사용 모델

| 역할 | 모델 |
|------|------|
| 페이지 텍스트 OCR | [mlx-community/DeepSeek-OCR-2-8bit](https://huggingface.co/mlx-community/DeepSeek-OCR-2-8bit) |
| 이미지 설명 + 마크다운 변환 | [mlx-community/Qwen3.5-4B-MLX-4bit](https://huggingface.co/mlx-community/Qwen3.5-4B-MLX-4bit) |

모델은 **첫 실행 시 자동으로 다운로드**되며, 이후에는 HuggingFace 캐시를 재사용합니다.

---

## 처리 파이프라인

```
PDF
 │
 ├─ [PyMuPDF] ──────→ 페이지 이미지 (DPI 150)
 │
 ├─ [PyMuPDF] ──────→ 삽입 이미지 추출 (그림·도표·사진)
 │
 ├─ [DeepSeek-OCR-2] → 페이지별 원시 텍스트 추출
 │    모델 해제 (Unified Memory 반환)
 │
 └─ [Qwen3.5-VL] ───→ ① 삽입 이미지 시각 설명 (OCR이 놓친 영역 보완)
                       ② OCR 텍스트 + 이미지 설명 → Markdown 구조화
                       모델 해제
```

**메모리 최적화 (M2 16 GB):** 두 모델을 동시에 올리지 않고 순차적으로 로드·해제합니다.

---

## 요구사항

- macOS (Apple Silicon M1/M2/M3)
- Python 3.10 이상 권장

---

## 설치

```zsh
cd pdf2md

# 1. 가상환경 생성 및 패키지 설치 (자동)
./setup.sh

# 2. 가상환경 활성화
source .venv/bin/activate
```

또는 수동으로:

```zsh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 사용법

```zsh
# 기본 사용 (출력: 문서.md)
python pdf2md.py 문서.pdf

# 출력 파일 지정
python pdf2md.py 문서.pdf -o 결과.md

# 해상도 높이기 (정확도 향상, 속도 느려짐)
python pdf2md.py 문서.pdf --dpi 250

# OCR만 사용 (Qwen 마크다운 변환 생략, 빠름)
python pdf2md.py 문서.pdf --skip-qwen
```

### 옵션

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `pdf` | — | 변환할 PDF 파일 경로 |
| `-o`, `--output` | `입력파일명.md` | 출력 파일 경로 |
| `--dpi` | `150` | 렌더링 해상도 (높을수록 정확하나 느림) |
| `--skip-qwen` | `false` | Qwen 변환 생략, OCR 원시 결과 저장 |

---

## 출력 형식

- 페이지 구분자: `---`
- 삽입 이미지는 Qwen이 시각적으로 설명하여 blockquote로 삽입됩니다:
  ```markdown
  > **[Figure 1]** 막대 그래프로 2023년 분기별 매출을 나타낸다. ...
  ```

---

## 모델 다운로드 위치

HuggingFace 기본 캐시 경로:

```
~/.cache/huggingface/hub/
```

환경변수 `HF_HOME` 또는 `HUGGINGFACE_HUB_CACHE`로 변경 가능합니다.
