#!/usr/bin/env python3
"""
sensoren.py — Außensensoren im Heimnetz als Wetterquelle.

Warum das die bevorzugte Quelle ist
-----------------------------------
Der SMART-Modus vergleicht Innen- mit Außenluft. Ein Wetterdienst liefert die
Lage an einer Station, die durchaus zehn Kilometer entfernt sein kann — über die
Luft, die das Haus an der Nordwand ansaugt, sagt das wenig. Ein eigener Sensor
misst genau diese Luft. Nebenbei bleibt der Betrieb damit vollständig offline.

Vier Anbindungen, weil Außensensoren in der Praxis dort hängen
--------------------------------------------------------------
* **Home Assistant** — zwei Entitäten über die REST-Schnittstelle.
* **ESPHome** — der eingebaute Webserver, ohne Home Assistant dazwischen.
* **MQTT** — wo die meisten Funksensoren landen (Zigbee2MQTT, Tasmota, ESPHome).
* **Beliebiges JSON über HTTP** — für alles andere.

Alle vier liefern dasselbe ``Aussenwerte``-Objekt und lassen sich im
Wetterdienst in beliebiger Reihenfolge hintereinanderschalten.

Eine Regel gilt überall: **Ein Sensor, der nicht antwortet, liefert ``None``.**
Er wirft keine Ausnahme und erfindet keinen Wert. Ein erfundener Außenwert wäre
schlimmer als gar keiner — die Lüftung würde auf eine Messung reagieren, die es
nicht gibt.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Optional

from wetter import Aussenwerte, Wetterquelle

log = logging.getLogger("ambientika.sensoren")

#: Zeichenketten, mit denen Sensoren „ich habe gerade nichts" ausdrücken.
UNGUELTIG = {"unknown", "unavailable", "none", "nan", "null", ""}


def _zahl(wert) -> Optional[float]:
    """Wandelt einen Sensorwert in eine Zahl, oder None wenn er unbrauchbar ist."""
    if wert is None:
        return None
    if isinstance(wert, bool):          # True wäre sonst 1.0
        return None
    if isinstance(wert, (int, float)):
        zahl = float(wert)
    else:
        text = str(wert).strip()
        if text.lower() in UNGUELTIG:
            return None
        try:
            zahl = float(text.replace(",", "."))
        except ValueError:
            return None
    # NaN und Unendlich schleichen sich über JSON durch und würden jede
    # Taupunktrechnung vergiften.
    if zahl != zahl or zahl in (float("inf"), float("-inf")):
        return None
    return zahl


def _plausibel(temperatur: Optional[float],
               feuchte: Optional[float]) -> bool:
    """Grobe Plausibilitätsgrenzen für Außenluft in Mitteleuropa und darüber
    hinaus. Fängt vertauschte Felder und kaputte Sensoren ab, nicht
    Messungenauigkeit."""
    if temperatur is None or feuchte is None:
        return False
    if not -60.0 <= temperatur <= 60.0:
        return False
    if not 0.0 <= feuchte <= 100.0:
        return False
    return True


def _hole_json(url: str, kopfzeilen: Optional[dict], zeitlimit: float):
    anfrage = urllib.request.Request(url, headers=kopfzeilen or {})
    with urllib.request.urlopen(anfrage, timeout=zeitlimit) as antwort:
        return json.loads(antwort.read().decode("utf-8"))


class HttpSensorBasis(Wetterquelle):
    """Gemeinsames Verhalten aller über HTTP abgefragten Sensoren."""

    def __init__(self, zeitlimit: float = 5.0):
        self.zeitlimit = zeitlimit
        self.letzter_fehler: Optional[str] = None

    def _fertig(self, temperatur, feuchte) -> Optional[Aussenwerte]:
        t, rh = _zahl(temperatur), _zahl(feuchte)
        if not _plausibel(t, rh):
            self.letzter_fehler = (f"unbrauchbare Werte: Temperatur {temperatur!r}, "
                                   f"Feuchte {feuchte!r}")
            log.debug("%s: %s", self.name, self.letzter_fehler)
            return None
        self.letzter_fehler = None
        return Aussenwerte(t, rh, self.name, datetime.utcnow())


class HomeAssistantSensor(HttpSensorBasis):
    """Zwei Entitäten aus einer Home-Assistant-Instanz.

    Gebraucht werden die Basis-URL, ein langlebiges Zugriffstoken und die
    beiden Entitäts-IDs. Das Token wird unter *Profil → Sicherheit →
    Langlebige Zugriffstoken* erzeugt.

    Home Assistant liefert Messwerte als Zeichenkette im Feld ``state`` und
    setzt bei fehlendem Wert ``unavailable`` oder ``unknown`` — beides wird
    hier als „kein Wert" behandelt und nicht etwa als Null.
    """
    name = "Home Assistant"

    def __init__(self, basis_url: str, token: str,
                 entitaet_temperatur: str, entitaet_feuchte: str,
                 zeitlimit: float = 5.0):
        super().__init__(zeitlimit)
        self.basis_url = basis_url.rstrip("/")
        self.token = token
        self.entitaet_temperatur = entitaet_temperatur
        self.entitaet_feuchte = entitaet_feuchte

    def _zustand(self, entitaet: str):
        daten = _hole_json(f"{self.basis_url}/api/states/{entitaet}",
                           {"Authorization": f"Bearer {self.token}",
                            "Content-Type": "application/json"},
                           self.zeitlimit)
        return daten.get("state")

    def abrufen(self, breite=None, laenge=None) -> Optional[Aussenwerte]:
        try:
            temperatur = self._zustand(self.entitaet_temperatur)
            feuchte = self._zustand(self.entitaet_feuchte)
        except urllib.error.HTTPError as fehler:
            if fehler.code == 401:
                self.letzter_fehler = "Token abgelehnt (401)"
                log.error("Home Assistant weist das Token zurück. Das geht nicht "
                          "von selbst weg — neues langlebiges Token erzeugen.")
            else:
                self.letzter_fehler = f"HTTP {fehler.code}"
            return None
        except (urllib.error.URLError, OSError, json.JSONDecodeError,
                KeyError, TypeError) as fehler:
            self.letzter_fehler = str(fehler)
            return None
        return self._fertig(temperatur, feuchte)


class ESPHomeSensor(HttpSensorBasis):
    """Der eingebaute Webserver eines ESPHome-Geräts, ohne Umweg.

    ESPHome liefert unter ``/sensor/<name>`` ein JSON-Objekt mit den Feldern
    ``state`` (formatiert, mit Einheit) und ``value`` (die reine Zahl). Gelesen
    wird ``value``, weil ``state`` je nach Konfiguration „12.3 °C" enthält.
    """
    name = "ESPHome"

    def __init__(self, basis_url: str, sensor_temperatur: str,
                 sensor_feuchte: str, zeitlimit: float = 5.0):
        super().__init__(zeitlimit)
        self.basis_url = basis_url.rstrip("/")
        self.sensor_temperatur = sensor_temperatur
        self.sensor_feuchte = sensor_feuchte

    def _wert(self, sensor: str):
        daten = _hole_json(f"{self.basis_url}/sensor/{sensor}", None, self.zeitlimit)
        # 'value' ist die reine Zahl, 'state' die formatierte Anzeige.
        return daten.get("value", daten.get("state"))

    def abrufen(self, breite=None, laenge=None) -> Optional[Aussenwerte]:
        try:
            temperatur = self._wert(self.sensor_temperatur)
            feuchte = self._wert(self.sensor_feuchte)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                json.JSONDecodeError, KeyError, TypeError) as fehler:
            self.letzter_fehler = str(fehler)
            return None
        return self._fertig(temperatur, feuchte)


class JsonSensor(HttpSensorBasis):
    """Beliebige JSON-Antwort über HTTP, mit einstellbaren Feldnamen.

    Die Feldnamen dürfen verschachtelt angegeben werden, getrennt durch Punkte:
    ``sensor.aussen.temperatur``. Damit lässt sich fast jede Sensor-Firmware
    anbinden, ohne für jede eine eigene Klasse zu schreiben.
    """
    name = "JSON-Sensor"

    def __init__(self, url: str, pfad_temperatur: str = "temperature",
                 pfad_feuchte: str = "humidity", zeitlimit: float = 5.0,
                 kopfzeilen: Optional[dict] = None):
        super().__init__(zeitlimit)
        self.url = url
        self.pfad_temperatur = pfad_temperatur
        self.pfad_feuchte = pfad_feuchte
        self.kopfzeilen = kopfzeilen or {}

    @staticmethod
    def _tief(daten, pfad: str):
        stelle = daten
        for teil in pfad.split("."):
            if isinstance(stelle, dict) and teil in stelle:
                stelle = stelle[teil]
            elif isinstance(stelle, list) and teil.isdigit():
                index = int(teil)
                if index >= len(stelle):
                    return None
                stelle = stelle[index]
            else:
                return None
        return stelle

    def abrufen(self, breite=None, laenge=None) -> Optional[Aussenwerte]:
        try:
            daten = _hole_json(self.url, self.kopfzeilen, self.zeitlimit)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                json.JSONDecodeError) as fehler:
            self.letzter_fehler = str(fehler)
            return None
        return self._fertig(self._tief(daten, self.pfad_temperatur),
                            self._tief(daten, self.pfad_feuchte))


class MqttSensor(Wetterquelle):
    """Hört auf zwei MQTT-Themen und merkt sich den letzten Wert.

    Der wichtigste Fall in der Praxis: Zigbee2MQTT, Tasmota und ESPHome
    veröffentlichen ihre Messwerte alle hierhin. Die Werte kommen, wann der
    Sensor sendet — deshalb wird hier nicht abgefragt, sondern zugehört und
    der zuletzt empfangene Wert vorgehalten.

    Der Wert bekommt eine Verfallszeit. Ein Funksensor, dessen Batterie leer
    ist, hört einfach auf zu senden; ohne Verfallszeit würde die Lüftung
    wochenlang auf den letzten Messwert vor dem Ausfall regeln.

    Nutzlast entweder eine nackte Zahl (``12.3``) oder ein JSON-Objekt, aus dem
    ein Feld gelesen wird (Zigbee2MQTT sendet ``{"temperature": 12.3,
    "humidity": 88}``).
    """
    name = "MQTT"

    def __init__(self, broker: str, port: int = 1883,
                 thema_temperatur: str = "", thema_feuchte: str = "",
                 feld_temperatur: Optional[str] = None,
                 feld_feuchte: Optional[str] = None,
                 benutzer: Optional[str] = None, passwort: Optional[str] = None,
                 max_alter: timedelta = timedelta(minutes=30),
                 client_id: str = "ambientika-local-wetter"):
        self.broker = broker
        self.port = port
        self.thema_temperatur = thema_temperatur
        self.thema_feuchte = thema_feuchte
        self.feld_temperatur = feld_temperatur
        self.feld_feuchte = feld_feuchte
        self.benutzer = benutzer
        self.passwort = passwort
        self.max_alter = max_alter
        self.client_id = client_id

        self._sperre = threading.Lock()
        self._temperatur: Optional[float] = None
        self._feuchte: Optional[float] = None
        self._temperatur_zeit: Optional[datetime] = None
        self._feuchte_zeit: Optional[datetime] = None
        self._client = None
        self.verbunden = False
        self.letzter_fehler: Optional[str] = None

    # -- Verbindung ---------------------------------------------------------
    def starten(self) -> bool:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            self.letzter_fehler = "paho-mqtt fehlt: pip install paho-mqtt"
            log.error(self.letzter_fehler)
            return False

        rueckruf = getattr(mqtt, "CallbackAPIVersion", None)
        if rueckruf is not None:
            self._client = mqtt.Client(callback_api_version=rueckruf.VERSION2,
                                       client_id=self.client_id)
        else:
            self._client = mqtt.Client(client_id=self.client_id)

        if self.benutzer:
            self._client.username_pw_set(self.benutzer, self.passwort or "")
        self._client.on_connect = self._bei_verbindung
        self._client.on_message = self._bei_nachricht
        self._client.on_disconnect = self._bei_trennung

        try:
            self._client.connect(self.broker, self.port, keepalive=60)
        except OSError as fehler:
            self.letzter_fehler = f"Broker nicht erreichbar: {fehler}"
            log.error("MQTT-Broker %s:%s nicht erreichbar: %s",
                      self.broker, self.port, fehler)
            return False

        # loop_start hält die Verbindung in einem eigenen Thread offen und
        # verbindet nach einer Störung selbstständig neu.
        self._client.loop_start()
        return True

    def beenden(self) -> None:
        if self._client:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:                            # noqa: BLE001
                pass

    def _bei_verbindung(self, client, userdata, flags, reason_code=0,
                        properties=None, *rest):
        self.verbunden = True
        for thema in (self.thema_temperatur, self.thema_feuchte):
            if thema:
                client.subscribe(thema)
        log.info("MQTT verbunden mit %s:%s, abonniert: %s",
                 self.broker, self.port,
                 ", ".join(t for t in (self.thema_temperatur,
                                       self.thema_feuchte) if t))

    def _bei_trennung(self, client, userdata, *rest):
        self.verbunden = False
        log.warning("MQTT-Verbindung getrennt — es wird selbstständig neu "
                    "verbunden. Bis dahin altern die Werte.")

    # -- Empfang ------------------------------------------------------------
    def _auslesen(self, nutzlast: bytes, feld: Optional[str]) -> Optional[float]:
        text = nutzlast.decode("utf-8", errors="replace").strip()
        if feld:
            try:
                daten = json.loads(text)
            except json.JSONDecodeError:
                return None
            if not isinstance(daten, dict):
                return None
            return _zahl(daten.get(feld))
        return _zahl(text)

    def _bei_nachricht(self, client, userdata, nachricht):
        jetzt = datetime.utcnow()
        with self._sperre:
            if nachricht.topic == self.thema_temperatur:
                wert = self._auslesen(nachricht.payload, self.feld_temperatur)
                if wert is not None:
                    self._temperatur, self._temperatur_zeit = wert, jetzt
            if nachricht.topic == self.thema_feuchte:
                wert = self._auslesen(nachricht.payload, self.feld_feuchte)
                if wert is not None:
                    self._feuchte, self._feuchte_zeit = wert, jetzt

    def werte_setzen(self, temperatur: Optional[float], feuchte: Optional[float],
                     zeitpunkt: Optional[datetime] = None) -> None:
        """Setzt die Werte direkt — für Tests und für Einspeisung von außen."""
        jetzt = zeitpunkt or datetime.utcnow()
        with self._sperre:
            self._temperatur, self._temperatur_zeit = temperatur, jetzt
            self._feuchte, self._feuchte_zeit = feuchte, jetzt

    # -- Abfrage ------------------------------------------------------------
    def abrufen(self, breite=None, laenge=None) -> Optional[Aussenwerte]:
        jetzt = datetime.utcnow()
        with self._sperre:
            t, rh = self._temperatur, self._feuchte
            t_zeit, rh_zeit = self._temperatur_zeit, self._feuchte_zeit

        if t is None or rh is None:
            self.letzter_fehler = "noch kein Wert auf den Themen empfangen"
            return None

        for zeit, was in ((t_zeit, "Temperatur"), (rh_zeit, "Feuchte")):
            if zeit is None or jetzt - zeit > self.max_alter:
                alter = "nie" if zeit is None else f"{(jetzt - zeit)}"
                self.letzter_fehler = (f"{was} zu alt ({alter}) — sendet der "
                                       f"Sensor noch? Batterie prüfen.")
                log.warning("MQTT: %s", self.letzter_fehler)
                return None

        if not _plausibel(t, rh):
            self.letzter_fehler = f"unbrauchbare Werte: {t}, {rh}"
            return None

        self.letzter_fehler = None
        # Zeitstempel des älteren der beiden Werte: Das Paar ist nur so frisch
        # wie sein ältester Teil.
        return Aussenwerte(t, rh, self.name, min(t_zeit, rh_zeit))


def sensor_aus_konfiguration(konfig: dict) -> Optional[Wetterquelle]:
    """Baut genau eine Sensorquelle aus einem Konfigurationsabschnitt.

    Die Art wird über ``typ`` gewählt: ``homeassistant``, ``esphome``,
    ``mqtt`` oder ``json``.
    """
    typ = (konfig.get("typ") or "").strip().lower()

    if typ == "homeassistant":
        return HomeAssistantSensor(
            konfig["url"], konfig["token"],
            konfig["entitaet_temperatur"], konfig["entitaet_feuchte"])

    if typ == "esphome":
        return ESPHomeSensor(
            konfig["url"], konfig["sensor_temperatur"], konfig["sensor_feuchte"])

    if typ == "mqtt":
        sensor = MqttSensor(
            konfig["broker"], int(konfig.get("port", 1883)),
            konfig.get("thema_temperatur", ""), konfig.get("thema_feuchte", ""),
            konfig.get("feld_temperatur"), konfig.get("feld_feuchte"),
            konfig.get("benutzer"), konfig.get("passwort"),
            timedelta(minutes=float(konfig.get("max_alter_minuten", 30))))
        sensor.starten()
        return sensor

    if typ == "json":
        return JsonSensor(
            konfig["url"], konfig.get("pfad_temperatur", "temperature"),
            konfig.get("pfad_feuchte", "humidity"),
            kopfzeilen=konfig.get("kopfzeilen"))

    if typ:
        log.error("Unbekannter Sensortyp %r — bekannt sind homeassistant, "
                  "esphome, mqtt, json", typ)
    return None
