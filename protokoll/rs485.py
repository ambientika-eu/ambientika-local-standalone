#!/usr/bin/env python3
"""
rs485.py — Codec für den kabelgebundenen RS485-Bus (ADVANCED+ / ADVANCED B+).

Quelle: „Programmierung Ambientika über RS485", Revision Juli 2026, sowie
„RS485-Kommunikationsprotokoll Ambientika", November 2024.

WICHTIG — das ist NICHT das Protokoll der SMART/OFFICE-Geräte.
------------------------------------------------------------------
Der RS485-Bus verbindet Wandpanel und Lüfter. Er überträgt Betriebsmodus und
drei Alarmbits, aber weder Seriennummer noch Temperatur, Feuchte, Luftqualität
oder Signalstärke. Die SMART- und OFFICE-Geräte sprechen ein völlig anderes,
binäres Protokoll über TCP 11000 (siehe ``ambientika_protocol.py``).

Beide Wege koexistieren, lösen aber verschiedene Aufgaben: RS485 für KNX und
Loxone an verkabelten Anlagen, TCP für die App und den lokalen Server.

Widersprüche zwischen den Dokumentrevisionen
--------------------------------------------
Die Ausgabe November 2024 und die Revision Juli 2026 widersprechen sich an zwei
Stellen. Implementiert ist jeweils die Juli-2026-Fassung, weil sie laut eigenem
Vorwort aus einem realen Gerätemitschnitt korrigiert wurde — und weil ihre
Beispielrahmen sich mit ihren eigenen Prüfsummen decken.

1. **Statusmeldung.** November 2024 nennt Bit 7 Feuchtealarm, Bit 6 Filteralarm,
   Bit 5 Dämmerung. Juli 2026 nennt Bit 0 Dämmerung, Bit 1 Filteralarm,
   Bit 2 Feuchtealarm, Bit 3 dauerhaft gesetzt. Die acht Beispielrahmen 0x08
   bis 0x0F bestätigen ausschließlich die zweite Lesart.

   Die Ausgabe Juni 2025 beschrieb zusätzlich eine 11 Byte lange Antwort mit
   acht Einzelregistern. Die existiert laut Juli-2026-Revision nicht.

2. **Filter-Reset.** November 2024 verortet ihn auf Bit 5 des zweiten
   Datenbytes. Der Beispielrahmen beider Ausgaben setzt jedoch Bit 0
   (Datenbyte 2 = 0x01). Implementiert ist Bit 0; am Gerät zu bestätigen.

Rahmenformat
------------
Jeder Rahmen: Nachrichtentyp, Nachrichtendaten, Prüfsumme (XOR über Typ und
Daten). Jedes Byte wird zu zwei ASCII-Zeichen seiner Hex-Darstellung erweitert,
davor STX (0x02), dahinter ETX (0x03).

Serielle Parameter: 9600 Bit/s, 8 Datenbits, 1 Stoppbit, keine Parität.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

STX = 0x02
ETX = 0x03

TYP_STATUSMELDUNG = 0x00
TYP_BEFEHL = 0x01
TYP_STATUSABFRAGE = 0x02

#: Moduskommandos müssen zyklisch gesendet werden. Bleiben sie aus, fällt das
#: Gerät in den Standalone-Betrieb zurück und hält den Modus nicht.
SENDETAKT_MS = 500
#: Nach einer Statusabfrage muss die Leitung so lange frei bleiben.
STATUS_RUHEZEIT_S = 3.0

# --- Bitbelegung des Befehls-Datenbytes -----------------------------------
# Gegen alle 27 Beispielrahmen der Juli-2026-Anleitung verifiziert.
#   Bit 1..0  Motordrehzahl      0 Nacht, 1..3 Stufe 1..3
#   Bit 3..2  Motordrehung       0 gestoppt, 1 Abluft, 2 Zuluft
#   Bit 4     Slave-Verhalten    0 nach DIP-Schaltern, 1 gleichläufig zum Master
#   Bit 5     Thermoaktor        0 aus (Klappe zu), 1 ein (Klappe offen)
#   Bit 7..6  Feuchteschwelle    0 Stufe 1, 1 Stufe 2, 2 Stufe 3
DREHZAHL_NACHT, DREHZAHL_1, DREHZAHL_2, DREHZAHL_3 = 0, 1, 2, 3
DREHUNG_GESTOPPT, DREHUNG_ABLUFT, DREHUNG_ZULUFT = 0, 1, 2
SLAVE_NACH_DIP, SLAVE_WIE_MASTER = 0, 1
FEUCHTE_1, FEUCHTE_2, FEUCHTE_3 = 0, 1, 2

#: Bit 0 des zweiten Datenbytes — siehe Widerspruch 2 im Modulkopf.
FLAG_FILTER_RESET = 0x01

DREHZAHL_NAMEN = {0: "Nacht", 1: "Stufe 1", 2: "Stufe 2", 3: "Stufe 3"}
DREHUNG_NAMEN = {0: "gestoppt", 1: "Abluft", 2: "Zuluft"}
FEUCHTE_NAMEN = {0: "Feuchtestufe 1", 1: "Feuchtestufe 2", 2: "Feuchtestufe 3"}


def pruefsumme(*bytes_: int) -> int:
    """XOR über Nachrichtentyp und Nachrichtendaten."""
    wert = 0
    for b in bytes_:
        wert ^= b & 0xFF
    return wert


def rahmen_bauen(typ: int, *daten: int) -> bytes:
    """Baut einen sendefertigen Rahmen inklusive Erweiterung und STX/ETX."""
    cs = pruefsumme(typ, *daten)
    nutz = "".join(f"{b & 0xFF:02X}" for b in (typ, *daten, cs))
    return bytes([STX]) + nutz.encode("ascii") + bytes([ETX])


def rahmen_zerlegen(rahmen: bytes) -> tuple:
    """Zerlegt einen empfangenen Rahmen in (typ, [daten...]).

    Wirft ValueError bei fehlendem STX/ETX, ungerader Zeichenzahl, ungültigen
    Hex-Zeichen oder falscher Prüfsumme — ein verworfener Rahmen ist besser als
    ein falsch gelesener.
    """
    if len(rahmen) < 4 or rahmen[0] != STX or rahmen[-1] != ETX:
        raise ValueError("Rahmen ohne STX/ETX")
    kern = rahmen[1:-1].decode("ascii", errors="strict")
    if len(kern) % 2:
        raise ValueError(f"ungerade Zeichenzahl: {len(kern)}")
    werte = [int(kern[i:i + 2], 16) for i in range(0, len(kern), 2)]
    if len(werte) < 2:
        raise ValueError("Rahmen ohne Prüfsumme")
    *nutz, cs = werte
    if pruefsumme(*nutz) != cs:
        raise ValueError(f"Prüfsumme falsch: erwartet {pruefsumme(*nutz):02X}, "
                         f"empfangen {cs:02X}")
    return nutz[0], nutz[1:]


# ---------------------------------------------------------------------------
# Befehle
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Befehl:
    """Ein Moduskommando, wie es zyklisch alle 500 ms gesendet wird."""
    drehzahl: int = DREHZAHL_NACHT
    drehung: int = DREHUNG_GESTOPPT
    slave: int = SLAVE_NACH_DIP
    thermoaktor: int = 0
    feuchteschwelle: int = FEUCHTE_1
    filter_reset: bool = False

    def datenbyte(self) -> int:
        return ((self.feuchteschwelle & 0x03) << 6
                | (self.thermoaktor & 0x01) << 5
                | (self.slave & 0x01) << 4
                | (self.drehung & 0x03) << 2
                | (self.drehzahl & 0x03))

    def rahmen(self) -> bytes:
        d2 = FLAG_FILTER_RESET if self.filter_reset else 0x00
        return rahmen_bauen(TYP_BEFEHL, self.datenbyte(), d2)

    def beschreibung(self) -> str:
        if self.drehung == DREHUNG_GESTOPPT:
            zustand = "Motor Pause, Klappe offen" if self.thermoaktor \
                      else "Motor aus, Klappe geschlossen"
        else:
            zustand = (f"{DREHUNG_NAMEN[self.drehung]} "
                       f"{DREHZAHL_NAMEN[self.drehzahl]}")
        teile = [zustand, FEUCHTE_NAMEN[self.feuchteschwelle]]
        if self.slave == SLAVE_WIE_MASTER:
            teile.append("Slave gleichläufig")
        if self.filter_reset:
            teile.append("Filter-Reset")
        return ", ".join(teile)


def befehl_zerlegen(datenbyte: int, datenbyte2: int = 0x00) -> Befehl:
    return Befehl(
        drehzahl=datenbyte & 0x03,
        drehung=(datenbyte >> 2) & 0x03,
        slave=(datenbyte >> 4) & 0x01,
        thermoaktor=(datenbyte >> 5) & 0x01,
        feuchteschwelle=(datenbyte >> 6) & 0x03,
        filter_reset=bool(datenbyte2 & FLAG_FILTER_RESET),
    )


MOTOR_AUS = Befehl()
MOTOR_PAUSE = Befehl(thermoaktor=1)
FILTER_RESET = Befehl(filter_reset=True)


def statusabfrage() -> bytes:
    """Nur der Nachrichtentyp, ohne Daten. Danach 3 s Funkstille halten."""
    return rahmen_bauen(TYP_STATUSABFRAGE)


# ---------------------------------------------------------------------------
# Statusmeldung
# ---------------------------------------------------------------------------
BIT_DAEMMERUNG = 0x01
BIT_FILTERALARM = 0x02
BIT_FEUCHTEALARM = 0x04
#: Dauerhaft gesetzt; kennzeichnet die gültige Meldung eines betriebsbereiten
#: Masters. Fehlt das Bit, ist der Rahmen keine belastbare Statusmeldung.
BIT_GUELTIG = 0x08


@dataclass(frozen=True)
class Status:
    daemmerung: bool
    filteralarm: bool
    feuchtealarm: bool
    gueltig: bool

    @property
    def alarme(self) -> list:
        aktiv = []
        if self.daemmerung:
            aktiv.append("Dämmerung")
        if self.filteralarm:
            aktiv.append("Filteralarm")
        if self.feuchtealarm:
            aktiv.append("Feuchtealarm")
        return aktiv


def status_zerlegen(datenbyte: int) -> Status:
    return Status(
        daemmerung=bool(datenbyte & BIT_DAEMMERUNG),
        filteralarm=bool(datenbyte & BIT_FILTERALARM),
        feuchtealarm=bool(datenbyte & BIT_FEUCHTEALARM),
        gueltig=bool(datenbyte & BIT_GUELTIG),
    )


def antwort_lesen(rahmen: bytes) -> Optional[Status]:
    """Wertet einen empfangenen Rahmen aus, oder None wenn es keine
    Statusmeldung ist.

    Der Master schickt zu jedem Moduskommando einen Echo-Rahmen vom Typ 01
    zurück. Diese Echos werden hier still verworfen — genau dafür filtert der
    Parser auf Nachrichtentyp 00.
    """
    typ, daten = rahmen_zerlegen(rahmen)
    if typ != TYP_STATUSMELDUNG:
        return None
    if not daten:
        raise ValueError("Statusmeldung ohne Datenbyte")
    return status_zerlegen(daten[0])
