#!/usr/bin/env python3
"""
speicher.py — Ablage für Konten, Häuser, Zonen, Räume und Geräte.

SQLite, eine Datei, keine Serverinstallation. Für eine Anlage mit zehn Geräten
und einer Handvoll Konten ist das reichlich; die Datei lässt sich zudem einfach
sichern und mitnehmen.

Getrennt davon steht der **Livezustand** der Geräte: Betriebsmodus, Messwerte,
Alarme. Der wird nicht in die Datenbank geschrieben, sondern nur im Speicher
gehalten und vom Geräteserver bei jedem eintreffenden Statusrahmen aktualisiert.
Zwei Gründe: Er ist nach einem Neustart ohnehin binnen Sekunden wieder da, und
ein Statusrahmen alle paar Sekunden mal zehn Geräte würde die SD-Karte eines
Raspberry Pi unnötig beschreiben.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from hashlib import pbkdf2_hmac
from pathlib import Path
from typing import Dict, List, Optional

#: Iterationen für die Passwortableitung. Bewusst hoch genug, dass ein
#: Wörterbuchangriff auf die Datenbankdatei nicht in Minuten durchläuft.
PBKDF2_RUNDEN = 210_000


def passwort_hashen(passwort: str, salz: Optional[bytes] = None) -> str:
    salz = salz or secrets.token_bytes(16)
    ableitung = pbkdf2_hmac("sha256", passwort.encode(), salz, PBKDF2_RUNDEN)
    return f"pbkdf2_sha256${PBKDF2_RUNDEN}${salz.hex()}${ableitung.hex()}"


def passwort_pruefen(passwort: str, gespeichert: str) -> bool:
    try:
        verfahren, runden, salz_hex, erwartet = gespeichert.split("$")
        if verfahren != "pbkdf2_sha256":
            return False
        ableitung = pbkdf2_hmac("sha256", passwort.encode(),
                                bytes.fromhex(salz_hex), int(runden))
    except (ValueError, TypeError):
        return False
    # Zeitkonstanter Vergleich: verhindert, dass die Antwortzeit verrät,
    # wie viele Zeichen des Hashes übereinstimmen.
    return secrets.compare_digest(ableitung.hex(), erwartet)


@dataclass
class Livezustand:
    """Was der Geräteserver zuletzt von einem Gerät gehört hat."""
    seriennummer: str
    modus_code: int = 0
    stufe_code: int = 0
    feuchte_code: int = 1
    licht_code: int = 0
    temperatur: int = 0
    feuchte: int = 0
    luftguete_roh: int = 0
    feuchtealarm: bool = False
    filter_code: int = 0
    nachtalarm: bool = False
    rolle_code: int = 0
    letzter_modus_code: int = 0
    signalstaerke: Optional[int] = None
    radio_fw: Optional[str] = None
    micro_fw: Optional[str] = None
    radio_at_fw: Optional[str] = None
    gesehen: Optional[datetime] = None
    online: bool = False

    @property
    def veraltet(self) -> bool:
        """True, wenn seit fünf Minuten nichts mehr kam."""
        if self.gesehen is None:
            return True
        return (datetime.utcnow() - self.gesehen).total_seconds() > 300


SCHEMA = """
CREATE TABLE IF NOT EXISTS benutzer (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    passwort_hash TEXT NOT NULL,
    vorname       TEXT,
    nachname      TEXT,
    level         INTEGER NOT NULL DEFAULT 0,
    angelegt      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS haus (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    benutzer_id   INTEGER NOT NULL REFERENCES benutzer(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    adresse       TEXT,
    breite        REAL,
    laenge        REAL,
    zeitzone      INTEGER,
    iana_zeitzone TEXT
);
CREATE TABLE IF NOT EXISTS zone (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    haus_id  INTEGER NOT NULL REFERENCES haus(id) ON DELETE CASCADE,
    name     TEXT
);
CREATE TABLE IF NOT EXISTS raum (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    haus_id  INTEGER NOT NULL REFERENCES haus(id) ON DELETE CASCADE,
    zone_id  INTEGER REFERENCES zone(id) ON DELETE SET NULL,
    name     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS geraet (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    raum_id       INTEGER NOT NULL REFERENCES raum(id) ON DELETE CASCADE,
    seriennummer  TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL,
    typ           TEXT NOT NULL DEFAULT 'Diamond',
    subtyp        TEXT NOT NULL DEFAULT 'Version160',
    rolle         TEXT NOT NULL DEFAULT 'NotConfigured',
    zonen_index   INTEGER,
    installiert   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS zeitfenster (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    geraet_id    INTEGER NOT NULL REFERENCES geraet(id) ON DELETE CASCADE,
    wochentag    TEXT NOT NULL,
    beginn       TEXT NOT NULL,
    ende         TEXT NOT NULL,
    modus        TEXT NOT NULL,
    stufe        TEXT NOT NULL,
    feuchtestufe TEXT NOT NULL,
    lichtsensor  TEXT NOT NULL DEFAULT 'NotAvailable'
);
CREATE INDEX IF NOT EXISTS idx_haus_benutzer ON haus(benutzer_id);
CREATE INDEX IF NOT EXISTS idx_raum_haus     ON raum(haus_id);
CREATE INDEX IF NOT EXISTS idx_geraet_raum   ON geraet(raum_id);
CREATE INDEX IF NOT EXISTS idx_zeitf_geraet  ON zeitfenster(geraet_id);
"""


class Speicher:
    """Dünne Schicht über SQLite. Alle Zugriffe sind durch ein Lock serialisiert.

    Der Geräteserver läuft in einem asyncio-Loop, die REST-Schicht in
    Threadpool-Workern. SQLite verträgt beides, solange nicht zwei Schreiber
    gleichzeitig zugreifen — das Lock stellt genau das sicher.
    """

    def __init__(self, pfad: str = "ambientika.db"):
        self.pfad = pfad
        neu = pfad == ":memory:" or not Path(pfad).exists()
        self._lock = threading.RLock()
        self.db = sqlite3.connect(pfad, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.neu_angelegt = neu
        self.live: Dict[str, Livezustand] = {}

    # -- Benutzer -----------------------------------------------------------
    def benutzer_anlegen(self, username: str, passwort: str,
                         vorname: str = "", nachname: str = "") -> int:
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO benutzer (username, passwort_hash, vorname, nachname,"
                " angelegt) VALUES (?,?,?,?,?)",
                (username.lower().strip(), passwort_hashen(passwort), vorname,
                 nachname, datetime.utcnow().isoformat()))
            self.db.commit()
            return cur.lastrowid

    def benutzer_holen(self, username: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.db.execute(
                "SELECT * FROM benutzer WHERE username = ?",
                (username.lower().strip(),)).fetchone()

    def benutzer_pruefen(self, username: str, passwort: str) -> Optional[sqlite3.Row]:
        zeile = self.benutzer_holen(username)
        if zeile and passwort_pruefen(passwort, zeile["passwort_hash"]):
            return zeile
        return None

    def benutzer_anzahl(self) -> int:
        with self._lock:
            return self.db.execute("SELECT COUNT(*) c FROM benutzer").fetchone()["c"]

    # -- Haus ---------------------------------------------------------------
    def haus_anlegen(self, benutzer_id: int, name: str, adresse: Optional[str] = None,
                     breite: Optional[float] = None, laenge: Optional[float] = None,
                     zeitzone: Optional[int] = None,
                     iana: Optional[str] = None) -> int:
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO haus (benutzer_id, name, adresse, breite, laenge,"
                " zeitzone, iana_zeitzone) VALUES (?,?,?,?,?,?,?)",
                (benutzer_id, name, adresse, breite, laenge, zeitzone, iana))
            self.db.commit()
            return cur.lastrowid

    def haeuser(self, benutzer_id: int) -> List[sqlite3.Row]:
        with self._lock:
            return self.db.execute(
                "SELECT * FROM haus WHERE benutzer_id = ? ORDER BY id",
                (benutzer_id,)).fetchall()

    def haus(self, haus_id: int, benutzer_id: int) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.db.execute(
                "SELECT * FROM haus WHERE id = ? AND benutzer_id = ?",
                (haus_id, benutzer_id)).fetchone()

    def haus_umbenennen(self, haus_id: int, benutzer_id: int, name: str) -> bool:
        with self._lock:
            cur = self.db.execute(
                "UPDATE haus SET name = ? WHERE id = ? AND benutzer_id = ?",
                (name, haus_id, benutzer_id))
            self.db.commit()
            return cur.rowcount > 0

    def haus_zeitzone_setzen(self, haus_id: int, benutzer_id: int,
                             zeitzone: int) -> bool:
        with self._lock:
            cur = self.db.execute(
                "UPDATE haus SET zeitzone = ? WHERE id = ? AND benutzer_id = ?",
                (zeitzone, haus_id, benutzer_id))
            self.db.commit()
            return cur.rowcount > 0

    def haus_loeschen(self, haus_id: int, benutzer_id: int) -> bool:
        with self._lock:
            cur = self.db.execute(
                "DELETE FROM haus WHERE id = ? AND benutzer_id = ?",
                (haus_id, benutzer_id))
            self.db.commit()
            return cur.rowcount > 0

    # -- Zone und Raum ------------------------------------------------------
    def zone_anlegen(self, haus_id: int, name: str) -> int:
        with self._lock:
            cur = self.db.execute("INSERT INTO zone (haus_id, name) VALUES (?,?)",
                                  (haus_id, name))
            self.db.commit()
            return cur.lastrowid

    def zonen(self, haus_id: int) -> List[sqlite3.Row]:
        with self._lock:
            return self.db.execute(
                "SELECT * FROM zone WHERE haus_id = ? ORDER BY id",
                (haus_id,)).fetchall()

    def zone_umbenennen(self, zone_id: int, name: str) -> bool:
        with self._lock:
            cur = self.db.execute("UPDATE zone SET name = ? WHERE id = ?",
                                  (name, zone_id))
            self.db.commit()
            return cur.rowcount > 0

    def zone_loeschen(self, zone_id: int) -> bool:
        with self._lock:
            cur = self.db.execute("DELETE FROM zone WHERE id = ?", (zone_id,))
            self.db.commit()
            return cur.rowcount > 0

    def raum_anlegen(self, haus_id: int, name: str,
                     zone_id: Optional[int] = None) -> int:
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO raum (haus_id, zone_id, name) VALUES (?,?,?)",
                (haus_id, zone_id, name))
            self.db.commit()
            return cur.lastrowid

    def raeume(self, haus_id: int,
               zone_id: Optional[int] = None,
               nur_freie: bool = False) -> List[sqlite3.Row]:
        with self._lock:
            if nur_freie:
                return self.db.execute(
                    "SELECT * FROM raum WHERE haus_id = ? AND zone_id IS NULL"
                    " ORDER BY id", (haus_id,)).fetchall()
            if zone_id is not None:
                return self.db.execute(
                    "SELECT * FROM raum WHERE haus_id = ? AND zone_id = ?"
                    " ORDER BY id", (haus_id, zone_id)).fetchall()
            return self.db.execute(
                "SELECT * FROM raum WHERE haus_id = ? ORDER BY id",
                (haus_id,)).fetchall()

    def raum_zone_zuweisen(self, raum_id: int, zone_id: Optional[int]) -> None:
        with self._lock:
            self.db.execute("UPDATE raum SET zone_id = ? WHERE id = ?",
                            (zone_id, raum_id))
            self.db.commit()

    # -- Gerät --------------------------------------------------------------
    def geraet_anlegen(self, raum_id: int, seriennummer: str, name: str,
                       rolle: str = "NotConfigured",
                       zonen_index: Optional[int] = None) -> int:
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO geraet (raum_id, seriennummer, name, rolle,"
                " zonen_index, installiert) VALUES (?,?,?,?,?,?)",
                (raum_id, seriennummer.upper(), name, rolle, zonen_index,
                 datetime.utcnow().isoformat()))
            self.db.commit()
            return cur.lastrowid

    def geraete(self, haus_id: Optional[int] = None,
                raum_id: Optional[int] = None,
                zone_id: Optional[int] = None) -> List[sqlite3.Row]:
        with self._lock:
            if raum_id is not None:
                return self.db.execute(
                    "SELECT * FROM geraet WHERE raum_id = ? ORDER BY id",
                    (raum_id,)).fetchall()
            if zone_id is not None:
                return self.db.execute(
                    "SELECT g.* FROM geraet g JOIN raum r ON r.id = g.raum_id"
                    " WHERE r.zone_id = ? ORDER BY g.id", (zone_id,)).fetchall()
            if haus_id is not None:
                return self.db.execute(
                    "SELECT g.* FROM geraet g JOIN raum r ON r.id = g.raum_id"
                    " WHERE r.haus_id = ? ORDER BY g.id", (haus_id,)).fetchall()
            return self.db.execute("SELECT * FROM geraet ORDER BY id").fetchall()

    def geraet_nach_seriennummer(self, sn: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.db.execute("SELECT * FROM geraet WHERE seriennummer = ?",
                                   (sn.upper(),)).fetchone()

    def geraet_umbenennen(self, geraet_id: int, name: str) -> bool:
        with self._lock:
            cur = self.db.execute("UPDATE geraet SET name = ? WHERE id = ?",
                                  (name, geraet_id))
            self.db.commit()
            return cur.rowcount > 0

    def geraet_rolle_setzen(self, sn: str, rolle: str,
                            zonen_index: Optional[int] = None) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE geraet SET rolle = ?, zonen_index = COALESCE(?, zonen_index)"
                " WHERE seriennummer = ?", (rolle, zonen_index, sn.upper()))
            self.db.commit()

    def haus_von_geraet(self, sn: str) -> Optional[int]:
        with self._lock:
            zeile = self.db.execute(
                "SELECT r.haus_id AS h FROM geraet g JOIN raum r ON r.id = g.raum_id"
                " WHERE g.seriennummer = ?", (sn.upper(),)).fetchone()
            return zeile["h"] if zeile else None

    # -- Zeitfenster --------------------------------------------------------
    def zeitfenster(self, geraet_id: int) -> List[sqlite3.Row]:
        with self._lock:
            return self.db.execute(
                "SELECT * FROM zeitfenster WHERE geraet_id = ?"
                " ORDER BY wochentag, beginn", (geraet_id,)).fetchall()

    def zeitfenster_anlegen(self, geraet_id: int, wochentag: str, beginn: str,
                            ende: str, modus: str, stufe: str,
                            feuchtestufe: str,
                            lichtsensor: str = "NotAvailable") -> int:
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO zeitfenster (geraet_id, wochentag, beginn, ende,"
                " modus, stufe, feuchtestufe, lichtsensor)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (geraet_id, wochentag, beginn, ende, modus, stufe, feuchtestufe,
                 lichtsensor))
            self.db.commit()
            return cur.lastrowid

    def zeitfenster_aendern(self, zeitfenster_id: int, **felder) -> bool:
        erlaubt = {"wochentag", "beginn", "ende", "modus", "stufe",
                   "feuchtestufe", "lichtsensor"}
        setzen = {k: v for k, v in felder.items() if k in erlaubt and v is not None}
        if not setzen:
            return False
        with self._lock:
            zuweisung = ", ".join(f"{k} = ?" for k in setzen)
            cur = self.db.execute(
                f"UPDATE zeitfenster SET {zuweisung} WHERE id = ?",
                (*setzen.values(), zeitfenster_id))
            self.db.commit()
            return cur.rowcount > 0

    def zeitfenster_loeschen(self, zeitfenster_id: int) -> bool:
        with self._lock:
            cur = self.db.execute("DELETE FROM zeitfenster WHERE id = ?",
                                  (zeitfenster_id,))
            self.db.commit()
            return cur.rowcount > 0

    # -- Livezustand --------------------------------------------------------
    def zustand(self, sn: str) -> Livezustand:
        sn = sn.upper()
        if sn not in self.live:
            self.live[sn] = Livezustand(seriennummer=sn)
        return self.live[sn]

    def zustand_bekannt(self, sn: str) -> bool:
        return sn.upper() in self.live

    def schliessen(self) -> None:
        with self._lock:
            self.db.close()
