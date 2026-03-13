#!/bin/zsh
# pdf2md 설치 스크립트 (Apple Silicon / Python 3.11+ 권장)
set -e

# ── Python 버전 확인 ──────────────────────────────────────────
PYTHON=$(command -v python3.11 || command -v python3.12 || command -v python3.10 || command -v python3)
PY_VER=$($PYTHON -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "사용 Python: $PYTHON ($PY_VER)"

MIN_VER="3.9"
if [[ "$(echo -e "$PY_VER\n$MIN_VER" | sort -V | head -1)" != "$MIN_VER" ]]; then
  echo "오류: Python $MIN_VER 이상이 필요합니다." >&2
  exit 1
fi

# ── 가상환경 생성 ─────────────────────────────────────────────
VENV_DIR="$(dirname "$0")/.venv"
if [[ ! -d "$VENV_DIR" ]]; then
  echo "가상환경 생성 중: $VENV_DIR"
  $PYTHON -m venv "$VENV_DIR"
fi
source "$VENV_DIR/bin/activate"

# ── pip 업그레이드 ────────────────────────────────────────────
pip install --upgrade pip --quiet

# ── 패키지 설치 ───────────────────────────────────────────────
echo "패키지 설치 중..."
pip install -r "$(dirname "$0")/requirements.txt"

echo ""
echo "✓ 설치 완료."
echo ""
echo "사용법:"
echo "  source .venv/bin/activate"
echo "  python pdf2md.py <파일.pdf>"
echo "  python pdf2md.py <파일.pdf> -o output.md --dpi 200"
echo "  python pdf2md.py <파일.pdf> --skip-qwen   # OCR만 사용"
