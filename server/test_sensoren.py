#!/usr/bin/env python3
"""
Tests der Außensensor-Anbindungen.

Der rote Faden: **Ein Sensor, der nicht antwortet, liefert None — nie einen
erfundenen Wert.** Ein erfundener Außenwert wäre schlimmer als gar keiner,
weil die Lüftung dann auf eine Messung reagiert, die es nicht gibt.

Kein Test geht ins Netz.

Ausführen mit:  python3 -m unittest test_sensoren -v
"""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from datetime import datetime, timedelta
from unittest import mock

from sensoren import (
    ESPHomeSensor, HomeAssistantSensor, JsonSensor, MqttSensor, _plausibel,
    _zahl, sensor_aus_konfiguration,
)


def _antwort(nutzdaten):
    inhalt = json.dumps(nutzdaten).encode("utf-8")
    objekt = mock.MagicMock()
    objekt.read.return_value = inhalt
    objekt.__enter__.return_value = objekt
    objekt.__exit__.return_value = False
    return objekt


class TestWertumwandlung(unittest.TestCase):
    def test_zahlen_und_zeichenketten(self):
        self.assertEqual(_zahl(12.3), 12.3)
        self.assertEqual(_zahl("12.3"), 12.3)
        self.assertEqual(_zahl("12,3"), 12.3)      # deutsches Komma
        self.assertEqual(_zahl(" 8 "), 8.0)

    def test_home_assistant_platzhalter_sind_kein_wert(self):
        for platzhalter in ("unknown", "unavailable", "none", "", "NaN"):
            with self.subTest(wert=platzhalter):
                self.assertIsNone(_zahl(platzhalter))

    def test_wahrheitswerte_werden_nicht_zu_zahlen(self):
        # True würde sonst als 1.0 durchgehen und wie ein Messwert aussehen.
        self.assertIsNone(_zahl(True))
        self.assertIsNone(_zahl(False))

    def test_unendlich_und_nan_werden_abgewiesen(self):
        self.assertIsNone(_zahl(float("inf")))
        self.assertIsNone(_zahl(float("nan")))

    def test_plausibilitaetsgrenzen(self):
        self.assertTrue(_plausibel(-15.0, 85.0))
        self.assertTrue(_plausibel(35.0, 20.0))
        self.assertFalse(_plausibel(-80.0, 50.0))
        self.assertFalse(_plausibel(20.0, 150.0))
        self.assertFalse(_plausibel(None, 50.0))

    def test_vertauschte_felder_fallen_auf(self):
        # Temperatur und Feuchte verwechselt: 85 °C gibt es draußen nicht.
        self.assertFalse(_plausibel(85.0, 12.0))


class TestHomeAssistant(unittest.TestCase):
    def setUp(self):
        self.sensor = HomeAssistantSensor(
            "http://homeassistant.local:8123", "token123",
            "sensor.aussen_temperatur", "sensor.aussen_feuchte")

    def test_werte_werden_gelesen(self):
        antworten = [_antwort({"state": "4.7"}), _antwort({"state": "88"})]
        with mock.patch("sensoren.urllib.request.urlopen", side_effect=antworten):
            werte = self.sensor.abrufen()
        self.assertAlmostEqual(werte.temperatur, 4.7)
        self.assertAlmostEqual(werte.feuchte, 88.0)
        self.assertEqual(werte.quelle, "Home Assistant")

    def test_token_wird_mitgeschickt(self):
        antworten = [_antwort({"state": "4.7"}), _antwort({"state": "88"})]
        with mock.patch("sensoren.urllib.request.urlopen",
                        side_effect=antworten) as aufruf:
            self.sensor.abrufen()
        anfrage = aufruf.call_args_list[0][0][0]
        self.assertEqual(anfrage.headers["Authorization"], "Bearer token123")
        self.assertIn("/api/states/sensor.aussen_temperatur", anfrage.full_url)

    def test_unavailable_liefert_keinen_wert(self):
        antworten = [_antwort({"state": "unavailable"}), _antwort({"state": "88"})]
        with mock.patch("sensoren.urllib.request.urlopen", side_effect=antworten):
            self.assertIsNone(self.sensor.abrufen())

    def test_abgelehntes_token_wird_deutlich_gemeldet(self):
        fehler = urllib.error.HTTPError("u", 401, "Unauthorized", {},
                                        io.BytesIO(b""))
        with mock.patch("sensoren.urllib.request.urlopen", side_effect=fehler):
            with self.assertLogs("ambientika.sensoren", level="ERROR"):
                self.assertIsNone(self.sensor.abrufen())
        self.assertIn("401", self.sensor.letzter_fehler)

    def test_home_assistant_nicht_erreichbar(self):
        with mock.patch("sensoren.urllib.request.urlopen",
                        side_effect=urllib.error.URLError("weg")):
            self.assertIsNone(self.sensor.abrufen())


class TestESPHome(unittest.TestCase):
    def setUp(self):
        self.sensor = ESPHomeSensor("http://aussen.local", "aussentemperatur",
                                    "aussenfeuchte")

    def test_value_wird_gegenueber_state_bevorzugt(self):
        # 'state' enthält "12.3 °C", 'value' die reine Zahl.
        antworten = [_antwort({"state": "12.3 °C", "value": 12.3}),
                     _antwort({"state": "88.0 %", "value": 88.0})]
        with mock.patch("sensoren.urllib.request.urlopen", side_effect=antworten):
            werte = self.sensor.abrufen()
        self.assertAlmostEqual(werte.temperatur, 12.3)
        self.assertAlmostEqual(werte.feuchte, 88.0)

    def test_ohne_value_wird_state_genommen(self):
        antworten = [_antwort({"state": "12.3"}), _antwort({"state": "88"})]
        with mock.patch("sensoren.urllib.request.urlopen", side_effect=antworten):
            self.assertIsNotNone(self.sensor.abrufen())

    def test_pfad_stimmt(self):
        antworten = [_antwort({"value": 1}), _antwort({"value": 50})]
        with mock.patch("sensoren.urllib.request.urlopen",
                        side_effect=antworten) as aufruf:
            self.sensor.abrufen()
        self.assertIn("/sensor/aussentemperatur",
                      aufruf.call_args_list[0][0][0].full_url)


class TestJsonSensor(unittest.TestCase):
    def test_verschachtelte_felder(self):
        sensor = JsonSensor("http://sensor/api", "daten.aussen.temp",
                            "daten.aussen.rh")
        nutzdaten = {"daten": {"aussen": {"temp": 3.5, "rh": 91}}}
        with mock.patch("sensoren.urllib.request.urlopen",
                        return_value=_antwort(nutzdaten)):
            werte = sensor.abrufen()
        self.assertAlmostEqual(werte.temperatur, 3.5)
        self.assertAlmostEqual(werte.feuchte, 91.0)

    def test_listenindex_im_pfad(self):
        sensor = JsonSensor("http://sensor/api", "werte.0.t", "werte.0.rh")
        with mock.patch("sensoren.urllib.request.urlopen",
                        return_value=_antwort({"werte": [{"t": 9, "rh": 70}]})):
            self.assertAlmostEqual(sensor.abrufen().temperatur, 9.0)

    def test_fehlender_pfad_liefert_nichts(self):
        sensor = JsonSensor("http://sensor/api", "gibt.es.nicht", "auch.nicht")
        with mock.patch("sensoren.urllib.request.urlopen",
                        return_value=_antwort({"temperature": 5})):
            self.assertIsNone(sensor.abrufen())


class TestMqtt(unittest.TestCase):
    """Der Fall, in dem die meisten Funksensoren landen."""

    def setUp(self):
        self.sensor = MqttSensor("192.168.1.10",
                                 thema_temperatur="aussen/temperatur",
                                 thema_feuchte="aussen/feuchte")

    def _nachricht(self, thema, nutzlast):
        objekt = mock.MagicMock()
        objekt.topic = thema
        objekt.payload = nutzlast
        return objekt

    def test_nackte_zahlen_als_nutzlast(self):
        self.sensor._bei_nachricht(None, None,
                                   self._nachricht("aussen/temperatur", b"6.4"))
        self.sensor._bei_nachricht(None, None,
                                   self._nachricht("aussen/feuchte", b"82"))
        werte = self.sensor.abrufen()
        self.assertAlmostEqual(werte.temperatur, 6.4)
        self.assertAlmostEqual(werte.feuchte, 82.0)

    def test_zigbee2mqtt_sendet_ein_objekt(self):
        sensor = MqttSensor("broker", thema_temperatur="zigbee2mqtt/aussen",
                            thema_feuchte="zigbee2mqtt/aussen",
                            feld_temperatur="temperature",
                            feld_feuchte="humidity")
        nutzlast = json.dumps({"temperature": 2.1, "humidity": 93,
                               "battery": 87}).encode()
        sensor._bei_nachricht(None, None,
                              self._nachricht("zigbee2mqtt/aussen", nutzlast))
        werte = sensor.abrufen()
        self.assertAlmostEqual(werte.temperatur, 2.1)
        self.assertAlmostEqual(werte.feuchte, 93.0)

    def test_ohne_empfang_gibt_es_keinen_wert(self):
        self.assertIsNone(self.sensor.abrufen())
        self.assertIn("noch kein Wert", self.sensor.letzter_fehler)

    def test_veraltete_werte_werden_verworfen(self):
        # Der Fall leere Batterie: Der Sensor hört auf zu senden, und ohne
        # Verfallszeit würde die Lüftung wochenlang auf den letzten Wert regeln.
        self.sensor.werte_setzen(6.4, 82.0,
                                 datetime.utcnow() - timedelta(hours=2))
        with self.assertLogs("ambientika.sensoren", level="WARNING"):
            self.assertIsNone(self.sensor.abrufen())
        self.assertIn("zu alt", self.sensor.letzter_fehler)

    def test_frische_werte_werden_geliefert(self):
        self.sensor.werte_setzen(6.4, 82.0,
                                 datetime.utcnow() - timedelta(minutes=5))
        self.assertIsNotNone(self.sensor.abrufen())

    def test_unbrauchbare_nutzlast_ueberschreibt_nichts(self):
        self.sensor.werte_setzen(6.4, 82.0)
        self.sensor._bei_nachricht(None, None,
                                   self._nachricht("aussen/temperatur", b"kaputt"))
        self.assertAlmostEqual(self.sensor.abrufen().temperatur, 6.4)

    def test_zeitstempel_ist_der_aeltere_der_beiden(self):
        # Das Wertepaar ist nur so frisch wie sein ältester Teil.
        jetzt = datetime.utcnow()
        with self.sensor._sperre:
            self.sensor._temperatur, self.sensor._temperatur_zeit = 5.0, jetzt
            self.sensor._feuchte = 80.0
            self.sensor._feuchte_zeit = jetzt - timedelta(minutes=10)
        self.assertEqual(self.sensor.abrufen().zeitpunkt,
                         jetzt - timedelta(minutes=10))

    def test_fehlendes_paho_wird_gemeldet_statt_zu_stuerzen(self):
        # Ein None-Eintrag in sys.modules lässt genau diesen einen Import
        # scheitern, ohne die Importe von unittest selbst zu stören.
        with self.assertLogs("ambientika.sensoren", level="ERROR") as protokoll:
            with mock.patch.dict("sys.modules", {"paho.mqtt.client": None}):
                erfolg = self.sensor.starten()
        self.assertFalse(erfolg)
        self.assertIn("paho-mqtt", self.sensor.letzter_fehler)
        self.assertTrue(any("pip install" in z for z in protokoll.output))


class TestKonfiguration(unittest.TestCase):
    def test_home_assistant(self):
        sensor = sensor_aus_konfiguration({
            "typ": "homeassistant", "url": "http://ha:8123", "token": "t",
            "entitaet_temperatur": "sensor.a", "entitaet_feuchte": "sensor.b"})
        self.assertIsInstance(sensor, HomeAssistantSensor)

    def test_esphome(self):
        sensor = sensor_aus_konfiguration({
            "typ": "esphome", "url": "http://esp", "sensor_temperatur": "t",
            "sensor_feuchte": "rh"})
        self.assertIsInstance(sensor, ESPHomeSensor)

    def test_json(self):
        sensor = sensor_aus_konfiguration({"typ": "json", "url": "http://x"})
        self.assertIsInstance(sensor, JsonSensor)

    def test_unbekannter_typ_wird_gemeldet(self):
        with self.assertLogs("ambientika.sensoren", level="ERROR"):
            self.assertIsNone(sensor_aus_konfiguration({"typ": "telepathie"}))

    def test_leere_konfiguration(self):
        self.assertIsNone(sensor_aus_konfiguration({}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
