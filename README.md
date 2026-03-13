# pdf2md

PDF 파일을 Markdown으로 변환하는 CLI 도구입니다.
LM Studio의 OpenAI 호환 API를 통해 원격 LLM을 사용합니다.

---

## 사용 모델

| 모드 | 모델 | 비고 |
|------|------|------|
| 원격 (기본) | `qwen/qwen3.5-35b-a3b` | LM Studio, localhost:1234 |
| 로컬 (`--local`) | `mlx-community/Qwen3.5-4B-MLX-4bit` | Apple Silicon 전용, mlx_vlm |

**원격 모드:** LM Studio를 실행하고 해당 모델을 로드해두어야 합니다.
**로컬 모드:** 첫 실행 시 HuggingFace에서 모델을 자동 다운로드합니다.

---

## 처리 파이프라인

```
PDF
 │
 ├─ [PyMuPDF] ──────→ 페이지 이미지 (DPI 150)
 │
 ├─ [PyMuPDF] ──────→ 삽입 이미지 추출 (그림·도표·사진)
 │
 ├─ [LM Studio API] → Stage 1: 페이지 이미지(비전) → 원시 텍스트 OCR
 │
 └─ [LM Studio API] → Stage 2: ① 삽입 이미지 시각 설명
                                ② OCR 텍스트 + 이미지 설명 → Markdown 구조화
```

---

## 요구사항

- Python 3.10 이상

**원격 모드 (기본)**
- [LM Studio](https://lmstudio.ai/) 실행 중 + `qwen/qwen3.5-35b-a3b` 모델 로드됨
- LM Studio 로컬 서버 활성화 (기본 포트: 1234)

**로컬 모드 (`--local`)**
- Apple Silicon (M1/M2/M3)
- `mlx`, `mlx-lm`, `mlx-vlm` 별도 설치 필요:
  ```zsh
  pip install mlx>=0.21.0 mlx-lm>=0.21.0 mlx-vlm>=0.1.0
  ```

---

## 설치

```zsh
cd pdf2md
pip install -r requirements.txt
```

---

## 사용법

### GUI (드래그앤드롭)

```zsh
python gui.py
```

- PDF 파일을 드롭 영역에 드래그하거나 클릭하여 선택
- DPI, 페이지 범위, 모드 등 옵션 설정 후 **변환 시작** 클릭
- 로그 창에서 실시간 진행 상황 확인

### CLI

```zsh
# 기본 사용 (출력: 문서.md)
python pdf2md.py 문서.pdf

# 출력 파일 지정
python pdf2md.py 문서.pdf -o 결과.md

# 해상도 높이기 (정확도 향상, 속도 느려짐)
python pdf2md.py 문서.pdf --dpi 250

# OCR만 사용 (마크다운 변환 생략, 빠름)
python pdf2md.py 문서.pdf --skip-md

# 로컬 MLX 모드 (LM Studio 불필요, Apple Silicon 전용)
python pdf2md.py 문서.pdf --local

# 페이지 범위 지정
python pdf2md.py 문서.pdf --pages 1-3
python pdf2md.py 문서.pdf --pages 1,3,5
python pdf2md.py 문서.pdf --pages 1-3,5,7-9
```

### 옵션

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `pdf` | — | 변환할 PDF 파일 경로 |
| `-o`, `--output` | `입력파일명.md` | 출력 파일 경로 |
| `--dpi` | `150` | 렌더링 해상도 (높을수록 정확하나 느림) |
| `--skip-md` | `false` | 마크다운 변환 생략, OCR 원시 결과 저장 |
| `--pages` | 전체 | 변환할 페이지 범위 (예: `1-3`, `1,3,5`, `1-3,5,7-9`) |
| `--local` | `false` | 로컬 MLX 모드 (`mlx-community/Qwen3.5-4B-MLX-4bit`, Apple Silicon 전용) |
| `--base-url` | `http://localhost:1234/v1` | LM Studio 엔드포인트 |
| `--model` | `qwen/qwen3.5-35b-a3b` | 사용할 모델 ID |

### 환경변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `LM_STUDIO_BASE_URL` | `http://localhost:1234/v1` | LM Studio 엔드포인트 |
| `LM_STUDIO_MODEL` | `qwen/qwen3.5-35b-a3b` | 사용할 모델 ID |

---

## 출력 형식

- 페이지 구분자: `---`
- 삽입 이미지는 모델이 시각적으로 설명하여 blockquote로 삽입됩니다:
  ```markdown
  > **[Figure 1]** 막대 그래프로 2023년 분기별 매출을 나타낸다. ...
  ```

---

## 변경 히스토리

### v2.3.0 — 2026-03-14

**GUI 추가 (드래그앤드롭)**

- **추가:** `gui.py` — PyQt6 기반 GUI 런처
- 드래그앤드롭으로 PDF 파일 선택 (macOS 네이티브 지원)
- 클릭하여 파일 선택 다이얼로그도 지원
- DPI, 페이지 범위, 로컬 MLX / 마크다운 생략 옵션 제공
- 변환 로그 실시간 표시 (스레드 안전 시그널 브릿지)
- `PyQt6>=6.5.0` 의존성 추가 (tkinterdnd2 macOS 호환 문제로 대체)

### v2.2.0 — 2026-03-13

**로컬 MLX 모드 추가 (`--local`)**

- **추가:** `--local` 옵션 — LM Studio 없이 `mlx-community/Qwen3.5-4B-MLX-4bit`로 로컬 실행
- OCR과 마크다운 변환 모두 동일 모델로 처리 (모델 1회 로드 후 양 스테이지 공유)
- 완료 후 Metal 캐시 해제로 메모리 반환
- mlx/mlx-lm/mlx-vlm은 선택적 의존성 (로컬 모드 사용 시에만 별도 설치)

### v2.1.0 — 2026-03-13

**페이지 범위 선택 옵션 추가**

- **추가:** `--pages RANGE` 옵션 — 변환할 페이지를 지정 가능 (예: `1-3`, `1,3,5`, `1-3,5,7-9`)
- 미지정 시 전체 페이지 처리 (기존 동작 유지)
- 잘못된 범위(페이지 초과, 역순 등) 입력 시 오류 메시지 출력 후 종료

### v2.0.0 — 2026-03-13

**LM Studio 원격 API 전환 (MLX 로컬 실행 제거)**

- **변경:** MLX 기반 로컬 모델 실행 → LM Studio OpenAI 호환 API (`http://localhost:1234/v1`) 사용으로 전환
- **모델:** DeepSeek-OCR-2 (transformers) + Qwen3.5-4B MLX → **qwen/qwen3.5-35b-a3b** (LM Studio) 단일 모델로 통합
- **이유:** Apple Silicon에서 대형 모델 로컬 추론 속도가 너무 느려 원격 서버 활용으로 전환
- **의존성 제거:** `mlx`, `mlx-lm`, `mlx-vlm`, `transformers`, `torch`, `torchvision`, `einops`, `addict`, `easydict`, `huggingface_hub`
- **의존성 추가:** `openai>=1.30.0` (LM Studio API 클라이언트)
- **새 옵션:** `--base-url`, `--model` (CLI) / `LM_STUDIO_BASE_URL`, `LM_STUDIO_MODEL` (환경변수)
- **옵션 변경:** `--skip-qwen` → `--skip-md` (범용 네이밍)
- **제거:** HuggingFace 모델 자동 다운로드 로직 (LM Studio에서 관리)

### v1.0.0 — 2026-03-13

**초기 릴리스**

- MLX 기반 로컬 파이프라인 구현 (Apple Silicon 전용)
- Stage 1: DeepSeek-OCR-2 (transformers + MPS) 페이지 OCR
- Stage 2: Qwen3.5-4B MLX 4-bit 이미지 설명 + 마크다운 변환
- HuggingFace Hub 자동 모델 다운로드 및 캐시 재사용
- PyMuPDF 기반 페이지 렌더링 및 삽입 이미지 추출
