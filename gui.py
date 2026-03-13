#!/usr/bin/env python3
"""
pdf2md GUI — 드래그앤드롭 PDF 변환기 (PyQt6)

실행:
  python gui.py
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal, QObject
from PyQt6.QtGui import QColor, QDragEnterEvent, QDropEvent, QFont, QPalette
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from pdf2md import convert, DEFAULT_DPI, LOCAL_VLM_MODEL_ID, LM_STUDIO_MODEL


# ──────────────────────────────────────────────────────────────
# stdout → 시그널 브릿지
# ──────────────────────────────────────────────────────────────
class _LogBridge(QObject):
    message = pyqtSignal(str)

    def write(self, text: str) -> None:
        self.message.emit(text)

    def flush(self) -> None:
        pass


# ──────────────────────────────────────────────────────────────
# 드롭 영역
# ──────────────────────────────────────────────────────────────
class DropZone(QLabel):
    file_dropped = pyqtSignal(str)

    _NORMAL = "background:#2a2a2a; border:2px dashed #555; border-radius:10px; color:#888;"
    _HOVER  = "background:#2f3f55; border:2px dashed #4f8ef7; border-radius:10px; color:#ccc;"
    _READY  = "background:#1e2e1e; border:2px dashed #4caf50; border-radius:10px; color:#4caf50;"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setText("PDF를 여기에 드래그하거나\n클릭하여 파일 선택")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumHeight(140)
        self.setStyleSheet(self._NORMAL)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self.setStyleSheet(self._HOVER)

    def dragLeaveEvent(self, e):
        self.setStyleSheet(self._NORMAL)

    def dropEvent(self, e: QDropEvent):
        self.setStyleSheet(self._NORMAL)
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if path.lower().endswith(".pdf"):
                self.file_dropped.emit(path)

    def mousePressEvent(self, e):
        path, _ = QFileDialog.getOpenFileName(
            self, "PDF 파일 선택", "", "PDF 파일 (*.pdf);;모든 파일 (*)"
        )
        if path:
            self.file_dropped.emit(path)

    def set_ready(self, name: str):
        self.setText(f"✓  {name}")
        self.setStyleSheet(self._READY)

    def set_done(self, name: str):
        self.setText(f"✓  완료 → {name}")
        self.setStyleSheet(self._READY)

    def set_error(self):
        self.setText("오류 발생 — 로그를 확인하세요")
        self.setStyleSheet(
            "background:#2e1e1e; border:2px dashed #e07b39;"
            "border-radius:10px; color:#e07b39;"
        )

    def reset(self, name: str):
        self.setText(f"✓  {name}")
        self.setStyleSheet(self._READY)


# ──────────────────────────────────────────────────────────────
# 메인 윈도우
# ──────────────────────────────────────────────────────────────
class MainWindow(QWidget):
    _log_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("pdf2md")
        self.setFixedWidth(520)
        self.setStyleSheet("background:#1e1e1e; color:#e8e8e8;")

        self._pdf_path: str | None = None
        self._running = False

        self._build_ui()
        self._log_signal.connect(self._append_log)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)

        # 제목
        title = QLabel("pdf2md")
        title.setFont(QFont("SF Pro Display", 22, QFont.Weight.Bold))
        title.setStyleSheet("color:#4f8ef7;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(title)

        sub = QLabel("PDF → Markdown 변환기")
        sub.setFont(QFont("SF Pro Display", 12))
        sub.setStyleSheet("color:#888;")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(sub)

        # 드롭 영역
        self.drop_zone = DropZone()
        self.drop_zone.file_dropped.connect(self._on_file)
        root.addWidget(self.drop_zone)

        # 옵션 행
        opt = QHBoxLayout()
        opt.setSpacing(12)

        opt.addWidget(self._label("DPI"))
        self.dpi_edit = self._lineedit(str(DEFAULT_DPI), 60)
        opt.addWidget(self.dpi_edit)

        opt.addSpacing(8)
        opt.addWidget(self._label("페이지"))
        self.pages_edit = self._lineedit("예: 1-3,5", 120)
        self.pages_edit.setStyleSheet(
            "background:#2a2a2a; color:#888; border:none;"
            "border-radius:4px; padding:4px 8px;"
        )
        self.pages_edit.focusInEvent = self._pages_focus_in
        opt.addWidget(self.pages_edit)
        opt.addStretch()
        root.addLayout(opt)

        # 체크박스 행
        chk = QHBoxLayout()
        short_name = LOCAL_VLM_MODEL_ID.split("/")[-1]
        self.local_chk   = QCheckBox(f"로컬 MLX ({short_name})")
        self.skip_md_chk = QCheckBox("마크다운 변환 생략")
        for c in (self.local_chk, self.skip_md_chk):
            c.setFont(QFont("SF Pro Display", 12))
            c.setStyleSheet("color:#ccc; spacing:6px;")
            chk.addWidget(c)
        chk.addStretch()
        root.addLayout(chk)

        # 변환 버튼
        self.btn = QPushButton("변환 시작")
        self.btn.setFont(QFont("SF Pro Display", 14, QFont.Weight.Bold))
        self.btn.setFixedHeight(46)
        self.btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn.setStyleSheet(
            "QPushButton { background:#4f8ef7; color:#fff; border-radius:8px; }"
            "QPushButton:hover { background:#3a78e0; }"
            "QPushButton:disabled { background:#555; color:#888; }"
        )
        self.btn.clicked.connect(self._start_convert)
        root.addWidget(self.btn)

        # 로그
        log_label = QLabel("로그")
        log_label.setFont(QFont("SF Pro Display", 11))
        log_label.setStyleSheet("color:#666;")
        root.addWidget(log_label)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("SF Mono", 11))
        self.log.setMinimumHeight(180)
        self.log.setStyleSheet(
            "background:#141414; color:#c8c8c8; border:none; border-radius:6px;"
        )
        root.addWidget(self.log)

    # ── 헬퍼 ───────────────────────────────────────────────────
    def _label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setFont(QFont("SF Pro Display", 13))
        lbl.setStyleSheet("color:#e8e8e8;")
        return lbl

    def _lineedit(self, text: str, width: int) -> QLineEdit:
        e = QLineEdit(text)
        e.setFixedWidth(width)
        e.setFont(QFont("SF Pro Display", 13))
        e.setStyleSheet(
            "background:#2a2a2a; color:#e8e8e8; border:none;"
            "border-radius:4px; padding:4px 8px;"
        )
        return e

    def _pages_focus_in(self, e):
        if self.pages_edit.text() == "예: 1-3,5":
            self.pages_edit.clear()
            self.pages_edit.setStyleSheet(
                "background:#2a2a2a; color:#e8e8e8; border:none;"
                "border-radius:4px; padding:4px 8px;"
            )
        QLineEdit.focusInEvent(self.pages_edit, e)

    # ── 파일 선택 ───────────────────────────────────────────────
    def _on_file(self, path: str):
        self._pdf_path = path
        self.drop_zone.set_ready(Path(path).name)

    # ── 변환 ───────────────────────────────────────────────────
    def _start_convert(self):
        if self._running:
            return
        if not self._pdf_path:
            self._append_log("PDF 파일을 먼저 선택해주세요.\n")
            return

        dpi_str = self.dpi_edit.text().strip()
        try:
            dpi = int(dpi_str)
        except ValueError:
            self._append_log(f"오류: DPI 값이 올바르지 않습니다 — {dpi_str}\n")
            return

        pages_text = self.pages_edit.text().strip()
        pages = None if pages_text in ("", "예: 1-3,5") else pages_text

        self._running = True
        self.btn.setEnabled(False)
        self.btn.setText("변환 중...")
        self.log.clear()

        threading.Thread(target=self._run_convert, args=(
            self._pdf_path, dpi, pages,
            self.local_chk.isChecked(), self.skip_md_chk.isChecked(),
        ), daemon=True).start()

    def _run_convert(self, pdf_path, dpi, pages, local, skip_md):
        bridge = _LogBridge()
        bridge.message.connect(self._log_signal)
        old_stdout = sys.stdout
        sys.stdout = bridge
        try:
            out = convert(pdf_path, dpi=dpi, pages=pages, local=local, skip_md=skip_md)
            self._finish(success=True, result=out)
        except Exception as exc:
            sys.stdout.write(f"\n오류: {exc}\n")
            self._finish(success=False)
        finally:
            sys.stdout = old_stdout

    def _finish(self, success: bool, result: str = ""):
        # 스레드에서 호출되므로 시그널로 메인 스레드에 전달
        self._log_signal.emit("\x00")  # 완료 마커
        self._done_success = success
        self._done_result  = result

    # ── 로그 슬롯 ───────────────────────────────────────────────
    def _append_log(self, text: str):
        if text == "\x00":  # 완료 마커
            self._running = False
            self.btn.setEnabled(True)
            self.btn.setText("변환 시작")
            if self._done_success:
                self.drop_zone.set_done(Path(self._done_result).name)
            else:
                self.drop_zone.set_error()
            return
        self.log.moveCursor(self.log.textCursor().MoveOperation.End)
        self.log.insertPlainText(text)
        self.log.moveCursor(self.log.textCursor().MoveOperation.End)


# ──────────────────────────────────────────────────────────────
# 진입점
# ──────────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
