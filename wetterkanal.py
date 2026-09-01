#!/usr/bin/env python3
"""
wetterkanal.py — beantwortet die Außenwetter-Anfrage des Geräts.

Der SMART-Modus vergleicht Innen- mit Außenluft. Diese Referenz kommt nicht
über die REST-Schnittstelle: Das Gerät fragt sie über dieselbe TCP-Verbindung
an, mit der es seinen Status meldet. In der Cloud-API heißt der Pakettyp
``OutsideWeatherRequest``.

Stand der Kenntnis
------------------
Dass es diesen Kanal gibt, ist gesichert — der Pakettyp steht in der
veröffentlichten Spezifikation. **Wie die Bytes aussehen, ist es nicht.** Ein
Mitschnitt einer echten Verbindung wird das klären (siehe ``MITSCHNITT.md``).

Bis dahin ist dieses Modul vollständig gebaut, aber bewusst untätig: Es erkennt
Anfragen, holt die Außenwerte, protokolliert alles Nötige — und **sendet
nichts**, solange kein bestätigter Kodierer eingetragen ist. Ein geratenes
Antwortpaket wäre schlimmer als keines: Das Gerät würde auf erfundene
Außenwerte regeln, ohne dass es jemandem auffällt.

Sobald das Format bekannt ist, wird genau eine Funktion ergänzt — der Rest
dieser Datei bleibt, wie er ist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from wetter import Aussenwerte, Wetterdienst

log = logging.getLogger("ambientika.wetterkanal")

#: Rahmentypen, die das Gerät sendet und die bereits verstanden werden.
BEKANNTE_GERAETETYPEN = {0x01, 0x03}


@dataclass
class Wetterstatistik:
    """Was auf dem Kanal passiert ist — für Betrieb und Fehlersuche."""
    anfragen: int = 0
    beantwortet: int = 0
    ohne_daten: int = 0
    ohne_kodierer: int = 0
    unbekannte_typen: dict = field(default_factory=dict)
    letzte_anfrage: Optional[datetime] = None
    letzte_antwort: Optional[datetime] = None


#: Ein Kodierer baut aus Seriennummer und Außenwerten das Antwortpaket.
#: Signatur: (seriennummer, werte) -> bytes
Kodierer = Callable[[str, Aussenwerte], bytes]


class Wetterkanal:
    """Erkennt Wetteranfragen und beantwortet sie, sobald das möglich ist.

    Der Kodierer wird von außen gesetzt. Ist keiner gesetzt, wird die Anfrage
    protokolliert und sonst nichts getan — der einzige verantwortbare Umgang
    mit einem unbekannten Format.
    """

    def __init__(self, wetterdienst: Wetterdienst,
                 kodierer: Optional[Kodierer] = None,
                 anfragetyp: Optional[int] = None):
        self.wetterdienst = wetterdienst
        self.kodierer = kodierer
        #: Das Typbyte der Anfrage. Unbekannt, bis ein Mitschnitt es zeigt —
        #: dann hier eintragen oder über die Konfiguration setzen.
        self.anfragetyp = anfragetyp
        self.statistik = Wetterstatistik()

    @property
    def einsatzbereit(self) -> bool:
        return self.kodierer is not None and self.anfragetyp is not None

    def ist_wetteranfrage(self, rahmen: bytes) -> bool:
        """True, wenn der Rahmen die bekannte Wetteranfrage ist."""
        if not rahmen or self.anfragetyp is None:
            return False
        return rahmen[0] == self.anfragetyp

    def unbekannten_rahmen_notieren(self, rahmen: bytes,
                                    seriennummer: str = "") -> None:
        """Hält Rahmen fest, die weder Status noch Firmware sind.

        Genau hier taucht die Wetteranfrage auf, solange ihr Typbyte noch nicht
        bekannt ist. Die Meldung nennt Typ, Länge und Rohbytes — mehr braucht
        es nicht, um das Format zu bestimmen.
        """
        if not rahmen:
            return
        typ = rahmen[0]
        if typ in BEKANNTE_GERAETETYPEN:
            return
        schluessel = f"0x{typ:02X}"
        self.statistik.unbekannte_typen[schluessel] = \
            self.statistik.unbekannte_typen.get(schluessel, 0) + 1
        anzahl = self.statistik.unbekannte_typen[schluessel]
        # Nur die ersten paar melden, danach still mitzählen — sonst füllt ein
        # stündliches Paket über Monate das Log.
        if anzahl <= 5:
            log.info("Unbekannter Rahmentyp %s von %s, %d Byte: %s  "
                     "(könnte die Wetteranfrage sein — bitte melden)",
                     schluessel, seriennummer or "?", len(rahmen), rahmen.hex())

    def antwort_bauen(self, seriennummer: str, breite: Optional[float],
                      laenge: Optional[float]) -> Optional[bytes]:
        """Baut das Antwortpaket, oder None wenn das nicht möglich ist.

        Drei Gründe für None, alle drei protokolliert und gezählt:
        kein Kodierer, keine Außenwerte, oder ein Kodierer, der scheitert.
        """
        self.statistik.anfragen += 1
        self.statistik.letzte_anfrage = datetime.utcnow()

        if self.kodierer is None:
            self.statistik.ohne_kodierer += 1
            if self.statistik.ohne_kodierer <= 3:
                log.info("Wetteranfrage von %s — das Antwortformat ist noch "
                         "nicht bestätigt, es wird nichts gesendet. Der "
                         "SMART-Modus läuft solange mit den zuletzt bekannten "
                         "Werten des Geräts weiter.", seriennummer)
            return None

        werte = self.wetterdienst.holen(breite, laenge)
        if werte is None:
            self.statistik.ohne_daten += 1
            log.warning("Wetteranfrage von %s, aber keine Außenwerte verfügbar. "
                        "Sensor oder Wetterdienst prüfen.", seriennummer)
            return None

        try:
            paket = self.kodierer(seriennummer, werte)
        except Exception as fehler:                      # noqa: BLE001
            log.error("Kodierer für die Wetterantwort ist gescheitert: %s", fehler)
            return None

        self.statistik.beantwortet += 1
        self.statistik.letzte_antwort = datetime.utcnow()
        log.debug("Wetterantwort an %s: %.1f °C, %.0f %% (%s)",
                  seriennummer, werte.temperatur, werte.feuchte, werte.quelle)
        return paket

    def bericht(self) -> dict:
        """Zustand des Kanals, für die Betriebsanzeige."""
        s = self.statistik
        return {
            "einsatzbereit": self.einsatzbereit,
            "anfragetyp": None if self.anfragetyp is None
                          else f"0x{self.anfragetyp:02X}",
            "anfragen": s.anfragen,
            "beantwortet": s.beantwortet,
            "ohne_aussenwerte": s.ohne_daten,
            "ohne_kodierer": s.ohne_kodierer,
            "unbekannte_rahmentypen": dict(s.unbekannte_typen),
            "letzte_anfrage": s.letzte_anfrage.isoformat()
                              if s.letzte_anfrage else None,
            "letzte_antwort": s.letzte_antwort.isoformat()
                              if s.letzte_antwort else None,
        }


# ---------------------------------------------------------------------------
# Platz für den bestätigten Kodierer
# ---------------------------------------------------------------------------
def kodierer_aus_mitschnitt(anfragetyp: int, vorlage: bytes,
                            offset_temperatur: int,
                            offset_feuchte: int) -> Kodierer:
    """Baut einen Kodierer aus dem, was ein Mitschnitt gezeigt hat.

    Die Vorlage ist ein echtes Antwortpaket der Cloud. Ersetzt werden nur die
    beiden Messwerte an den angegebenen Stellen; alles andere bleibt Byte für
    Byte wie beobachtet. Das ist der sicherste Weg, ein Format zu bedienen,
    das man nicht vollständig versteht: Was man nicht kennt, lässt man in Ruhe.

    Temperatur wird vorzeichenbehaftet als ein Byte in ganzen Grad kodiert,
    die Feuchte als ein Byte in Prozent — so, wie das Gerät seine eigenen
    Messwerte meldet. Ob die Antwort dieselbe Auflösung verwendet, muss der
    Mitschnitt zeigen; falls nicht, wird diese Funktion entsprechend angepasst.
    """
    def kodieren(seriennummer: str, werte: Aussenwerte) -> bytes:
        paket = bytearray(vorlage)
        if len(seriennummer) == 12 and len(paket) >= 8:
            paket[2:8] = bytes.fromhex(seriennummer)
        temperatur = max(-128, min(127, int(round(werte.temperatur))))
        paket[offset_temperatur] = temperatur & 0xFF
        paket[offset_feuchte] = max(0, min(100, int(round(werte.feuchte))))
        return bytes(paket)

    kodieren.anfragetyp = anfragetyp
    return kodieren


def kanal_aus_konfiguration(wetterdienst: Wetterdienst,
                            konfig: dict) -> Wetterkanal:
    """Baut den Kanal; ohne bestätigtes Format bleibt er untätig.

    Konfigurationsbeispiel, sobald der Mitschnitt ausgewertet ist::

        wetterkanal:
          anfragetyp: 0x05
          vorlage: "05001c9dc24304440a2d0000"
          offset_temperatur: 8
          offset_feuchte: 9
    """
    if not konfig or "vorlage" not in konfig or "anfragetyp" not in konfig:
        return Wetterkanal(wetterdienst)

    try:
        anfragetyp = int(str(konfig["anfragetyp"]), 0)
        vorlage = bytes.fromhex(str(konfig["vorlage"]).replace(" ", ""))
        kodierer = kodierer_aus_mitschnitt(
            anfragetyp, vorlage,
            int(konfig["offset_temperatur"]), int(konfig["offset_feuchte"]))
    except (KeyError, TypeError, ValueError) as fehler:
        log.error("Wetterkanal-Konfiguration unbrauchbar (%s) — der Kanal "
                  "bleibt untätig, statt geratene Pakete zu senden.", fehler)
        return Wetterkanal(wetterdienst)

    log.info("Wetterkanal einsatzbereit: Anfragetyp 0x%02X, Vorlage %d Byte",
             anfragetyp, len(vorlage))
    return Wetterkanal(wetterdienst, kodierer, anfragetyp)
