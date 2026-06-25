#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GV Device Correction Tool — GUI
================================
PySide6 frontend for gv_device_tool.py.
Runs the SSH/Postgres/Redis workflow in a background thread so the UI stays
responsive. Same Corinex blue-light theme as csv_tool_modern.py.

Dependencies:  pip install paramiko PySide6
.exe:          pyinstaller --onefile --windowed --name gv_device_tool_gui gv_device_tool_gui.py
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QObject
from PySide6.QtGui import QColor, QFont, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QGroupBox,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QPushButton,
    QFileDialog,
    QLabel,
    QLineEdit,
    QComboBox,
    QPlainTextEdit,
    QCheckBox,
    QFrame,
    QSizePolicy,
    QMessageBox,
)

import gv_device_tool as core


# ---------------------------------------------------------------------------
# Colors — same palette as csv_tool_modern.py
# ---------------------------------------------------------------------------
ACCENT      = "#0067b8"
ACCENT_DARK = "#004f8c"
OK_GREEN    = "#107c10"
WARN_ORANGE = "#b85c00"
ERR_RED     = "#c42b1c"

LOG_COLORS = {
    "INFO":  "#1b1b1b",
    "OK":    OK_GREEN,
    "WARN":  WARN_ORANGE,
    "ERROR": ERR_RED,
}


# ---------------------------------------------------------------------------
# Stylesheet (identical feel to CSV Tool)
# ---------------------------------------------------------------------------
def _stylesheet() -> str:
    return f"""
    QWidget {{
        background: #eef1f5;
        color: #1b1b1b;
        font-size: 10pt;
    }}
    QFrame#header {{
        background: {ACCENT};
        border: none;
    }}
    QLabel#headerTitle {{
        color: #ffffff;
        font-size: 17pt;
        font-weight: bold;
        background: transparent;
    }}
    QLabel#headerSub {{
        color: #d7e7f6;
        font-size: 10pt;
        background: transparent;
    }}
    QLabel#muted {{ color: #5b6068; background: transparent; }}
    QGroupBox#card {{
        background: #ffffff;
        border: 1px solid #d6dce3;
        border-radius: 8px;
        margin-top: 10px;
        padding: 14px 14px 10px 14px;
        font-weight: bold;
    }}
    QGroupBox#card::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: 12px;
        padding: 0 4px;
        color: {ACCENT};
    }}
    QLineEdit, QComboBox, QPlainTextEdit {{
        background: #ffffff;
        border: 1px solid #c4cbd3;
        border-radius: 4px;
        padding: 5px 8px;
        selection-background-color: {ACCENT};
        selection-color: #ffffff;
    }}
    QLineEdit:focus, QComboBox:focus {{
        border: 1px solid {ACCENT};
    }}
    QLineEdit:disabled, QComboBox:disabled {{
        background: #f2f5f9;
        color: #8a9099;
    }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QPushButton {{
        background: #ffffff;
        color: #1b1b1b;
        border: 1px solid #c4cbd3;
        border-radius: 5px;
        padding: 6px 14px;
    }}
    QPushButton:hover {{ background: #f1f6fb; border-color: {ACCENT}; }}
    QPushButton:pressed {{ background: #e4eef8; }}
    QPushButton:disabled {{ background: #f2f5f9; color: #a0a8b0; border-color: #d6dce3; }}
    QPushButton#primary {{
        background: {ACCENT};
        color: #ffffff;
        border: none;
        border-radius: 7px;
        padding: 11px 26px;
        font-size: 11pt;
        font-weight: bold;
    }}
    QPushButton#primary:hover {{ background: {ACCENT_DARK}; }}
    QPushButton#primary:disabled {{ background: #a0bdd6; color: #e0eaf3; }}
    QPushButton#danger {{
        background: {ERR_RED};
        color: #ffffff;
        border: none;
        border-radius: 7px;
        padding: 11px 26px;
        font-size: 11pt;
        font-weight: bold;
    }}
    QPushButton#danger:hover {{ background: #a32318; }}
    QCheckBox {{ spacing: 6px; }}
    QCheckBox::indicator {{
        width: 16px; height: 16px;
        border: 1px solid #c4cbd3;
        border-radius: 3px;
        background: #ffffff;
    }}
    QCheckBox::indicator:checked {{
        background: {ACCENT};
        border-color: {ACCENT};
    }}
    QPlainTextEdit#log {{
        background: #0f0f14;
        color: #d4d4d4;
        font-family: "Consolas", "Courier New", monospace;
        font-size: 9pt;
        border-radius: 6px;
        border: none;
    }}
    """


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------
class WorkerSignals(QObject):
    log_line  = Signal(str, str)   # (level, message)
    finished  = Signal(int)        # return code


class GvWorker(QThread):
    def __init__(self, cfg: core.Config):
        super().__init__()
        self._cfg = cfg
        self.signals = WorkerSignals()
        self._log = _SignalLog(self.signals.log_line)

    def run(self):
        rc = core.run(self._cfg, self._log)
        self.signals.finished.emit(rc)


class _SignalLog:
    """Adapts the core.Log interface to Qt signals (thread-safe)."""
    def __init__(self, signal):
        self._sig = signal

    def info(self, m):  self._sig.emit("INFO",  m)
    def warn(self, m):  self._sig.emit("WARN",  m)
    def error(self, m): self._sig.emit("ERROR", m)
    def ok(self, m):    self._sig.emit("OK",    m)
    def close(self):    pass


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class GvToolWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GV Device Correction Tool")
        self.resize(820, 760)
        self.setMinimumSize(700, 640)
        self._worker: GvWorker | None = None

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        scroll_area = QWidget()
        scroll_layout = QVBoxLayout(scroll_area)
        scroll_layout.setContentsMargins(18, 16, 18, 16)
        scroll_layout.setSpacing(12)

        scroll_layout.addWidget(self._build_csv_card())
        scroll_layout.addWidget(self._build_connection_card())
        scroll_layout.addWidget(self._build_db_card())
        scroll_layout.addWidget(self._build_options_card())
        scroll_layout.addWidget(self._build_run_card())
        scroll_layout.addWidget(self._build_log_card())
        scroll_layout.addStretch(1)

        root.addWidget(scroll_area)

    # ------------------------------------------------------------------
    # UI builders
    # ------------------------------------------------------------------

    def _build_header(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("header")
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(18, 12, 18, 12)
        lay.setSpacing(2)
        title = QLabel("GV Device Correction Tool")
        title.setObjectName("headerTitle")
        sub = QLabel("Remove incorrectly imported devices from GridValue (Postgres + Redis) and re-import via GV Web UI")
        sub.setObjectName("headerSub")
        sub.setWordWrap(True)
        lay.addWidget(title)
        lay.addWidget(sub)
        return bar

    def _card(self, title: str) -> tuple[QGroupBox, QVBoxLayout]:
        box = QGroupBox(title)
        box.setObjectName("card")
        lay = QVBoxLayout(box)
        lay.setSpacing(8)
        return box, lay

    def _build_csv_card(self) -> QGroupBox:
        box, lay = self._card("① Input CSV")

        hint = QLabel("Select the corrected GV import CSV (columns: serialNumber, macAddress, type, …)")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        row = QHBoxLayout()
        self.csv_edit = QLineEdit()
        self.csv_edit.setPlaceholderText("Path to CSV file…")
        row.addWidget(self.csv_edit)
        btn = QPushButton("Browse…")
        btn.clicked.connect(self._browse_csv)
        row.addWidget(btn)
        lay.addLayout(row)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Device mode:"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(list(core.MODE_TYPE_PREFIXES.keys()))
        self.mode_combo.setCurrentText("auto")
        self.mode_combo.setToolTip(
            "auto — no type-column check\n"
            "repeater / headend / proxy / 1t — warns if type column doesn't match"
        )
        mode_row.addWidget(self.mode_combo)
        mode_row.addStretch(1)
        lay.addLayout(mode_row)

        return box

    def _build_connection_card(self) -> QGroupBox:
        box, lay = self._card("② SSH Connection")

        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        grid.addWidget(QLabel("Host / IP:"), 0, 0, Qt.AlignRight)
        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("e.g. 172.31.2.2")
        grid.addWidget(self.host_edit, 0, 1)

        grid.addWidget(QLabel("Port:"), 0, 2, Qt.AlignRight)
        self.port_edit = QLineEdit("22")
        self.port_edit.setFixedWidth(60)
        grid.addWidget(self.port_edit, 0, 3)

        grid.addWidget(QLabel("SSH User:"), 1, 0, Qt.AlignRight)
        self.user_edit = QLineEdit()
        self.user_edit.setPlaceholderText("e.g. corinex")
        grid.addWidget(self.user_edit, 1, 1)

        grid.addWidget(QLabel("SSH Password:"), 1, 2, Qt.AlignRight)
        self.pw_edit = QLineEdit()
        self.pw_edit.setEchoMode(QLineEdit.Password)
        self.pw_edit.setPlaceholderText("leave blank to use SSH key / agent")
        grid.addWidget(self.pw_edit, 1, 3)

        lay.addLayout(grid)
        return box

    def _build_db_card(self) -> QGroupBox:
        box, lay = self._card("③ Postgres / Redis")

        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        grid.addWidget(QLabel("Postgres container:"), 0, 0, Qt.AlignRight)
        self.pg_container_edit = QLineEdit(core.DEFAULTS["pg_container"])
        grid.addWidget(self.pg_container_edit, 0, 1)

        grid.addWidget(QLabel("Postgres DB:"), 0, 2, Qt.AlignRight)
        self.pg_db_edit = QLineEdit()
        self.pg_db_edit.setPlaceholderText("e.g. corinex")
        grid.addWidget(self.pg_db_edit, 0, 3)

        grid.addWidget(QLabel("Postgres user:"), 1, 0, Qt.AlignRight)
        self.pg_user_edit = QLineEdit(core.DEFAULTS["pg_user"])
        grid.addWidget(self.pg_user_edit, 1, 1)

        grid.addWidget(QLabel("Redis container:"), 1, 2, Qt.AlignRight)
        self.redis_edit = QLineEdit()
        self.redis_edit.setPlaceholderText("e.g. deployment-redis-1")
        grid.addWidget(self.redis_edit, 1, 3)

        lay.addLayout(grid)
        return box

    def _build_options_card(self) -> QGroupBox:
        box, lay = self._card("④ Options")
        row = QHBoxLayout()
        self.dry_run_cb = QCheckBox("Dry-run (show commands only, change nothing)")
        self.dry_run_cb.setChecked(True)
        self.no_redis_cb = QCheckBox("Skip Redis FLUSHALL")
        row.addWidget(self.dry_run_cb)
        row.addSpacing(24)
        row.addWidget(self.no_redis_cb)
        row.addStretch(1)
        lay.addLayout(row)
        return box

    def _build_run_card(self) -> QGroupBox:
        box, lay = self._card("⑤ Run")

        warn = QLabel(
            "⚠  LIVE mode permanently deletes devices from the database. "
            "Always verify with dry-run first."
        )
        warn.setObjectName("warn")
        warn.setWordWrap(True)
        lay.addWidget(warn)

        btn_row = QHBoxLayout()
        self.run_btn = QPushButton("▶  Run")
        self.run_btn.setObjectName("primary")
        self.run_btn.clicked.connect(self._on_run)

        self.cancel_btn = QPushButton("■  Cancel")
        self.cancel_btn.setObjectName("danger")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._on_cancel)

        btn_row.addWidget(self.run_btn)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        lay.addWidget(self.status_label)

        return box

    def _build_log_card(self) -> QGroupBox:
        box, lay = self._card("Log")

        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("log")
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(200)
        lay.addWidget(self.log_view)

        btn_row = QHBoxLayout()
        btn_clear = QPushButton("Clear log")
        btn_clear.clicked.connect(self.log_view.clear)
        btn_row.addStretch(1)
        btn_row.addWidget(btn_clear)
        lay.addLayout(btn_row)

        return box

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _browse_csv(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select GV import CSV", "", "CSV files (*.csv);;All files (*)"
        )
        if path:
            self.csv_edit.setText(path)

    def _validate_inputs(self) -> str | None:
        """Returns an error message if inputs are incomplete, else None."""
        if not self.csv_edit.text().strip():
            return "Please select a CSV file."
        if not Path(self.csv_edit.text().strip()).exists():
            return f"CSV file not found:\n{self.csv_edit.text().strip()}"
        if not self.host_edit.text().strip():
            return "Host / IP is required."
        if not self.user_edit.text().strip():
            return "SSH User is required."
        if not self.pg_db_edit.text().strip():
            return "Postgres DB name is required."
        if not self.redis_edit.text().strip():
            return "Redis container name is required."
        try:
            port = int(self.port_edit.text().strip())
            if not (1 <= port <= 65535):
                raise ValueError
        except ValueError:
            return "SSH Port must be a number between 1 and 65535."
        return None

    def _build_config(self) -> core.Config:
        return core.Config(
            csv_path=self.csv_edit.text().strip(),
            mode=self.mode_combo.currentText(),
            host=self.host_edit.text().strip(),
            ssh_user=self.user_edit.text().strip(),
            ssh_port=int(self.port_edit.text().strip()),
            ssh_password=self.pw_edit.text() or None,
            ssh_key=None,
            pg_container=self.pg_container_edit.text().strip(),
            pg_db=self.pg_db_edit.text().strip(),
            pg_user=self.pg_user_edit.text().strip(),
            redis_container=self.redis_edit.text().strip(),
            dry_run=self.dry_run_cb.isChecked(),
            assume_yes=True,   # GUI always confirms via the warning label
            no_redis=self.no_redis_cb.isChecked(),
        )

    def _on_run(self):
        err = self._validate_inputs()
        if err:
            QMessageBox.warning(self, "Missing input", err)
            return

        if not self.dry_run_cb.isChecked():
            reply = QMessageBox.warning(
                self,
                "Live mode — are you sure?",
                "LIVE mode will permanently delete devices from the database.\n\n"
                "Type JA in the box below to confirm.",
                QMessageBox.Ok | QMessageBox.Cancel,
            )
            if reply != QMessageBox.Ok:
                return

        self.log_view.clear()
        self._set_running(True)
        self.status_label.setText("")

        cfg = self._build_config()
        self._worker = GvWorker(cfg)
        self._worker.signals.log_line.connect(self._on_log_line)
        self._worker.signals.finished.connect(self._on_finished)
        self._worker.start()

    def _on_cancel(self):
        if self._worker and self._worker.isRunning():
            self._worker.terminate()
            self._append_log("WARN", "Cancelled by user.")
            self._set_running(False)

    def _on_log_line(self, level: str, message: str):
        self._append_log(level, message)

    def _on_finished(self, rc: int):
        self._set_running(False)
        if rc == 0:
            self.status_label.setStyleSheet(f"color: {OK_GREEN}; font-weight: bold;")
            if self.dry_run_cb.isChecked():
                self.status_label.setText("✓  Dry-run complete — review the log above.")
            else:
                self.status_label.setText("✓  Done. Devices removed + cache cleared. Re-import CSV via GV Web UI.")
        elif rc == 1:
            self.status_label.setStyleSheet(f"color: {WARN_ORANGE}; font-weight: bold;")
            self.status_label.setText("Aborted.")
        elif rc == 2:
            self.status_label.setStyleSheet(f"color: {ERR_RED}; font-weight: bold;")
            self.status_label.setText("✗  CSV error — see log.")
        elif rc == 3:
            self.status_label.setStyleSheet(f"color: {ERR_RED}; font-weight: bold;")
            self.status_label.setText("✗  Server/DB error — see log.")
        else:
            self.status_label.setStyleSheet(f"color: {ERR_RED}; font-weight: bold;")
            self.status_label.setText(f"✗  Unexpected error (rc={rc}) — see log.")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_running(self, running: bool):
        self.run_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        for widget in (
            self.csv_edit, self.host_edit, self.port_edit, self.user_edit,
            self.pw_edit, self.pg_container_edit, self.pg_db_edit,
            self.pg_user_edit, self.redis_edit, self.mode_combo,
            self.dry_run_cb, self.no_redis_cb,
        ):
            widget.setEnabled(not running)

    def _append_log(self, level: str, message: str):
        color = LOG_COLORS.get(level, "#d4d4d4")
        prefix = f"[{level:<5}]"
        html = (
            f'<span style="color:{color};">'
            f'<span style="color:#5a7fa0;">{prefix}</span> '
            f'{_escape(message)}'
            f'</span>'
        )
        self.log_view.appendHtml(html)
        self.log_view.moveCursor(QTextCursor.End)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(_stylesheet())
    window = GvToolWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
