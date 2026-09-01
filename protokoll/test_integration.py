#!/usr/bin/env python3
"""
Integrationstest über die ganze Kette.

Ein echter TCP-Server, ein simuliertes Gerät, die echte REST-Schicht. Geprüft
wird, was zwischen den Bausteinen passiert — genau dort, wo Einzeltests nichts
sehen: Kommt der Status vom Draht bis in die Antwort der App? Landet ein
Moduswechsel als richtiges Byte auf der Leitung? Wird ein unbekannter Rahmen
sichtbar, statt still zu verschwinden?

Ausführen mit:  python3 -m unittest test_integration -v
"""

from __future__ import annotations

import socket
import sys
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "protokoll"))

from api import anwendung_bauen                      # noqa: E402
from auth import Tokendienst                         # noqa: E402
from geraeteserver import Geraeteserver              # noqa: E402
from speicher import Speicher                        # noqa: E402
from wetter import FesteWerte, Wetterdienst          # noqa: E402
from wetterkanal import Wetterkanal, kodierer_aus_mitschnitt  # noqa: E402

SN = "1C9DC2430444"

#: Echter 19-Byte-Statusrahmen aus Issue #5: Nachtmodus, Stufe mittel,
#: 27 °C, 53 % rF, Rolle Master.
STATUS_19 = bytes.fromhex("01001c9dc24304440301011b350000000100" + "03")
FIRMWARE = (bytes([0x03, 0x00]) + bytes.fromhex(SN)
            + bytes([0, 0, 11, 0, 0, 11, 1, 1, 3, 0]))
#: Ein Rahmen mit unbekanntem Typ — so könnte die Wetteranfrage aussehen.
UNBEKANNT = bytes([0x05, 0x00]) + bytes.fromhex(SN) + bytes([0x0A, 0x2D])


class Kette(unittest.TestCase):
    def setUp(self):
        self.speicher = Speicher(":memory:")
        self.wetterkanal = Wetterkanal(Wetterdienst(FesteWerte(6.0, 82.0)))
        # Port 0: Das System vergibt einen freien Port, damit parallele
        # Testläufe sich nicht in die Quere kommen.
        self.server = Geraeteserver(self.speicher, host="127.0.0.1", port=0,
                                    wetterkanal=self.wetterkanal)
        self.server.starten()
        self.port = self.server.gebundener_port

        self.app = anwendung_bauen(self.speicher, Tokendienst(geheimnis="t"),
                                   self.server)
        self.client = TestClient(self.app)

        self.benutzer_id = self.speicher.benutzer_anlegen("k@example.com", "pw")
        self.haus_id = self.speicher.haus_anlegen(
            self.benutzer_id, "Zuhause", "Weg 1", 46.45, 11.28)
        self.raum_id = self.speicher.raum_anlegen(self.haus_id, "Bedroom")
        self.geraet_id = self.speicher.geraet_anlegen(self.raum_id, SN,
                                                      "Schlafzimmer", "Master")
        token = self.client.post("/Users/authenticate", json={
            "username": "k@example.com", "password": "pw"}).json()["jwtToken"]
        self.kopf = {"Authorization": f"Bearer {token}"}

    def tearDown(self):
        self.server.beenden()
        self.speicher.schliessen()

    def _geraet_verbinden(self):
        verbindung = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        verbindung.settimeout(2)
        return verbindung

    def _warten_auf(self, bedingung, sekunden=3.0):
        ende = time.time() + sekunden
        while time.time() < ende:
            if bedingung():
                return True
            time.sleep(0.02)
        return False


class TestStatusVomDrahtBisInDieApp(Kette):
    def test_ein_statusrahmen_erscheint_in_der_rest_antwort(self):
        geraet = self._geraet_verbinden()
        try:
            geraet.sendall(FIRMWARE)
            geraet.sendall(STATUS_19)
            self.assertTrue(self._warten_auf(
                lambda: self.speicher.zustand(SN).gesehen is not None),
                "der Statusrahmen ist nicht angekommen")

            antwort = self.client.get("/Device/device-status",
                                      params={"deviceSerialNumber": SN},
                                      headers=self.kopf)
            self.assertEqual(antwort.status_code, 200)
            daten = antwort.json()
            self.assertEqual(daten["operatingMode"], "Night")
            self.assertEqual(daten["fanSpeed"], "Medium")
            self.assertEqual(daten["temperature"], 27)
            self.assertEqual(daten["humidity"], 53)
            self.assertEqual(daten["deviceRole"], "Master")
        finally:
            geraet.close()

    def test_firmware_wird_uebernommen(self):
        geraet = self._geraet_verbinden()
        try:
            geraet.sendall(FIRMWARE)
            self.assertTrue(self._warten_auf(
                lambda: self.speicher.zustand(SN).radio_fw is not None))
            self.assertEqual(self.speicher.zustand(SN).radio_fw, "0.0.11")
        finally:
            geraet.close()

    def test_die_hausuebersicht_zeigt_das_geraet(self):
        geraet = self._geraet_verbinden()
        try:
            geraet.sendall(FIRMWARE + STATUS_19)
            self.assertTrue(self._warten_auf(
                lambda: self.speicher.zustand(SN).gesehen is not None))
            daten = self.client.get("/Device/house-devices-status",
                                    params={"houseId": self.haus_id},
                                    headers=self.kopf).json()
            self.assertEqual(daten["masterSn"], SN)
            self.assertEqual(daten["uniqueZoneStatusPacket"]["temperature"], 27)
        finally:
            geraet.close()

    def test_trennung_wird_bemerkt(self):
        geraet = self._geraet_verbinden()
        geraet.sendall(FIRMWARE + STATUS_19)
        self.assertTrue(self._warten_auf(lambda: self.server.verbunden(SN)))
        geraet.close()
        self.assertTrue(self._warten_auf(lambda: not self.server.verbunden(SN)),
                        "die Trennung wurde nicht bemerkt")


class TestBefehlBisAufDieLeitung(Kette):
    def test_moduswechsel_erreicht_das_geraet(self):
        geraet = self._geraet_verbinden()
        try:
            geraet.sendall(FIRMWARE + STATUS_19)
            self.assertTrue(self._warten_auf(lambda: self.server.verbunden(SN)))

            antwort = self.client.post("/Device/change-mode", headers=self.kopf,
                                       json={"deviceSerialNumber": SN,
                                             "operatingMode": "Intake",
                                             "fanSpeed": "High",
                                             "humidityLevel": "Normal",
                                             "lightSensorLevel": "Off"})
            self.assertEqual(antwort.status_code, 200)

            empfangen = geraet.recv(64)
            # 13-Byte-Kommando: 02 00 <MAC> 01 <modus><stufe><feuchte><licht>
            self.assertEqual(len(empfangen), 13)
            self.assertEqual(empfangen[0:2], bytes([0x02, 0x00]))
            self.assertEqual(empfangen[2:8].hex().upper(), SN)
            self.assertEqual(empfangen[9], 8)     # Intake
            self.assertEqual(empfangen[10], 2)    # High
            self.assertEqual(empfangen[11], 1)    # Normal
            self.assertEqual(empfangen[12], 1)    # Off
        finally:
            geraet.close()

    def test_wirkungsloser_befehl_geht_nicht_auf_die_leitung(self):
        # Jeder angenommene Befehl piept am Gerät. Ein Befehl, der den
        # bestehenden Zustand wiederholt, ist nachts reiner Lärm.
        geraet = self._geraet_verbinden()
        try:
            geraet.sendall(FIRMWARE + STATUS_19)
            self.assertTrue(self._warten_auf(lambda: self.server.verbunden(SN)))

            # STATUS_19 meldet Night/Medium/Normal, Licht 1.
            antwort = self.client.post("/Device/change-mode", headers=self.kopf,
                                       json={"deviceSerialNumber": SN,
                                             "operatingMode": "Night",
                                             "fanSpeed": "Medium",
                                             "humidityLevel": "Normal",
                                             "lightSensorLevel": "Off"})
            self.assertEqual(antwort.status_code, 200)
            with self.assertRaises(socket.timeout):
                geraet.recv(64)
        finally:
            geraet.close()

    def test_filter_reset_erreicht_das_geraet(self):
        geraet = self._geraet_verbinden()
        try:
            geraet.sendall(FIRMWARE + STATUS_19)
            self.assertTrue(self._warten_auf(lambda: self.server.verbunden(SN)))
            self.client.get("/Device/reset-filter",
                            params={"deviceSerialNumber": SN}, headers=self.kopf)
            empfangen = geraet.recv(64)
            self.assertEqual(len(empfangen), 9)
            self.assertEqual(empfangen[8], 0x03)
        finally:
            geraet.close()


class TestKeinSetupRahmen(Kette):
    """Der Blocker aus dem Konzept: Rollen dürfen nicht überschrieben werden."""

    def test_beim_verbinden_wird_nichts_gesendet(self):
        geraet = self._geraet_verbinden()
        try:
            geraet.sendall(FIRMWARE + STATUS_19)
            self.assertTrue(self._warten_auf(lambda: self.server.verbunden(SN)))
            time.sleep(0.3)
            with self.assertRaises(socket.timeout):
                geraet.recv(64)
        finally:
            geraet.close()

    def test_die_rolle_wird_aus_dem_status_gelesen(self):
        geraet = self._geraet_verbinden()
        try:
            geraet.sendall(FIRMWARE + STATUS_19)
            self.assertTrue(self._warten_auf(
                lambda: self.speicher.zustand(SN).gesehen is not None))
            zeile = self.speicher.geraet_nach_seriennummer(SN)
            self.assertEqual(zeile["rolle"], "Master")
        finally:
            geraet.close()


class TestUnbekannteRahmen(Kette):
    """Die Spur zur noch unbekannten Wetteranfrage."""

    def test_unbekannter_rahmen_wird_sichtbar_statt_verworfen(self):
        geraet = self._geraet_verbinden()
        try:
            geraet.sendall(FIRMWARE + STATUS_19)
            self.assertTrue(self._warten_auf(lambda: self.server.verbunden(SN)))
            geraet.sendall(UNBEKANNT + STATUS_19)
            self.assertTrue(self._warten_auf(
                lambda: self.wetterkanal.statistik.unbekannte_typen),
                "der unbekannte Rahmen wurde nicht bemerkt")
            self.assertIn("0x05", self.wetterkanal.statistik.unbekannte_typen)
        finally:
            geraet.close()

    def test_der_status_danach_kommt_trotzdem_an(self):
        # Ein unbekannter Rahmen darf den Strom nicht aus dem Tritt bringen.
        geraet = self._geraet_verbinden()
        try:
            geraet.sendall(FIRMWARE)
            geraet.sendall(UNBEKANNT + STATUS_19)
            self.assertTrue(self._warten_auf(
                lambda: self.speicher.zustand(SN).gesehen is not None),
                "nach dem unbekannten Rahmen kam kein Status mehr durch")
            self.assertEqual(self.speicher.zustand(SN).temperatur, 27)
        finally:
            geraet.close()

    def test_bei_bestaetigtem_format_wird_geantwortet(self):
        vorlage = bytes([0x05, 0x00]) + bytes.fromhex(SN) + bytes([0x00, 0x00])
        self.wetterkanal.kodierer = kodierer_aus_mitschnitt(0x05, vorlage, 8, 9)
        self.wetterkanal.anfragetyp = 0x05

        geraet = self._geraet_verbinden()
        try:
            geraet.sendall(FIRMWARE + STATUS_19)
            self.assertTrue(self._warten_auf(lambda: self.server.verbunden(SN)))
            geraet.sendall(UNBEKANNT)
            antwort = geraet.recv(64)
            self.assertEqual(antwort[0], 0x05)
            self.assertEqual(antwort[8], 6)      # 6 °C
            self.assertEqual(antwort[9], 82)     # 82 %
            self.assertEqual(self.wetterkanal.statistik.beantwortet, 1)
        finally:
            geraet.close()


class TestFreigabeliste(unittest.TestCase):
    def test_unbekannte_seriennummer_wird_abgewiesen(self):
        speicher = Speicher(":memory:")
        server = Geraeteserver(speicher, host="127.0.0.1", port=0,
                               erlaubte_serien={"AABBCCDDEEFF"})
        server.starten()
        try:
            geraet = socket.create_connection(
                ("127.0.0.1", server.gebundener_port), timeout=5)
            geraet.settimeout(3)
            geraet.sendall(STATUS_19)
            # Der Server beendet die Verbindung; recv liefert dann nichts mehr.
            ende = time.time() + 3
            geschlossen = False
            while time.time() < ende:
                try:
                    if geraet.recv(64) == b"":
                        geschlossen = True
                        break
                except socket.timeout:
                    break
                except OSError:
                    geschlossen = True
                    break
            self.assertTrue(geschlossen or not server.verbunden(SN))
            self.assertFalse(server.verbunden(SN))
            geraet.close()
        finally:
            server.beenden()
            speicher.schliessen()


class TestBeobachtungsmodus(unittest.TestCase):
    def test_im_beobachtungsmodus_wird_nichts_gesendet(self):
        speicher = Speicher(":memory:")
        server = Geraeteserver(speicher, host="127.0.0.1", port=0,
                               nur_beobachten=True)
        server.starten()
        try:
            benutzer = speicher.benutzer_anlegen("b@example.com", "pw")
            haus = speicher.haus_anlegen(benutzer, "H")
            raum = speicher.raum_anlegen(haus, "Bedroom")
            speicher.geraet_anlegen(raum, SN, "G", "Master")

            app = anwendung_bauen(speicher, Tokendienst(geheimnis="t"), server)
            client = TestClient(app)
            token = client.post("/Users/authenticate", json={
                "username": "b@example.com", "password": "pw"}).json()["jwtToken"]

            geraet = socket.create_connection(
                ("127.0.0.1", server.gebundener_port), timeout=5)
            geraet.settimeout(1.5)
            geraet.sendall(FIRMWARE + STATUS_19)
            ende = time.time() + 3
            while time.time() < ende and not server.verbunden(SN):
                time.sleep(0.02)

            antwort = client.post(
                "/Device/change-mode",
                headers={"Authorization": f"Bearer {token}"},
                json={"deviceSerialNumber": SN, "operatingMode": "Off"})
            # Der Aufruf scheitert, weil nichts zugestellt werden konnte —
            # und auf der Leitung kommt nichts an.
            self.assertIn(antwort.status_code, (200, 502))
            with self.assertRaises(socket.timeout):
                geraet.recv(64)
            geraet.close()
        finally:
            server.beenden()
            speicher.schliessen()


if __name__ == "__main__":
    unittest.main(verbosity=2)
