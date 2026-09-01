#!/usr/bin/env python3
"""
Tests der Wetterquellen — gegen die dokumentierte OpenWeatherMap-Antwort.

Die Beispielantwort stammt wörtlich aus der Dokumentation von
``data/2.5/weather``. Ändert OpenWeatherMap die Struktur, schlägt dieser Test
fehl, statt dass der SMART-Modus still auf falschen Werten regelt.

Kein Test hier geht ins Netz. Die HTTP-Schicht wird ersetzt.

Ausführen mit:  python3 -m unittest test_wetter -v
"""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from datetime import datetime, timedelta
from unittest import mock

from wetter import (
    MAX_ALTER, Aussenwerte, FesteWerte, LokalerSensor, OpenWeatherMap,
    Wetterdienst, absolute_feuchte, dienst_aus_konfiguration, taupunkt,
)

#: Wörtlich aus der Dokumentation, units=metric.
OWM_ANTWORT = {
    "coord": {"lon": -2.15, "lat": 57},
    "weather": [{"id": 804, "main": "Clouds",
                 "description": "overcast clouds", "icon": "04d"}],
    "base": "stations",
    "main": {"temp": 8.48, "feels_like": 4.9, "temp_min": 8.18,
             "temp_max": 9.26, "pressure": 1016, "humidity": 79,
             "sea_level": 1016, "grnd_level": 1016},
    "visibility": 10000,
    "wind": {"speed": 7.3, "deg": 189, "gust": 13.48},
    "clouds": {"all": 100},
    "dt": 1647347424,
    "sys": {"type": 2, "id": 2031790, "country": "GB",
            "sunrise": 1647325488, "sunset": 1647367827},
    "timezone": 0, "id": 2641549, "name": "Newtonhill", "cod": 200,
}


def _antwort(nutzdaten: dict):
    """Baut ein Objekt, das sich wie die Antwort von urlopen verhält."""
    inhalt = json.dumps(nutzdaten).encode("utf-8")
    unbeaufsichtigt = mock.MagicMock()
    unbeaufsichtigt.read.return_value = inhalt
    unbeaufsichtigt.__enter__.return_value = unbeaufsichtigt
    unbeaufsichtigt.__exit__.return_value = False
    return unbeaufsichtigt


class TestOpenWeatherMap(unittest.TestCase):
    def setUp(self):
        self.quelle = OpenWeatherMap("test-schluessel")

    def test_dokumentierte_antwort_wird_korrekt_gelesen(self):
        with mock.patch("wetter.urllib.request.urlopen",
                        return_value=_antwort(OWM_ANTWORT)):
            werte = self.quelle.abrufen(57.0, -2.15)
        self.assertIsNotNone(werte)
        self.assertAlmostEqual(werte.temperatur, 8.48, places=2)
        self.assertAlmostEqual(werte.feuchte, 79.0, places=1)
        self.assertEqual(werte.quelle, "OpenWeatherMap")
        self.assertIsNone(self.quelle.letzter_fehler)

    def test_die_abfrage_traegt_die_erwarteten_parameter(self):
        with mock.patch("wetter.urllib.request.urlopen",
                        return_value=_antwort(OWM_ANTWORT)) as aufruf:
            self.quelle.abrufen(46.45, 11.28)
        url = aufruf.call_args[0][0]
        self.assertIn("data/2.5/weather", url)
        self.assertIn("lat=46.45", url)
        self.assertIn("lon=11.28", url)
        self.assertIn("units=metric", url)
        self.assertIn("appid=test-schluessel", url)

    def test_falscher_schluessel_wird_als_dauerhaft_gemeldet(self):
        # Der Fall, der sonst monatelang wie „gerade keine Daten" aussieht.
        fehler = urllib.error.HTTPError("u", 401, "Unauthorized", {},
                                        io.BytesIO(b""))
        with mock.patch("wetter.urllib.request.urlopen", side_effect=fehler):
            with self.assertLogs("ambientika.wetter", level="ERROR") as protokoll:
                werte = self.quelle.abrufen(46.45, 11.28)
        self.assertIsNone(werte)
        self.assertIn("Schlüssel ungültig", self.quelle.letzter_fehler)
        self.assertTrue(any("nicht von selbst weg" in z for z in protokoll.output))

    def test_erschoepftes_kontingent_wird_gemeldet(self):
        fehler = urllib.error.HTTPError("u", 429, "Too Many Requests", {},
                                        io.BytesIO(b""))
        with mock.patch("wetter.urllib.request.urlopen", side_effect=fehler):
            with self.assertLogs("ambientika.wetter", level="ERROR"):
                self.quelle.abrufen(46.45, 11.28)
        self.assertIn("Kontingent", self.quelle.letzter_fehler)

    def test_netzstoerung_wird_nicht_als_dauerfehler_protokolliert(self):
        with mock.patch("wetter.urllib.request.urlopen",
                        side_effect=urllib.error.URLError("Netz weg")):
            werte = self.quelle.abrufen(46.45, 11.28)
        self.assertIsNone(werte)
        self.assertIsNotNone(self.quelle.letzter_fehler)

    def test_haus_ohne_koordinaten_fragt_gar_nicht_erst(self):
        with mock.patch("wetter.urllib.request.urlopen") as aufruf:
            werte = self.quelle.abrufen(None, None)
        aufruf.assert_not_called()
        self.assertIsNone(werte)
        self.assertIn("Koordinaten", self.quelle.letzter_fehler)

    def test_unerwartete_antwortstruktur_wird_abgefangen(self):
        with mock.patch("wetter.urllib.request.urlopen",
                        return_value=_antwort({"cod": 200})):
            werte = self.quelle.abrufen(46.45, 11.28)
        self.assertIsNone(werte)
        self.assertIn("main.temp", self.quelle.letzter_fehler)


class TestLokalerSensor(unittest.TestCase):
    def test_feldnamen_sind_einstellbar(self):
        quelle = LokalerSensor("http://sensor/api", "t", "rh")
        with mock.patch("wetter.urllib.request.urlopen",
                        return_value=_antwort({"t": 4.2, "rh": 88})):
            werte = quelle.abrufen()
        self.assertAlmostEqual(werte.temperatur, 4.2)
        self.assertAlmostEqual(werte.feuchte, 88.0)

    def test_fehlendes_feld_liefert_nichts(self):
        quelle = LokalerSensor("http://sensor/api")
        with mock.patch("wetter.urllib.request.urlopen",
                        return_value=_antwort({"temperature": 4.2})):
            self.assertIsNone(quelle.abrufen())


class TestReihenfolgeUndZwischenspeicher(unittest.TestCase):
    def test_die_erste_antwortende_quelle_gewinnt(self):
        class Stumm(FesteWerte):
            name = "stumm"

            def abrufen(self, breite=None, laenge=None):
                return None

        dienst = Wetterdienst(Stumm(), FesteWerte(5.0, 60.0))
        werte = dienst.holen(46.45, 11.28)
        self.assertEqual(werte.quelle, "fest")
        self.assertAlmostEqual(werte.temperatur, 5.0)

    def test_zweite_abfrage_kommt_aus_dem_zwischenspeicher(self):
        # Zehn Geräte, die unabhängig fragen, dürfen nicht zehn Abrufe auslösen.
        class Zaehlend(FesteWerte):
            def __init__(self):
                super().__init__(3.0, 50.0)
                self.aufrufe = 0

            def abrufen(self, breite=None, laenge=None):
                self.aufrufe += 1
                return super().abrufen(breite, laenge)

        quelle = Zaehlend()
        dienst = Wetterdienst(quelle)
        for _ in range(10):
            dienst.holen(46.45, 11.28)
        self.assertEqual(quelle.aufrufe, 1)

    def test_verschiedene_orte_werden_getrennt_gehalten(self):
        class Zaehlend(FesteWerte):
            def __init__(self):
                super().__init__(3.0, 50.0)
                self.aufrufe = 0

            def abrufen(self, breite=None, laenge=None):
                self.aufrufe += 1
                return super().abrufen(breite, laenge)

        quelle = Zaehlend()
        dienst = Wetterdienst(quelle)
        dienst.holen(46.45, 11.28)
        dienst.holen(48.20, 16.37)
        self.assertEqual(quelle.aufrufe, 2)

    def test_bei_ausfall_gilt_der_letzte_wert_weiter(self):
        class Launisch(FesteWerte):
            def __init__(self):
                super().__init__(7.0, 65.0)
                self.antwortet = True

            def abrufen(self, breite=None, laenge=None):
                return super().abrufen(breite, laenge) if self.antwortet else None

        quelle = Launisch()
        dienst = Wetterdienst(quelle, cache_dauer=timedelta(0))
        dienst.holen(46.45, 11.28)
        quelle.antwortet = False
        werte = dienst.holen(46.45, 11.28)
        self.assertIsNotNone(werte, "eine kurze Störung darf nicht blind machen")
        self.assertAlmostEqual(werte.temperatur, 7.0)

    def test_zu_alte_werte_werden_nicht_mehr_ausgegeben(self):
        # Lieber keine Antwort als eine Regelung auf die Lage von gestern.
        alt = Aussenwerte(7.0, 65.0, "fest",
                          datetime.utcnow() - MAX_ALTER - timedelta(minutes=1))
        self.assertFalse(alt.brauchbar())

    def test_ohne_quelle_gibt_es_nichts(self):
        self.assertIsNone(Wetterdienst().holen(46.45, 11.28))


class TestKonfiguration(unittest.TestCase):
    def test_reihenfolge_sensor_vor_owm_vor_festwert(self):
        dienst = dienst_aus_konfiguration({
            "sensor_url": "http://sensor/api",
            "openweathermap_schluessel": "abc",
            "fester_wert": {"temperatur": 1, "feuchte": 2},
        })
        self.assertEqual([q.name for q in dienst.quellen],
                         ["lokaler Sensor", "OpenWeatherMap", "fest"])

    def test_leere_konfiguration_ergibt_keine_quelle(self):
        self.assertEqual(dienst_aus_konfiguration({}).quellen, [])


class TestFeuchtephysik(unittest.TestCase):
    """Dieselben Stützstellen wie im Home-Assistant-Paket."""

    def test_taupunkt_stuetzstellen(self):
        self.assertAlmostEqual(taupunkt(20, 50), 9.3, delta=0.3)
        self.assertAlmostEqual(taupunkt(25, 60), 16.7, delta=0.3)

    def test_taupunkt_bei_saettigung_gleich_lufttemperatur(self):
        self.assertAlmostEqual(taupunkt(15, 100), 15.0, places=5)

    def test_absolute_feuchte_stuetzstellen(self):
        self.assertAlmostEqual(absolute_feuchte(20, 50), 8.65, delta=0.2)
        self.assertAlmostEqual(absolute_feuchte(28, 60), 16.3, delta=0.3)
        self.assertAlmostEqual(absolute_feuchte(22, 60), 11.6, delta=0.3)

    def test_die_sommerfalle(self):
        # Gleiche relative Feuchte, wärmere Außenluft: Lüften befeuchtet.
        self.assertGreater(absolute_feuchte(28, 60), absolute_feuchte(22, 60))

    def test_unsinnige_eingaben_liefern_nichts(self):
        self.assertIsNone(taupunkt(20, 0))
        self.assertIsNone(taupunkt("warm", 50))
        self.assertIsNone(absolute_feuchte(20, 150))

    def test_werte_rechnen_sich_selbst_aus(self):
        werte = Aussenwerte(22.0, 60.0, "fest", datetime.utcnow())
        self.assertAlmostEqual(werte.absolute_feuchte, 11.6, delta=0.3)
        self.assertAlmostEqual(werte.taupunkt, 14.0, delta=0.4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
