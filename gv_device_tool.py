#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GridValue (GV) Device-Korrektur-Tool

Entfernt falsch importierte Geraete aus GV (Postgres + Redis-Cache) und
gibt einen Hinweis zum anschliessenden Re-Import ueber die GV-Web-UI.

Workflow (laut Jakub-Anleitung):
    1. CSV einlesen -> MACs + Types validieren
    2. SSH zum GV-Server
    3. docker exec -> Postgres: SELECT (Vorschau) -> DELETE device_credentials
                               -> DELETE device -> Kontrolle count=0
    4. docker exec -> Redis: FLUSHALL
    5. Hinweis: CSV per GV-Web-UI (Multi-Device-Import) neu einlesen

Unterstuetzte Modi: repeater | headend | proxy | 1t | auto

Abhaengigkeit:  pip install paramiko
.exe bauen:     pip install pyinstaller
                pyinstaller --onefile --name gv_device_tool gv_device_tool.py
"""

from __future__ import annotations

import argparse
import csv
import getpass
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Defaults — only generic / non-sensitive values hardcoded
# ---------------------------------------------------------------------------

DEFAULTS = {
    "ssh_port": 22,
    "pg_container": "deployment-postgres-1",
    "pg_user": "postgres",
}

# Erwartete Type-Praefixe je Modus (nur Warnung, kein Abbruch)
MODE_TYPE_PREFIXES: dict[str, Tuple[str, ...]] = {
    "repeater": ("R",),
    "headend":  ("H", "BPL", "HE"),
    "proxy":    ("P",),
    "1t":       ("C",),
    "auto":     (),
}

MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")

Device = Tuple[str, str, str]  # (serial, mac, type)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

class Log:
    def __init__(self, path: Optional[str] = None):
        self._fh = open(path, "a", encoding="utf-8") if path else None

    def _write(self, level: str, msg: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {level:<5} {msg}"
        print(line, flush=True)
        if self._fh:
            self._fh.write(line + "\n")
            self._fh.flush()

    def info(self, m: str) -> None:  self._write("INFO", m)
    def warn(self, m: str) -> None:  self._write("WARN", m)
    def error(self, m: str) -> None: self._write("ERROR", m)
    def ok(self, m: str) -> None:    self._write("OK", m)

    def close(self) -> None:
        if self._fh:
            self._fh.close()


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    csv_path: str
    mode: str
    host: str
    ssh_user: str
    ssh_port: int
    pg_container: str
    pg_db: str
    pg_user: str
    redis_container: str
    dry_run: bool
    assume_yes: bool
    no_redis: bool
    ssh_password: Optional[str] = None
    ssh_key: Optional[str] = None
    devices: List[Device] = field(default_factory=list)


# ---------------------------------------------------------------------------
# CSV einlesen
# ---------------------------------------------------------------------------

def read_csv(path: str, log: Log) -> List[Device]:
    """
    Liest die GV-Import-CSV.
    Pflicht-Spalte: macAddress
    Optional:       serialNumber, type
    """
    rows: List[Device] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError("CSV ist leer oder hat keine Kopfzeile.")
        cols = {c.strip().lower(): c for c in reader.fieldnames}
        mac_col    = cols.get("macaddress") or cols.get("mac")
        serial_col = cols.get("serialnumber") or cols.get("serial")
        type_col   = cols.get("type")
        if not mac_col:
            raise ValueError(
                f"Spalte 'macAddress' nicht gefunden. Vorhanden: {list(reader.fieldnames)}"
            )
        for i, row in enumerate(reader, start=2):
            mac = (row.get(mac_col) or "").strip()
            if not mac:
                continue
            serial = (row.get(serial_col) or "").strip() if serial_col else ""
            dtype  = (row.get(type_col)   or "").strip() if type_col   else ""
            if not MAC_RE.match(mac):
                raise ValueError(
                    f"Zeile {i}: ungueltige MAC '{mac}'. "
                    "Erwartet: AA:BB:CC:DD:EE:FF"
                )
            rows.append((serial, mac.upper(), dtype))
    if not rows:
        raise ValueError("Keine gueltigen Geraete in der CSV gefunden.")
    log.info(f"CSV gelesen: {len(rows)} Geraete aus '{path}'.")
    return rows


def validate_mode(devices: List[Device], mode: str, log: Log) -> None:
    prefixes = MODE_TYPE_PREFIXES.get(mode, ())
    if not prefixes:
        return
    mismatched = [
        (mac, dtype) for _, mac, dtype in devices
        if dtype and not any(dtype.upper().startswith(p.upper()) for p in prefixes)
    ]
    if not mismatched:
        return
    log.warn(
        f"{len(mismatched)} Geraet(e) passen nicht zum Modus '{mode}' "
        f"(erwartete Type-Praefixe: {', '.join(prefixes)}):"
    )
    for mac, dtype in mismatched[:10]:
        log.warn(f"    {mac}  type={dtype or '(leer)'}")
    if len(mismatched) > 10:
        log.warn(f"    ... und {len(mismatched) - 10} weitere.")


# ---------------------------------------------------------------------------
# SQL / Kommando-Builder
# ---------------------------------------------------------------------------

def _mac_in_list(devices: List[Device]) -> str:
    """SQL-IN-Liste. MACs sind durch MAC_RE validiert -> kein Injection-Risiko."""
    return ", ".join(f"'{mac}'" for _, mac, _ in devices)


def build_pg_cmd(container: str, db: str, user: str, sql: str) -> str:
    sql_flat = " ".join(sql.split())
    return f'docker exec {container} psql -U {user} -d {db} -t -A -c "{sql_flat}"'


def build_redis_cmd(container: str) -> str:
    return f"docker exec {container} redis-cli FLUSHALL"


def build_all_commands(cfg: Config) -> dict[str, str]:
    """Gibt alle Befehle als Dictionary zurueck (fuer --dry-run + Tests)."""
    in_list = _mac_in_list(cfg.devices)
    return {
        "select": build_pg_cmd(
            cfg.pg_container, cfg.pg_db, cfg.pg_user,
            f"SELECT name, type FROM device WHERE name IN ({in_list}) ORDER BY name;"
        ),
        "delete_creds": build_pg_cmd(
            cfg.pg_container, cfg.pg_db, cfg.pg_user,
            f"DELETE FROM device_credentials WHERE device_id IN "
            f"(SELECT id FROM device WHERE name IN ({in_list}));"
        ),
        "delete_devices": build_pg_cmd(
            cfg.pg_container, cfg.pg_db, cfg.pg_user,
            f"DELETE FROM device WHERE name IN ({in_list});"
        ),
        "check_count": build_pg_cmd(
            cfg.pg_container, cfg.pg_db, cfg.pg_user,
            f"SELECT count(*) FROM device WHERE name IN ({in_list});"
        ),
        "redis_flush": build_redis_cmd(cfg.redis_container),
    }


# ---------------------------------------------------------------------------
# SSH-Client
# ---------------------------------------------------------------------------

class SSHClient:
    def __init__(self, cfg: Config, log: Log):
        import paramiko  # lazy import: --help laeuft ohne Installation
        self._log = log
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kw: dict = dict(
            hostname=cfg.host,
            port=cfg.ssh_port,
            username=cfg.ssh_user,
            timeout=20,
            allow_agent=True,
            look_for_keys=True,
        )
        if cfg.ssh_key:
            kw["key_filename"] = cfg.ssh_key
        if cfg.ssh_password:
            kw["password"] = cfg.ssh_password
        log.info(f"SSH-Verbindung zu {cfg.ssh_user}@{cfg.host}:{cfg.ssh_port} ...")
        self._client.connect(**kw)
        log.ok("SSH verbunden.")

    def run(self, cmd: str) -> Tuple[int, str, str]:
        _, stdout, stderr = self._client.exec_command(cmd)
        rc  = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", "replace").strip()
        err = stderr.read().decode("utf-8", "replace").strip()
        return rc, out, err

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Container discovery
# ---------------------------------------------------------------------------

def list_containers(ssh: SSHClient) -> List[str]:
    """Returns names of all running Docker containers on the remote host."""
    rc, out, err = ssh.run("docker ps --format '{{.Names}}'")
    if rc != 0:
        return []
    return [l.strip() for l in out.splitlines() if l.strip()]


def find_similar_containers(name: str, containers: List[str]) -> List[str]:
    """Returns containers whose name contains any part of the target name (case-insensitive)."""
    import difflib
    # exact substring matches first
    parts = [p for p in name.replace("-", " ").replace("_", " ").split() if len(p) > 2]
    substring_hits = [c for c in containers if any(p.lower() in c.lower() for p in parts)]
    if substring_hits:
        return substring_hits
    # fall back to difflib close matches
    return difflib.get_close_matches(name, containers, n=5, cutoff=0.4)


def verify_container(ssh: SSHClient, name: str, label: str, log: Log) -> None:
    """
    Checks that a container is running. If not found, lists similar containers
    and raises so the user can correct the name via CLI flag.
    """
    rc, out, _ = ssh.run(f"docker inspect --format '{{{{.State.Running}}}}' {name}")
    if rc == 0 and out.strip() == "true":
        return  # all good

    log.warn(f"Container '{name}' ({label}) nicht gefunden oder nicht aktiv.")
    containers = list_containers(ssh)
    if not containers:
        log.warn("    Keine laufenden Docker-Container gefunden (docker ps fehlgeschlagen).")
        raise RuntimeError(f"Container '{name}' nicht erreichbar.")

    similar = find_similar_containers(name, containers)
    if similar:
        log.warn("    Aehnliche Container gefunden — meintest du einen davon?")
        for c in similar:
            log.warn(f"      {c}")
        log.warn(f"    Benutze z.B.: --{label.replace('_', '-')} {similar[0]}")
    else:
        log.warn(f"    Laufende Container: {', '.join(containers)}")
    raise RuntimeError(f"Container '{name}' nicht gefunden. Bitte --{label.replace('_', '-')} anpassen.")


# ---------------------------------------------------------------------------
# Ausfuehrungs-Schritte
# ---------------------------------------------------------------------------

def step_verify_containers(ssh: SSHClient, cfg: Config, log: Log) -> None:
    """Verifies both containers exist before touching the DB. Suggests alternatives if not."""
    log.info("Schritt 0/4 — Container-Pruefung:")
    verify_container(ssh, cfg.pg_container,    "pg-container",    log)
    log.ok(f"    Postgres: {cfg.pg_container} OK")
    if not cfg.no_redis:
        verify_container(ssh, cfg.redis_container, "redis-container", log)
        log.ok(f"    Redis:    {cfg.redis_container} OK")


def step_preview(ssh: SSHClient, cmds: dict, cfg: Config, log: Log) -> None:
    log.info(f"Schritt 1/4 — Bestandsaufnahme ({len(cfg.devices)} Geraete in CSV):")
    rc, out, err = ssh.run(cmds["select"])
    if rc != 0:
        raise RuntimeError(f"SELECT fehlgeschlagen (rc={rc}): {err or out}")
    found = [l for l in out.splitlines() if l.strip()]
    log.info(f"    In GV gefunden: {len(found)} von {len(cfg.devices)}.")
    for line in found:
        log.info(f"      {line}")
    missing = len(cfg.devices) - len(found)
    if missing > 0:
        log.warn(f"    {missing} Geraet(e) nicht in GV (werden beim Re-Import neu angelegt).")


def step_delete(ssh: SSHClient, cmds: dict, log: Log) -> None:
    log.info("Schritt 2/4 — Loeschen (device_credentials, dann device):")
    for label, key in (("device_credentials", "delete_creds"), ("device", "delete_devices")):
        rc, out, err = ssh.run(cmds[key])
        if rc != 0:
            raise RuntimeError(f"DELETE {label} fehlgeschlagen (rc={rc}): {err or out}")
        log.ok(f"    {label}: {out or 'DELETE'}")
    rc, out, err = ssh.run(cmds["check_count"])
    if rc != 0:
        raise RuntimeError(f"Kontrollabfrage fehlgeschlagen: {err or out}")
    if out.strip() != "0":
        raise RuntimeError(f"Kontrolle fehlgeschlagen: noch {out} Geraete in DB!")
    log.ok("    Kontrolle: 0 verbleibende Geraete — alles sauber geloescht.")


def step_redis(ssh: SSHClient, cmds: dict, cfg: Config, log: Log) -> None:
    if cfg.no_redis:
        log.warn("Schritt 3/4 — Redis FLUSHALL uebersprungen (--no-redis).")
        return
    log.info("Schritt 3/4 — Redis-Cache leeren:")
    rc, out, err = ssh.run(cmds["redis_flush"])
    if rc != 0:
        raise RuntimeError(f"Redis FLUSHALL fehlgeschlagen (rc={rc}): {err or out}")
    log.ok(f"    Redis: {out or 'OK'}")


def step_import_hint(cfg: Config, log: Log) -> None:
    log.info("Schritt 4/4 — Re-Import:")
    log.info("    CSV jetzt in der GV-Web-UI per Multi-Device-Import einlesen:")
    log.info(f"        {cfg.csv_path}")
    log.info("    (Import muss ueber GV erfolgen — nicht per SQL — damit alle")
    log.info("     GV-internen Strukturen korrekt angelegt werden.)")


# ---------------------------------------------------------------------------
# Benutzer-Bestaetigung
# ---------------------------------------------------------------------------

def print_summary(cfg: Config) -> None:
    w = 70
    print()
    print("=" * w)
    print(f"  GV Device-Korrektur-Tool | Modus: {cfg.mode}")
    print(f"  Server  : {cfg.ssh_user}@{cfg.host}:{cfg.ssh_port}")
    print(f"  Postgres: {cfg.pg_container} / DB={cfg.pg_db} / User={cfg.pg_user}")
    print(f"  Redis   : {cfg.redis_container}")
    print(f"  CSV     : {cfg.csv_path}")
    print(f"  Geraete : {len(cfg.devices)}")
    print(f"  Modus   : {'DRY-RUN (keine Aenderung)' if cfg.dry_run else 'LIVE — loescht Geraete in der DB!'}")
    print("=" * w)


def confirm(cfg: Config) -> bool:
    if cfg.dry_run or cfg.assume_yes:
        return True
    ans = input("  Fortfahren? [tippe 'JA' zum Bestaetigen]: ").strip()
    return ans == "JA"


# ---------------------------------------------------------------------------
# Dry-Run-Ausgabe
# ---------------------------------------------------------------------------

def print_dry_run(cmds: dict, cfg: Config, log: Log) -> None:
    log.info("DRY-RUN — diese Befehle wuerden auf dem Server laufen:")
    for key, cmd in cmds.items():
        if key == "redis_flush" and cfg.no_redis:
            log.info(f"# (uebersprungen: --no-redis)")
            log.info(f"# {cmd}")
        else:
            log.info(cmd)
    log.info("Danach: CSV per GV-Web-UI re-importieren.")


# ---------------------------------------------------------------------------
# Hauptablauf
# ---------------------------------------------------------------------------

def run(cfg: Config, log: Log) -> int:
    try:
        cfg.devices = read_csv(cfg.csv_path, log)
    except (FileNotFoundError, ValueError) as e:
        log.error(str(e))
        return 2

    validate_mode(cfg.devices, cfg.mode, log)
    print_summary(cfg)

    if not confirm(cfg):
        log.warn("Abgebrochen.")
        return 1

    cmds = build_all_commands(cfg)

    if cfg.dry_run:
        print_dry_run(cmds, cfg, log)
        return 0

    try:
        ssh = SSHClient(cfg, log)
        try:
            step_verify_containers(ssh, cfg, log)
            step_preview(ssh, cmds, cfg, log)
            step_delete(ssh, cmds, log)
            step_redis(ssh, cmds, cfg, log)
        finally:
            ssh.close()
    except RuntimeError as e:
        log.error(str(e))
        return 3
    except Exception as e:
        log.error(f"Unerwarteter Fehler: {e}")
        return 4

    step_import_hint(cfg, log)
    log.ok("Fertig. Geraete entfernt + Cache geleert. Bitte CSV in GV re-importieren.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="GV Device-Korrektur-Tool (repeater / headend / proxy / 1t).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--csv",             required=True,                        help="Path to the corrected GV import CSV.")
    p.add_argument("--mode",            default="auto",                       choices=list(MODE_TYPE_PREFIXES.keys()),
                                                                              help="Device category (validates type column).")
    # Connection — required, no hardcoded defaults
    p.add_argument("--host",            required=True,                        help="GV server hostname or IP.")
    p.add_argument("--ssh-user",        required=True,                        help="SSH username.")
    p.add_argument("--ssh-port",        default=DEFAULTS["ssh_port"],         type=int, help="SSH port.")
    p.add_argument("--ssh-key",         default=None,                         help="Path to private SSH key.")
    p.add_argument("--ssh-password",    default=None,                         help="SSH password (prefer --ask-password).")
    p.add_argument("--ask-password",    action="store_true",                  help="Prompt for SSH password interactively.")
    # Postgres
    p.add_argument("--pg-container",    default=DEFAULTS["pg_container"],     help="Postgres Docker container name.")
    p.add_argument("--pg-db",           required=True,                        help="Postgres database name.")
    p.add_argument("--pg-user",         default=DEFAULTS["pg_user"],          help="Postgres user.")
    # Redis
    p.add_argument("--redis-container", required=True,                        help="Redis Docker container name.")
    p.add_argument("--no-redis",        action="store_true",                  help="Skip Redis FLUSHALL.")
    # Behaviour
    p.add_argument("--dry-run",         action="store_true",                  help="Print commands only, change nothing.")
    p.add_argument("-y", "--yes",       action="store_true",                  help="Skip confirmation prompt.")
    p.add_argument("--log",             default=None,                         help="Path to log file.")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    log = Log(args.log)

    ssh_password = args.ssh_password
    if args.ask_password and not ssh_password:
        ssh_password = getpass.getpass("SSH-Passwort: ")

    cfg = Config(
        csv_path=args.csv,
        mode=args.mode,
        host=args.host,
        ssh_user=args.ssh_user,
        ssh_port=args.ssh_port,
        pg_container=args.pg_container,
        pg_db=args.pg_db,
        pg_user=args.pg_user,
        redis_container=args.redis_container,
        dry_run=args.dry_run,
        assume_yes=args.yes,
        no_redis=args.no_redis,
        ssh_password=ssh_password,
        ssh_key=args.ssh_key,
    )
    try:
        return run(cfg, log)
    except (FileNotFoundError, ValueError) as e:
        log.error(str(e))
        return 2
    except RuntimeError as e:
        log.error(str(e))
        return 3
    except Exception as e:
        log.error(f"Unerwarteter Fehler: {e}")
        return 4
    finally:
        log.close()


if __name__ == "__main__":
    sys.exit(main())
