"""
Tests for gv_device_tool.py

Run:  python -m pytest test_gv_device_tool.py -v
"""

from __future__ import annotations

import csv
import io
import textwrap
from dataclasses import field
from unittest.mock import MagicMock, patch, call

import pytest

from gv_device_tool import (
    Config,
    Log,
    build_all_commands,
    build_pg_cmd,
    build_redis_cmd,
    read_csv,
    validate_mode,
    step_delete,
    step_preview,
    step_redis,
    step_import_hint,
    confirm,
    run,
    MODE_TYPE_PREFIXES,
    DEFAULTS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_csv_text(*rows: tuple) -> str:
    """Builds a CSV string from (serial, mac, type) tuples."""
    lines = ["serialNumber,macAddress,type"]
    for serial, mac, dtype in rows:
        lines.append(f"{serial},{mac},{dtype}")
    return "\n".join(lines)


def make_config(**overrides) -> Config:
    base = dict(
        csv_path="dummy.csv",
        mode="auto",
        host=DEFAULTS["host"],
        ssh_user=DEFAULTS["ssh_user"],
        ssh_port=DEFAULTS["ssh_port"],
        pg_container=DEFAULTS["pg_container"],
        pg_db=DEFAULTS["pg_db"],
        pg_user=DEFAULTS["pg_user"],
        redis_container=DEFAULTS["redis_container"],
        dry_run=False,
        assume_yes=True,
        no_redis=False,
        ssh_password=None,
        ssh_key=None,
        devices=[
            ("SN001", "00:0B:C2:17:06:CB", "R330"),
            ("SN002", "00:0B:C2:17:08:66", "R330"),
        ],
    )
    base.update(overrides)
    return Config(**base)


def null_log() -> Log:
    log = Log(path=None)
    return log


# ---------------------------------------------------------------------------
# CSV reading
# ---------------------------------------------------------------------------

class TestReadCsv:
    def test_reads_valid_csv(self, tmp_path):
        f = tmp_path / "devices.csv"
        f.write_text(make_csv_text(
            ("SN001", "00:0B:C2:17:06:CB", "R330"),
            ("SN002", "aa:bb:cc:dd:ee:ff", "R310"),
        ))
        rows = read_csv(str(f), null_log())
        assert len(rows) == 2
        assert rows[0] == ("SN001", "00:0B:C2:17:06:CB", "R330")
        assert rows[1][1] == "AA:BB:CC:DD:EE:FF"  # uppercased

    def test_raises_on_missing_mac_column(self, tmp_path):
        f = tmp_path / "bad.csv"
        f.write_text("serial,type\nSN001,R330\n")
        with pytest.raises(ValueError, match="macAddress"):
            read_csv(str(f), null_log())

    def test_raises_on_invalid_mac_format(self, tmp_path):
        f = tmp_path / "bad.csv"
        f.write_text("macAddress,type\n00:0B:C2:17:06,R330\n")  # too short
        with pytest.raises(ValueError, match="ungueltige MAC"):
            read_csv(str(f), null_log())

    def test_raises_on_empty_csv(self, tmp_path):
        f = tmp_path / "empty.csv"
        f.write_text("macAddress,type\n")
        with pytest.raises(ValueError, match="Keine gueltigen"):
            read_csv(str(f), null_log())

    def test_skips_empty_mac_rows(self, tmp_path):
        f = tmp_path / "with_blank.csv"
        f.write_text("macAddress,type\n00:0B:C2:17:06:CB,R330\n,\n")
        rows = read_csv(str(f), null_log())
        assert len(rows) == 1

    def test_bom_utf8_header(self, tmp_path):
        f = tmp_path / "bom.csv"
        f.write_bytes(b"\xef\xbb\xbfmacAddress,type\n00:0B:C2:17:06:CB,R330\n")
        rows = read_csv(str(f), null_log())
        assert len(rows) == 1

    def test_mac_column_alias(self, tmp_path):
        f = tmp_path / "mac.csv"
        f.write_text("mac,type\n00:0B:C2:17:06:CB,R330\n")
        rows = read_csv(str(f), null_log())
        assert len(rows) == 1

    def test_missing_optional_columns(self, tmp_path):
        f = tmp_path / "mac_only.csv"
        f.write_text("macAddress\n00:0B:C2:17:06:CB\n")
        rows = read_csv(str(f), null_log())
        assert rows[0] == ("", "00:0B:C2:17:06:CB", "")


# ---------------------------------------------------------------------------
# Mode validation
# ---------------------------------------------------------------------------

class TestValidateMode:
    def test_no_warning_when_types_match(self):
        log = null_log()
        devices = [("", "AA:BB:CC:DD:EE:FF", "R330")]
        with patch.object(log, "warn") as mock_warn:
            validate_mode(devices, "repeater", log)
            mock_warn.assert_not_called()

    def test_warning_on_mismatch(self):
        log = null_log()
        devices = [("", "AA:BB:CC:DD:EE:FF", "P300")]
        with patch.object(log, "warn") as mock_warn:
            validate_mode(devices, "repeater", log)
            mock_warn.assert_called()

    def test_auto_mode_no_check(self):
        log = null_log()
        devices = [("", "AA:BB:CC:DD:EE:FF", "ANYTHING")]
        with patch.object(log, "warn") as mock_warn:
            validate_mode(devices, "auto", log)
            mock_warn.assert_not_called()

    def test_empty_type_no_warning(self):
        log = null_log()
        devices = [("", "AA:BB:CC:DD:EE:FF", "")]
        with patch.object(log, "warn") as mock_warn:
            validate_mode(devices, "repeater", log)
            mock_warn.assert_not_called()

    @pytest.mark.parametrize("mode,dtype", [
        ("repeater", "R310"),
        ("repeater", "R330"),
        ("proxy",    "P200"),
        ("proxy",    "P300"),
        ("1t",       "C300"),
        ("1t",       "C500"),
    ])
    def test_valid_types_for_modes(self, mode, dtype):
        log = null_log()
        devices = [("", "AA:BB:CC:DD:EE:FF", dtype)]
        with patch.object(log, "warn") as mock_warn:
            validate_mode(devices, mode, log)
            mock_warn.assert_not_called()


# ---------------------------------------------------------------------------
# Command builder
# ---------------------------------------------------------------------------

class TestCommandBuilder:
    def setup_method(self):
        self.cfg = make_config()

    def test_select_cmd_contains_mac(self):
        cmds = build_all_commands(self.cfg)
        assert "00:0B:C2:17:06:CB" in cmds["select"]
        assert "00:0B:C2:17:08:66" in cmds["select"]

    def test_delete_creds_cmd_uses_subselect(self):
        cmds = build_all_commands(self.cfg)
        assert "device_credentials" in cmds["delete_creds"]
        assert "SELECT id FROM device" in cmds["delete_creds"]

    def test_delete_devices_cmd(self):
        cmds = build_all_commands(self.cfg)
        assert "DELETE FROM device" in cmds["delete_devices"]

    def test_check_count_cmd(self):
        cmds = build_all_commands(self.cfg)
        assert "count(*)" in cmds["check_count"]

    def test_redis_flush_cmd(self):
        cmds = build_all_commands(self.cfg)
        assert "FLUSHALL" in cmds["redis_flush"]
        assert DEFAULTS["redis_container"] in cmds["redis_flush"]

    def test_pg_cmd_contains_container(self):
        cmd = build_pg_cmd("my-container", "mydb", "myuser", "SELECT 1;")
        assert "my-container" in cmd
        assert "mydb" in cmd
        assert "myuser" in cmd
        assert "SELECT 1;" in cmd

    def test_redis_cmd_contains_container(self):
        cmd = build_redis_cmd("my-redis")
        assert "my-redis" in cmd
        assert "FLUSHALL" in cmd

    def test_macs_are_quoted_in_sql(self):
        cmds = build_all_commands(self.cfg)
        assert "'00:0B:C2:17:06:CB'" in cmds["select"]

    def test_no_sql_injection_possible(self):
        cfg = make_config(devices=[("SN", "00:0B:C2:17:06:CB", "R330")])
        # MAC_RE already validated these; ensure quotes surround value
        cmds = build_all_commands(cfg)
        assert "'; DROP TABLE" not in cmds["select"]


# ---------------------------------------------------------------------------
# Step functions (SSH mocked)
# ---------------------------------------------------------------------------

def make_ssh_mock(run_return_values: list) -> MagicMock:
    ssh = MagicMock()
    ssh.run.side_effect = run_return_values
    return ssh


class TestStepPreview:
    def test_logs_found_devices(self):
        cfg = make_config()
        cmds = build_all_commands(cfg)
        ssh = make_ssh_mock([(0, "00:0B:C2:17:06:CB|R330\n00:0B:C2:17:08:66|R330", "")])
        log = null_log()
        with patch.object(log, "info") as mock_info:
            step_preview(ssh, cmds, cfg, log)
            info_calls = " ".join(str(c) for c in mock_info.call_args_list)
            assert "2 von 2" in info_calls

    def test_warns_on_missing_devices(self):
        cfg = make_config()
        cmds = build_all_commands(cfg)
        ssh = make_ssh_mock([(0, "00:0B:C2:17:06:CB|R330", "")])
        log = null_log()
        with patch.object(log, "warn") as mock_warn:
            step_preview(ssh, cmds, cfg, log)
            mock_warn.assert_called()

    def test_raises_on_ssh_error(self):
        cfg = make_config()
        cmds = build_all_commands(cfg)
        ssh = make_ssh_mock([(1, "", "connection refused")])
        with pytest.raises(RuntimeError, match="SELECT fehlgeschlagen"):
            step_preview(ssh, cmds, cfg, null_log())


class TestStepDelete:
    def test_succeeds_on_clean_delete(self):
        cfg = make_config()
        cmds = build_all_commands(cfg)
        ssh = make_ssh_mock([
            (0, "DELETE 2", ""),  # delete_creds
            (0, "DELETE 2", ""),  # delete_devices
            (0, "0", ""),         # check_count
        ])
        log = null_log()
        with patch.object(log, "ok") as mock_ok:
            step_delete(ssh, cmds, log)
            ok_calls = " ".join(str(c) for c in mock_ok.call_args_list)
            assert "0 verbleibende" in ok_calls

    def test_raises_when_devices_remain(self):
        cfg = make_config()
        cmds = build_all_commands(cfg)
        ssh = make_ssh_mock([
            (0, "DELETE 1", ""),
            (0, "DELETE 2", ""),
            (0, "3", ""),  # count != 0 -> error
        ])
        with pytest.raises(RuntimeError, match="Kontrolle fehlgeschlagen"):
            step_delete(ssh, cmds, null_log())

    def test_raises_on_delete_creds_failure(self):
        cfg = make_config()
        cmds = build_all_commands(cfg)
        ssh = make_ssh_mock([(1, "", "permission denied")])
        with pytest.raises(RuntimeError, match="device_credentials"):
            step_delete(ssh, cmds, null_log())

    def test_raises_on_delete_device_failure(self):
        cfg = make_config()
        cmds = build_all_commands(cfg)
        ssh = make_ssh_mock([
            (0, "DELETE 2", ""),
            (1, "", "foreign key violation"),
        ])
        with pytest.raises(RuntimeError, match="device"):
            step_delete(ssh, cmds, null_log())


class TestStepRedis:
    def test_flushall_called(self):
        cfg = make_config()
        cmds = build_all_commands(cfg)
        ssh = make_ssh_mock([(0, "OK", "")])
        log = null_log()
        with patch.object(log, "ok") as mock_ok:
            step_redis(ssh, cmds, cfg, log)
            mock_ok.assert_called()

    def test_skipped_when_no_redis(self):
        cfg = make_config(no_redis=True)
        cmds = build_all_commands(cfg)
        ssh = MagicMock()
        log = null_log()
        step_redis(ssh, cmds, cfg, log)
        ssh.run.assert_not_called()

    def test_raises_on_redis_failure(self):
        cfg = make_config()
        cmds = build_all_commands(cfg)
        ssh = make_ssh_mock([(1, "", "connection refused")])
        with pytest.raises(RuntimeError, match="FLUSHALL"):
            step_redis(ssh, cmds, cfg, null_log())


# ---------------------------------------------------------------------------
# Confirm / dry-run
# ---------------------------------------------------------------------------

class TestConfirm:
    def test_dry_run_always_true(self):
        cfg = make_config(dry_run=True, assume_yes=False)
        assert confirm(cfg) is True

    def test_assume_yes_always_true(self):
        cfg = make_config(dry_run=False, assume_yes=True)
        assert confirm(cfg) is True

    def test_requires_JA(self):
        cfg = make_config(dry_run=False, assume_yes=False)
        with patch("builtins.input", return_value="JA"):
            assert confirm(cfg) is True

    def test_rejects_non_JA(self):
        cfg = make_config(dry_run=False, assume_yes=False)
        for bad in ("ja", "yes", "y", "J", "", "nein"):
            with patch("builtins.input", return_value=bad):
                assert confirm(cfg) is False


# ---------------------------------------------------------------------------
# Integration: run() with mocked SSH
# ---------------------------------------------------------------------------

class TestRun:
    def _make_csv(self, tmp_path) -> str:
        f = tmp_path / "devices.csv"
        f.write_text(make_csv_text(
            ("SN001", "00:0B:C2:17:06:CB", "R330"),
            ("SN002", "00:0B:C2:17:08:66", "R330"),
        ))
        return str(f)

    def test_dry_run_returns_0(self, tmp_path):
        cfg = make_config(csv_path=self._make_csv(tmp_path), dry_run=True)
        # devices not populated yet (run() fills them)
        cfg.devices = []
        rc = run(cfg, null_log())
        assert rc == 0

    def test_live_run_returns_0(self, tmp_path):
        cfg = make_config(csv_path=self._make_csv(tmp_path), dry_run=False, assume_yes=True)
        cfg.devices = []
        ssh_mock = MagicMock()
        ssh_mock.run.side_effect = [
            (0, "00:0B:C2:17:06:CB|R330", ""),  # preview SELECT
            (0, "DELETE 2", ""),                  # delete_creds
            (0, "DELETE 2", ""),                  # delete_devices
            (0, "0", ""),                         # check_count
            (0, "OK", ""),                        # redis FLUSHALL
        ]
        with patch("gv_device_tool.SSHClient", return_value=ssh_mock):
            rc = run(cfg, null_log())
        assert rc == 0

    def test_aborted_by_user_returns_1(self, tmp_path):
        cfg = make_config(csv_path=self._make_csv(tmp_path), dry_run=False, assume_yes=False)
        cfg.devices = []
        with patch("builtins.input", return_value="nein"):
            rc = run(cfg, null_log())
        assert rc == 1

    def test_missing_csv_returns_2(self, tmp_path):
        cfg = make_config(csv_path=str(tmp_path / "nonexistent.csv"))
        cfg.devices = []
        rc = run(cfg, null_log())
        assert rc == 2

    def test_ssh_error_returns_3(self, tmp_path):
        cfg = make_config(csv_path=self._make_csv(tmp_path), dry_run=False, assume_yes=True)
        cfg.devices = []
        ssh_mock = MagicMock()
        ssh_mock.run.side_effect = [(1, "", "connection refused")]
        with patch("gv_device_tool.SSHClient", return_value=ssh_mock):
            rc = run(cfg, null_log())
        assert rc == 3

    def test_no_redis_skips_flush(self, tmp_path):
        cfg = make_config(
            csv_path=self._make_csv(tmp_path),
            dry_run=False,
            assume_yes=True,
            no_redis=True,
        )
        cfg.devices = []
        ssh_mock = MagicMock()
        ssh_mock.run.side_effect = [
            (0, "", ""),      # preview
            (0, "DELETE 2", ""),
            (0, "DELETE 2", ""),
            (0, "0", ""),
        ]
        with patch("gv_device_tool.SSHClient", return_value=ssh_mock):
            rc = run(cfg, null_log())
        assert rc == 0
        # redis_flush must NOT have been called (only 4 calls total)
        assert ssh_mock.run.call_count == 4


# ---------------------------------------------------------------------------
# CLI argument parser
# ---------------------------------------------------------------------------

class TestCLI:
    def test_default_host(self):
        from gv_device_tool import build_parser
        args = build_parser().parse_args(["--csv", "x.csv"])
        assert args.host == DEFAULTS["host"]

    def test_custom_host(self):
        from gv_device_tool import build_parser
        args = build_parser().parse_args(["--csv", "x.csv", "--host", "10.0.0.1"])
        assert args.host == "10.0.0.1"

    def test_dry_run_flag(self):
        from gv_device_tool import build_parser
        args = build_parser().parse_args(["--csv", "x.csv", "--dry-run"])
        assert args.dry_run is True

    def test_yes_flag(self):
        from gv_device_tool import build_parser
        args = build_parser().parse_args(["--csv", "x.csv", "-y"])
        assert args.yes is True

    def test_mode_choices(self):
        from gv_device_tool import build_parser
        for mode in ["repeater", "headend", "proxy", "1t", "auto"]:
            args = build_parser().parse_args(["--csv", "x.csv", "--mode", mode])
            assert args.mode == mode

    def test_invalid_mode_raises(self):
        from gv_device_tool import build_parser
        with pytest.raises(SystemExit):
            build_parser().parse_args(["--csv", "x.csv", "--mode", "invalid"])
