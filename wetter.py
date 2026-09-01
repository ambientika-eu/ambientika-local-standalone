#!/usr/bin/env python3
"""
wetter.py — Außenluftwerte für den SMART-Modus.

Warum es das überhaupt braucht
------------------------------
Der SMART-Modus vergleicht die Innenwerte mit der Außenluft. Diese Referenz
kommt nicht über die REST-Schnittstelle: Das Gerät fragt sie über dieselbe
TCP-Verbindung an, mit der es seinen Status meldet — in der Cloud-API trägt der
Pakettyp den Namen ``OutsideWeatherRequest``. Der lokale Server beantwortet
genau diese Anfrage.

Drei Quellen, absteigend nach Güte
----------------------------------
1. **Eigener Sensor im Heimnetz.** Der Betrieb bleibt vollständig offline, und
   die Werte stammen von der Luft, die das Haus tatsächlich ansaugt. Eine
   Wetterstation zehn Kilometer entfernt weiß nichts über die Nordwand.
2. **OpenWeatherMap**, wie die Cloud es tut. Braucht Internet und einen eigenen
   Schlüssel.
3. **Fester Wert.** Nur für Inbetriebnahme und Prüfstand.

Der Zwischenspeicher ist kein Komfort, sondern Notwendigkeit: Zehn Geräte, die
ihre Anfrage unabhängig stellen, würden sonst zehnmal dieselbe Abfrage
auslösen. Bei OpenWeatherMap ist das im freien Tarif schnell das Kontingent.
"""

from __future__ import annotations

import json
import logging
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

log = logging.getLogger("ambientika.wetter")

#: So lange gilt ein einmal geholter Wert als frisch. Außenluft ändert sich
#: nicht in Sekunden, und jede Abfrage kostet Kontingent.
CACHE_DAUER = timedelta(minutes=10)

#: Ab dieser Alterung gilt ein Wert als unbrauchbar. Lieber keine Antwort als
#: eine Regelung auf die Wetterlage von gestern.
MAX_ALTER = timedelta(hours=3)


@dataclass(frozen=True)
class Aussenwerte:
    temperatur: float
    feuchte: float
    quelle: str
    zeitpunkt: datetime

    @property
    def taupunkt(self) -> Optional[float]:
        return taupunkt(self.temperatur, self.feuchte)

    @property
    def absolute_feuchte(self) -> Optional[float]:
        return absolute_feuchte(self.temperatur, self.feuchte)

    def alter(self, jetzt: Optional[datetime] = None) -> timedelta:
        return (jetzt or datetime.utcnow()) - self.zeitpunkt

    def brauchbar(self, jetzt: Optional[datetime] = None) -> bool:
        return self.alter(jetzt) <= MAX_ALTER


def taupunkt(temp_c: float, rh_pct: float) -> Optional[float]:
    """Taupunkt in °C, Magnus-Tetens mit Sonntag-Koeffizienten."""
    try:
        t, rh = float(temp_c), float(rh_pct)
    except (TypeError, ValueError):
        return None
    if rh <= 0 or rh > 100:
        return None
    a, b = 17.62, 243.12
    gamma = math.log(rh / 100.0) + (a * t) / (b + t)
    return round((b * gamma) / (a - gamma), 2)


def absolute_feuchte(temp_c: float, rh_pct: float) -> Optional[float]:
    """Absolute Feuchte in g/m³ — die Größe, auf die zu regeln ist.

    Lüften trocknet nur, wenn die Außenluft absolut trockener ist. Die relative
    Feuchte taugt für diese Entscheidung nicht: Draußen 28 °C bei 60 % tragen
    16,3 g/m³, drinnen 22 °C bei 60 % nur 11,6.
    """
    try:
        t, rh = float(temp_c), float(rh_pct)
    except (TypeError, ValueError):
        return None
    if rh < 0 or rh > 100:
        return None
    es = 6.112 * math.exp((17.62 * t) / (243.12 + t))
    return round(216.679 * (rh / 100.0) * es / (t + 273.15), 2)


# ---------------------------------------------------------------------------
# Quellen
# ---------------------------------------------------------------------------
class Wetterquelle:
    name = "unbekannt"

    def abrufen(self, breite: Optional[float],
                laenge: Optional[float]) -> Optional[Aussenwerte]:
        raise NotImplementedError


class FesteWerte(Wetterquelle):
    """Unveränderliche Werte — für Inbetriebnahme und Tests."""
    name = "fest"

    def __init__(self, temperatur: float = 10.0, feuchte: float = 70.0):
        self.temperatur = temperatur
        self.feuchte = feuchte

    def abrufen(self, breite=None, laenge=None) -> Aussenwerte:
        return Aussenwerte(self.temperatur, self.feuchte, self.name,
                           datetime.utcnow())


class LokalerSensor(Wetterquelle):
    """Ein Sensor im eigenen Netz, abgefragt über HTTP.

    Erwartet eine JSON-Antwort und liest zwei Felder daraus. Die Feldnamen sind
    einstellbar, weil jede Sensor-Firmware sie anders nennt: Home Assistant
    liefert ``state``, ESPHome ``value``, andere ``temperature``.
    """
    name = "lokaler Sensor"

    def __init__(self, url: str, feld_temperatur: str = "temperature",
                 feld_feuchte: str = "humidity", zeitlimit: float = 5.0,
                 kopfzeilen: Optional[dict] = None):
        self.url = url
        self.feld_temperatur = feld_temperatur
        self.feld_feuchte = feld_feuchte
        self.zeitlimit = zeitlimit
        self.kopfzeilen = kopfzeilen or {}

    def abrufen(self, breite=None, laenge=None) -> Optional[Aussenwerte]:
        try:
            anfrage = urllib.request.Request(self.url, headers=self.kopfzeilen)
            with urllib.request.urlopen(anfrage, timeout=self.zeitlimit) as antwort:
                daten = json.loads(antwort.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
            return None
        try:
            return Aussenwerte(float(daten[self.feld_temperatur]),
                               float(daten[self.feld_feuchte]),
                               self.name, datetime.utcnow())
        except (KeyError, TypeError, ValueError):
            return None


class OpenWeatherMap(Wetterquelle):
    """Dieselbe Quelle, die auch die Cloud verwendet.

    Endpunkt ``data/2.5/weather`` mit ``lat``, ``lon``, ``appid`` und
    ``units=metric``. Gelesen werden ``main.temp`` (°C) und ``main.humidity``
    (%). Die Einheitenangabe wirkt nur auf die Temperatur — die Feuchte kommt
    immer in Prozent.

    Der Schlüssel gehört in die Konfiguration der Installation, nicht in den
    Quelltext. Ein in einer App oder einem Repository mitgelieferter Schlüssel
    ist für jeden auslesbar, der die Datei öffnet — dann verbraucht ein Fremder
    das Kontingent oder verursacht Kosten.
    """
    name = "OpenWeatherMap"
    BASIS = "https://api.openweathermap.org/data/2.5/weather"

    #: HTTP-Fehler, die nicht von selbst weggehen. Ein falscher Schlüssel wird
    #: durch Wiederholen nicht richtig — er muss gemeldet werden, sonst sieht
    #: es monatelang aus wie „gerade keine Daten".
    DAUERHAFTE_FEHLER = {401: "Schlüssel ungültig oder noch nicht aktiv",
                         403: "Zugriff verweigert",
                         404: "Koordinaten nicht auflösbar",
                         429: "Kontingent erschöpft"}

    def __init__(self, api_schluessel: str, zeitlimit: float = 8.0):
        self.api_schluessel = api_schluessel
        self.zeitlimit = zeitlimit
        self.letzter_fehler: Optional[str] = None

    def abrufen(self, breite: Optional[float],
                laenge: Optional[float]) -> Optional[Aussenwerte]:
        if breite is None or laenge is None:
            self.letzter_fehler = "Haus ohne Koordinaten — Breite und Länge fehlen"
            return None
        parameter = urllib.parse.urlencode({
            "lat": breite, "lon": laenge,
            "appid": self.api_schluessel, "units": "metric",
        })
        try:
            with urllib.request.urlopen(f"{self.BASIS}?{parameter}",
                                        timeout=self.zeitlimit) as antwort:
                daten = json.loads(antwort.read().decode("utf-8"))
        except urllib.error.HTTPError as fehler:
            grund = self.DAUERHAFTE_FEHLER.get(fehler.code)
            if grund:
                self.letzter_fehler = f"HTTP {fehler.code}: {grund}"
                log.error("OpenWeatherMap antwortet %s (%s). Das geht nicht von "
                          "selbst weg — Schlüssel und Tarif prüfen.",
                          fehler.code, grund)
            else:
                self.letzter_fehler = f"HTTP {fehler.code}"
            return None
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as fehler:
            # Vorübergehend: Netz weg, Zeitüberschreitung, kaputte Antwort.
            self.letzter_fehler = str(fehler)
            return None

        try:
            haupt = daten["main"]
            werte = Aussenwerte(float(haupt["temp"]), float(haupt["humidity"]),
                                self.name, datetime.utcnow())
        except (KeyError, TypeError, ValueError) as fehler:
            self.letzter_fehler = f"Antwort ohne main.temp/main.humidity: {fehler}"
            return None

        self.letzter_fehler = None
        return werte


# ---------------------------------------------------------------------------
# Dienst mit Zwischenspeicher und Rückfallebene
# ---------------------------------------------------------------------------
class Wetterdienst:
    """Fragt die Quellen der Reihe nach ab und merkt sich das Ergebnis.

    Die Reihenfolge ist die Vorzugsreihenfolge: Der lokale Sensor kommt zuerst,
    OpenWeatherMap ist die Rückfallebene. Liefert keine Quelle etwas, wird der
    letzte bekannte Wert weitergereicht, solange er nicht zu alt ist — eine
    kurze Störung soll die Regelung nicht blind machen.
    """

    def __init__(self, *quellen: Wetterquelle,
                 cache_dauer: timedelta = CACHE_DAUER):
        self.quellen = [q for q in quellen if q is not None]
        self.cache_dauer = cache_dauer
        self._zwischenspeicher: dict = {}

    def _schluessel(self, breite, laenge) -> tuple:
        if breite is None or laenge is None:
            return (None, None)
        # Auf drei Nachkommastellen runden: rund 100 m. Feiner aufzulösen
        # brächte keine andere Wetterlage, würde aber den Zwischenspeicher
        # bei jeder Nachkommastelle neu füllen.
        return (round(float(breite), 3), round(float(laenge), 3))

    def holen(self, breite: Optional[float] = None,
              laenge: Optional[float] = None,
              jetzt: Optional[datetime] = None) -> Optional[Aussenwerte]:
        jetzt = jetzt or datetime.utcnow()
        schluessel = self._schluessel(breite, laenge)

        gemerkt = self._zwischenspeicher.get(schluessel)
        if gemerkt and jetzt - gemerkt.zeitpunkt < self.cache_dauer:
            return gemerkt

        for quelle in self.quellen:
            werte = quelle.abrufen(breite, laenge)
            if werte is not None:
                self._zwischenspeicher[schluessel] = werte
                return werte

        # Keine Quelle erreichbar: der letzte Wert, solange er brauchbar ist.
        if gemerkt and gemerkt.brauchbar(jetzt):
            return gemerkt
        return None

    def leeren(self) -> None:
        self._zwischenspeicher.clear()


def dienst_aus_konfiguration(konfig: dict) -> Wetterdienst:
    """Baut den Dienst aus einem Konfigurationsabschnitt.

    Beispiel::

        wetter:
          sensor_url: "http://192.168.1.50/api/aussen"
          sensor_feld_temperatur: "temperature"
          sensor_feld_feuchte: "humidity"
          openweathermap_schluessel: "..."
          fester_wert: {temperatur: 10, feuchte: 70}
    """
    quellen = []
    if konfig.get("sensor_url"):
        quellen.append(LokalerSensor(
            konfig["sensor_url"],
            konfig.get("sensor_feld_temperatur", "temperature"),
            konfig.get("sensor_feld_feuchte", "humidity")))
    if konfig.get("openweathermap_schluessel"):
        quellen.append(OpenWeatherMap(konfig["openweathermap_schluessel"]))
    fest = konfig.get("fester_wert")
    if fest:
        quellen.append(FesteWerte(float(fest.get("temperatur", 10.0)),
                                  float(fest.get("feuchte", 70.0))))
    return Wetterdienst(*quellen)
