#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GV Device Correction Tool — GUI  (Variant B: embedded PowerShell terminal)
===========================================================================
Spawns powershell.exe via QProcess. Auto-connects SSH, injects password,
sends docker commands one by one and shows live output. User can also type
manually at any time.

Dependencies:  pip install PySide6
               (paramiko NOT required for GUI — only for the CLI)
.exe:          pyinstaller --onefile --windowed --name gv_device_tool_gui gv_device_tool_gui.py
"""

from __future__ import annotations

import re
import sys
from enum import Enum, auto
from pathlib import Path

from PySide6.QtCore import Qt, QProcess, QTimer, Signal
from PySide6.QtGui import QTextCursor
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
    QPlainTextEdit,
    QCheckBox,
    QComboBox,
    QFrame,
    QMessageBox,
)

import gv_device_tool as core


# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
ACCENT      = "#0067b8"
ACCENT_DARK = "#004f8c"
OK_GREEN    = "#4ec94e"
WARN_ORANGE = "#e8a44a"
ERR_RED     = "#f47474"
INFO_COLOR  = "#d4d4d4"
CMD_COLOR   = "#7aabcc"    # echoed commands
META_COLOR  = "#888888"    # status / meta messages


# ---------------------------------------------------------------------------
# Stylesheet
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
    QLabel#muted  {{ color: #5b6068; background: transparent; }}
    QLabel#warn   {{ color: {WARN_ORANGE}; font-weight: bold; background: transparent; }}
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
    QLineEdit, QComboBox {{
        background: #ffffff;
        border: 1px solid #c4cbd3;
        border-radius: 4px;
        padding: 5px 8px;
        selection-background-color: {ACCENT};
        selection-color: #ffffff;
    }}
    QLineEdit:focus, QComboBox:focus {{ border: 1px solid {ACCENT}; }}
    QLineEdit:disabled, QComboBox:disabled {{ background: #f2f5f9; color: #8a9099; }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QPushButton {{
        background: #ffffff;
        color: #1b1b1b;
        border: 1px solid #c4cbd3;
        border-radius: 5px;
        padding: 6px 14px;
    }}
    QPushButton:hover   {{ background: #f1f6fb; border-color: {ACCENT}; }}
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
    QPushButton#primary:hover    {{ background: {ACCENT_DARK}; }}
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
    QPushButton#danger:hover    {{ background: #c43030; }}
    QPushButton#danger:disabled {{ background: #e8a0a0; color: #fff; }}
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
    """


# ---------------------------------------------------------------------------
# ANSI / pattern helpers
# ---------------------------------------------------------------------------
_ANSI_RE     = re.compile(r"\x1b\[[0-9;]*[mGKHFJABCDsuhl]")
_BASH_PROMPT = re.compile(r"[#\$]\s*$")          # remote shell prompt
_PW_PROMPT   = re.compile(r"password[^:]*:\s*$", re.IGNORECASE)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _html(text: str, color: str) -> str:
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<span style="color:{color};font-family:Consolas,monospace;">{safe}</span>'


# ---------------------------------------------------------------------------
# State machine for the SSH automation
# ---------------------------------------------------------------------------
class _State(Enum):
    IDLE             = auto()
    WAITING_PASSWORD = auto()   # sent ssh cmd, waiting for password prompt
    WAITING_PROMPT   = auto()   # password sent (or key auth), waiting for bash $
    RUNNING          = auto()   # sending commands one by one
    DONE             = auto()


# ---------------------------------------------------------------------------
# Embedded terminal panel
# ---------------------------------------------------------------------------
class TerminalPanel(QWidget):
    """
    Hosts a powershell.exe QProcess.
    Public API:
        start()                    — (re)start the PS process
        show_dry_run(cmds)         — print commands without SSH
        run_ssh(cfg, cmd_list)     — auto-run SSH workflow
        kill()                     — terminate process
    """

    all_done = Signal(bool)   # True = success / False = error

    def __init__(self, parent=None):
        super().__init__(parent)

        self._proc = QProcess(self)
        self._proc.setProcessChannelMode(QProcess.MergedChannels)
        self._proc.readyRead.connect(self._on_data)
        self._proc.finished.connect(self._on_finished)

        self._state: _State = _State.IDLE
        self._auto_pw: str | None = None
        self._cmd_queue: list[str] = []
        self._cmd_idx = 0

        # --- layout ---
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setMinimumHeight(240)
        self._view.setStyleSheet(
            "background:#0f0f14; color:#d4d4d4;"
            "font-family:Consolas,'Courier New',monospace; font-size:9pt;"
            "border-radius:6px; border:none; padding:6px;"
        )
        lay.addWidget(self._view)

        in_row = QHBoxLayout()
        in_row.setContentsMargins(0, 4, 0, 0)
        in_row.setSpacing(6)
        lbl = QLabel("PS›")
        lbl.setStyleSheet(
            f"color:{CMD_COLOR}; font-family:Consolas; font-size:9pt;"
            "background:transparent; font-weight:bold;"
        )
        self._inp = QLineEdit()
        self._inp.setPlaceholderText("type command and press Enter…")
        self._inp.setStyleSheet(
            "background:#1a1a22; color:#d4d4d4;"
            "font-family:Consolas,'Courier New',monospace; font-size:9pt;"
            "border:1px solid #333; border-radius:4px; padding:4px 8px;"
        )
        self._inp.returnPressed.connect(self._on_manual_input)
        in_row.addWidget(lbl)
        in_row.addWidget(self._inp)
        lay.addLayout(in_row)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """(Re)start powershell.exe."""
        if self._proc.state() != QProcess.NotRunning:
            self._proc.kill()
            self._proc.waitForFinished(2000)
        self._state = _State.IDLE
        self._proc.start("powershell.exe", ["-NoLogo", "-NoExit", "-Command", "-"])
        self._meta("PowerShell started.")

    def show_dry_run(self, cmds: dict) -> None:
        """Print the commands without connecting — dry-run mode."""
        self._meta("DRY-RUN — commands that would run on the server:")
        self._append_line("")
        for cmd in cmds.values():
            self._append_html(_html(cmd, INFO_COLOR))
            self._append_line("")
        self._meta("Re-import CSV via GV Web UI afterwards.")
        self.all_done.emit(True)

    def run_ssh(self, cfg: core.Config, cmd_list: list[str]) -> None:
        """
        Auto-run SSH workflow:
          ssh user@host → [password] → docker commands → exit
        """
        self._auto_pw = cfg.ssh_password or None
        self._cmd_queue = cmd_list
        self._cmd_idx = 0
        self._state = _State.WAITING_PASSWORD if self._auto_pw else _State.WAITING_PROMPT

        ssh_cmd = f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 {cfg.ssh_user}@{cfg.host}"
        self._echo_cmd(ssh_cmd)
        self._write(ssh_cmd)

    def kill(self) -> None:
        if self._proc.state() != QProcess.NotRunning:
            self._proc.kill()
        self._state = _State.DONE
        self._meta("Process killed.")

    def clear(self) -> None:
        self._view.clear()

    # ------------------------------------------------------------------
    # Internal — data handler (state machine)
    # ------------------------------------------------------------------

    def _on_data(self) -> None:
        raw  = bytes(self._proc.readAll()).decode("utf-8", "replace")
        text = _strip_ansi(raw).replace("\r\n", "\n").replace("\r", "\n")

        # Always show raw output
        self._view.moveCursor(QTextCursor.End)
        self._view.insertPlainText(text)
        self._view.moveCursor(QTextCursor.End)

        last_line = text.rstrip("\n").rsplit("\n", 1)[-1]

        if self._state == _State.WAITING_PASSWORD:
            if _PW_PROMPT.search(last_line):
                self._write(self._auto_pw or "")
                self._meta("[password sent]")
                self._state = _State.WAITING_PROMPT

        elif self._state == _State.WAITING_PROMPT:
            if _BASH_PROMPT.search(last_line):
                self._state = _State.RUNNING
                self._send_next()

        elif self._state == _State.RUNNING:
            if _BASH_PROMPT.search(last_line):
                self._send_next()

    def _send_next(self) -> None:
        if self._cmd_idx >= len(self._cmd_queue):
            self._state = _State.DONE
            self._write("exit")
            self._meta("All commands sent. SSH session closed.")
            self.all_done.emit(True)
            return
        cmd = self._cmd_queue[self._cmd_idx]
        self._cmd_idx += 1
        self._echo_cmd(cmd)
        self._write(cmd)

    def _on_finished(self, code: int, _status) -> None:
        self._meta(f"[process exited: {code}]")

    def _on_manual_input(self) -> None:
        text = self._inp.text().strip()
        self._inp.clear()
        if text:
            self._echo_cmd(text)
            self._write(text)

    # ------------------------------------------------------------------
    # Internal — helpers
    # ------------------------------------------------------------------

    def _write(self, text: str) -> None:
        self._proc.write((text + "\r\n").encode("utf-8"))

    def _append_html(self, html: str) -> None:
        self._view.moveCursor(QTextCursor.End)
        self._view.appendHtml(html)
        self._view.moveCursor(QTextCursor.End)

    def _append_line(self, text: str) -> None:
        self._view.moveCursor(QTextCursor.End)
        self._view.insertPlainText(text + "\n")
        self._view.moveCursor(QTextCursor.End)

    def _echo_cmd(self, cmd: str) -> None:
        self._append_html(_html(f"\n▶ {cmd}", CMD_COLOR))

    def _meta(self, msg: str) -> None:
        self._append_html(_html(f"\n# {msg}", META_COLOR))


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class GvToolWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("GV Device Correction Tool")
        self.resize(860, 820)
        self.setMinimumSize(720, 680)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(18, 16, 18, 16)
        body_lay.setSpacing(12)

        body_lay.addWidget(self._build_csv_card())
        body_lay.addWidget(self._build_connection_card())
        body_lay.addWidget(self._build_db_card())
        body_lay.addWidget(self._build_options_card())
        body_lay.addWidget(self._build_run_card())
        body_lay.addWidget(self._build_terminal_card())
        body_lay.addStretch(1)

        root.addWidget(body)

    # ------------------------------------------------------------------
    # UI builders
    # ------------------------------------------------------------------

    def _build_header(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("header")
        lay = QVBoxLayout(bar)
        lay.setContentsMargins(18, 12, 18, 12)
        lay.setSpacing(2)
        t = QLabel("GV Device Correction Tool")
        t.setObjectName("headerTitle")
        s = QLabel(
            "Remove incorrectly imported devices from GridValue "
            "(Postgres + Redis) and re-import via GV Web UI"
        )
        s.setObjectName("headerSub")
        s.setWordWrap(True)
        lay.addWidget(t)
        lay.addWidget(s)
        return bar

    def _card(self, title: str) -> tuple[QGroupBox, QVBoxLayout]:
        box = QGroupBox(title)
        box.setObjectName("card")
        lay = QVBoxLayout(box)
        lay.setSpacing(8)
        return box, lay

    def _build_csv_card(self) -> QGroupBox:
        box, lay = self._card("①  Input CSV")
        hint = QLabel("Select the corrected GV import CSV (columns: serialNumber, macAddress, type, …)")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        row = QHBoxLayout()
        _default = str(Path(__file__).parent / "Device_import_20260603_Hassfurt_62_R310.csv")
        self.csv_edit = QLineEdit(_default if Path(_default).exists() else "")
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
            "repeater / headend / proxy / 1t — warns if type doesn't match"
        )
        mode_row.addWidget(self.mode_combo)
        mode_row.addStretch(1)
        lay.addLayout(mode_row)
        return box

    def _build_connection_card(self) -> QGroupBox:
        box, lay = self._card("②  SSH Connection")
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        grid.addWidget(QLabel("Host / IP:"), 0, 0, Qt.AlignRight)
        self.host_edit = QLineEdit("172.31.2.2")
        grid.addWidget(self.host_edit, 0, 1)

        grid.addWidget(QLabel("Port:"), 0, 2, Qt.AlignRight)
        self.port_edit = QLineEdit("22")
        self.port_edit.setFixedWidth(60)
        grid.addWidget(self.port_edit, 0, 3)

        grid.addWidget(QLabel("SSH User:"), 1, 0, Qt.AlignRight)
        self.user_edit = QLineEdit("corinex")
        grid.addWidget(self.user_edit, 1, 1)

        grid.addWidget(QLabel("SSH Password:"), 1, 2, Qt.AlignRight)
        self.pw_edit = QLineEdit()
        self.pw_edit.setEchoMode(QLineEdit.Password)
        self.pw_edit.setPlaceholderText("leave blank for SSH key / agent")
        grid.addWidget(self.pw_edit, 1, 3)

        lay.addLayout(grid)
        return box

    def _build_db_card(self) -> QGroupBox:
        box, lay = self._card("③  Postgres / Redis")
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)

        grid.addWidget(QLabel("Postgres container:"), 0, 0, Qt.AlignRight)
        self.pg_container_edit = QLineEdit(core.DEFAULTS["pg_container"])
        grid.addWidget(self.pg_container_edit, 0, 1)

        grid.addWidget(QLabel("Postgres DB:"), 0, 2, Qt.AlignRight)
        self.pg_db_edit = QLineEdit("corinex")
        grid.addWidget(self.pg_db_edit, 0, 3)

        grid.addWidget(QLabel("Postgres user:"), 1, 0, Qt.AlignRight)
        self.pg_user_edit = QLineEdit(core.DEFAULTS["pg_user"])
        grid.addWidget(self.pg_user_edit, 1, 1)

        grid.addWidget(QLabel("Redis container:"), 1, 2, Qt.AlignRight)
        self.redis_edit = QLineEdit("deployment-redis-1")
        grid.addWidget(self.redis_edit, 1, 3)

        lay.addLayout(grid)
        return box

    def _build_options_card(self) -> QGroupBox:
        box, lay = self._card("④  Options")
        row = QHBoxLayout()
        self.dry_run_cb = QCheckBox("Dry-run (show commands only — no changes)")
        self.dry_run_cb.setChecked(True)
        self.no_redis_cb = QCheckBox("Skip Redis FLUSHALL")
        row.addWidget(self.dry_run_cb)
        row.addSpacing(24)
        row.addWidget(self.no_redis_cb)
        row.addStretch(1)
        lay.addLayout(row)
        return box

    def _build_run_card(self) -> QGroupBox:
        box, lay = self._card("⑤  Run")
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
        self.cancel_btn = QPushButton("■  Kill terminal")
        self.cancel_btn.setObjectName("danger")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(self.run_btn)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addStretch(1)
        lay.addLayout(btn_row)

        self.status_lbl = QLabel("")
        self.status_lbl.setWordWrap(True)
        lay.addWidget(self.status_lbl)
        return box

    def _build_terminal_card(self) -> QGroupBox:
        box, lay = self._card("Terminal")

        # --- Step buttons (send individual commands) ---
        step_lbl = QLabel("Send individual step:")
        step_lbl.setObjectName("muted")
        lay.addWidget(step_lbl)

        step_row = QHBoxLayout()
        step_row.setSpacing(6)

        self._step_btns: list[QPushButton] = []

        steps = [
            ("① SELECT",  "select"),
            ("② DEL creds", "delete_creds"),
            ("③ DEL device", "delete_devices"),
            ("④ Check",   "check_count"),
            ("⑤ Redis",   "redis_flush"),
        ]
        for label, key in steps:
            b = QPushButton(label)
            b.setToolTip(f"Send the '{key}' command to the terminal")
            b.clicked.connect(lambda _=False, k=key: self._send_step(k))
            step_row.addWidget(b)
            self._step_btns.append(b)

        step_row.addStretch(1)
        lay.addLayout(step_row)

        # --- Free command input ---
        free_row = QHBoxLayout()
        self._free_cmd = QLineEdit()
        self._free_cmd.setPlaceholderText("or type any command and press Send / Enter…")
        self._free_cmd.returnPressed.connect(self._send_free_cmd)
        btn_send = QPushButton("Send")
        btn_send.clicked.connect(self._send_free_cmd)
        free_row.addWidget(self._free_cmd)
        free_row.addWidget(btn_send)
        lay.addLayout(free_row)

        # --- Terminal widget ---
        self.terminal = TerminalPanel()
        self.terminal.all_done.connect(self._on_done)
        lay.addWidget(self.terminal)

        btn_row = QHBoxLayout()
        btn_clear = QPushButton("Clear")
        btn_clear.clicked.connect(self.terminal.clear)
        btn_start = QPushButton("Start PowerShell")
        btn_start.clicked.connect(self.terminal.start)
        btn_row.addStretch(1)
        btn_row.addWidget(btn_clear)
        btn_row.addWidget(btn_start)
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

    def _validate(self) -> str | None:
        if not self.csv_edit.text().strip():
            return "Please select a CSV file."
        if not Path(self.csv_edit.text().strip()).exists():
            return f"CSV not found:\n{self.csv_edit.text().strip()}"
        if not self.host_edit.text().strip():
            return "Host / IP is required."
        if not self.user_edit.text().strip():
            return "SSH User is required."
        if not self.pg_db_edit.text().strip():
            return "Postgres DB name is required."
        if not self.redis_edit.text().strip():
            return "Redis container name is required."
        try:
            p = int(self.port_edit.text().strip())
            if not 1 <= p <= 65535:
                raise ValueError
        except ValueError:
            return "SSH Port must be 1–65535."
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
            assume_yes=True,
            no_redis=self.no_redis_cb.isChecked(),
        )

    def _on_run(self):
        err = self._validate()
        if err:
            QMessageBox.warning(self, "Missing input", err)
            return

        if not self.dry_run_cb.isChecked():
            reply = QMessageBox.warning(
                self,
                "Live mode — are you sure?",
                "LIVE mode will permanently delete devices from the database.\n\nProceed?",
                QMessageBox.Ok | QMessageBox.Cancel,
            )
            if reply != QMessageBox.Ok:
                return

        cfg = self._build_config()

        # Read CSV to get MACs (needed to build SQL commands)
        try:
            cfg.devices = core.read_csv(cfg.csv_path, _NullLog())
        except (FileNotFoundError, ValueError) as e:
            QMessageBox.critical(self, "CSV error", str(e))
            return

        core.validate_mode(cfg.devices, cfg.mode, _NullLog())
        cmds = core.build_all_commands(cfg)

        self._set_running(True)
        self.status_lbl.setText("")
        self.terminal.clear()

        if cfg.dry_run:
            self.terminal.show_dry_run(cmds)
        else:
            # Build ordered command list (without check_count as separate step —
            # it runs as part of the same psql session via the last DELETE output)
            cmd_list = [
                cmds["select"],
                cmds["delete_creds"],
                cmds["delete_devices"],
                cmds["check_count"],
            ]
            if not cfg.no_redis:
                cmd_list.append(cmds["redis_flush"])

            if self.terminal._proc.state() == QProcess.NotRunning:
                self.terminal.start()
                # Give PS a moment to start before sending SSH
                QTimer.singleShot(800, lambda: self.terminal.run_ssh(cfg, cmd_list))
            else:
                self.terminal.run_ssh(cfg, cmd_list)

    def _on_cancel(self):
        self.terminal.kill()
        self._set_running(False)

    def _send_step(self, key: str):
        """Send a single named step command to the terminal."""
        err = self._validate()
        if err:
            QMessageBox.warning(self, "Missing input", err)
            return
        cfg = self._build_config()
        try:
            cfg.devices = core.read_csv(cfg.csv_path, _NullLog())
        except (FileNotFoundError, ValueError) as e:
            QMessageBox.critical(self, "CSV error", str(e))
            return
        cmds = core.build_all_commands(cfg)
        cmd = cmds.get(key, "")
        if cmd:
            self.terminal._echo_cmd(cmd)
            self.terminal._write(cmd)

    def _send_free_cmd(self):
        """Send whatever is typed in the free command input."""
        text = self._free_cmd.text().strip()
        if text:
            self._free_cmd.clear()
            self.terminal._echo_cmd(text)
            self.terminal._write(text)

    def _on_done(self, success: bool):
        self._set_running(False)
        if success:
            self.status_lbl.setStyleSheet(f"color:{OK_GREEN}; font-weight:bold;")
            if self.dry_run_cb.isChecked():
                self.status_lbl.setText("✓  Dry-run complete.")
            else:
                self.status_lbl.setText(
                    "✓  Done — devices removed + cache cleared. Re-import CSV via GV Web UI."
                )
        else:
            self.status_lbl.setStyleSheet(f"color:{ERR_RED}; font-weight:bold;")
            self.status_lbl.setText("✗  Error — see terminal output.")

    def _set_running(self, running: bool):
        self.run_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        for w in (
            self.csv_edit, self.host_edit, self.port_edit,
            self.user_edit, self.pw_edit, self.pg_container_edit,
            self.pg_db_edit, self.pg_user_edit, self.redis_edit,
            self.mode_combo, self.dry_run_cb, self.no_redis_cb,
        ):
            w.setEnabled(not running)


# ---------------------------------------------------------------------------
# Null log (used when we only need side-effects, not log output)
# ---------------------------------------------------------------------------
class _NullLog:
    def info(self, m): pass
    def warn(self, m): pass
    def error(self, m): pass
    def ok(self, m): pass
    def close(self): pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(_stylesheet())
    win = GvToolWindow()
    win.show()
    # Auto-start PowerShell so terminal is ready immediately
    win.terminal.start()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
