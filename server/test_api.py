#!/usr/bin/env python3
"""
Tests der REST-Schicht gegen den Vertrag der Cloud-API.

Der Maßstab ist nicht „funktioniert irgendwie", sondern „die bestehende App
kann es lesen". Deshalb prüfen diese Tests Feldnamen und Enum-Schreibweisen
buchstabengetreu — inklusive des Tippfehlers ``signalStrenght``, der so in der
Original-API steht.

Ausführen mit:  python3 -m unittest test_api -v
"""

from __future__ import annotations

import unittest
from datetime import datetime

from fastapi.testclient import TestClient

from api import Geraetebus, anwendung_bauen
from auth import Tokendienst
from modelle import (
    DeviceRole, FanSpeed, HumidityLevel, OperatingMode,
    luftguete_von_protokoll, modus_von_protokoll, zu_protokoll,
)
from speicher import Speicher


class BusAttrappe(Geraetebus):
    """Merkt sich, was verlangt wurde, statt Bytes zu verschicken."""

    def __init__(self, verbundene=None):
        self.befehle = []
        self.filter_resets = []
        self.konfigurationen = []
        self.verbundene = set(verbundene or [])
        self.antwort = True

    def modus_setzen(self, seriennummer, modus, stufe, feuchte, licht):
        self.befehle.append((seriennummer, modus, stufe, feuchte, licht))
        return self.antwort

    def filter_zuruecksetzen(self, seriennummer):
        self.filter_resets.append(seriennummer)
        return self.antwort

    def konfiguration_senden(self, seriennummer):
        self.konfigurationen.append(seriennummer)
        return self.antwort

    def verbunden(self, seriennummer):
        return seriennummer.upper() in self.verbundene


SN = "1C9DC2430444"
SN2 = "AABBCCDDEEFF"


class ApiBasis(unittest.TestCase):
    def setUp(self):
        self.speicher = Speicher(":memory:")
        self.tokendienst = Tokendienst(geheimnis="test-geheimnis")
        self.bus = BusAttrappe(verbundene=[SN, SN2])
        self.app = anwendung_bauen(self.speicher, self.tokendienst, self.bus)
        self.client = TestClient(self.app)

        self.benutzer_id = self.speicher.benutzer_anlegen(
            "kunde@example.com", "geheim123", "Tobias", "Mock")
        self.haus_id = self.speicher.haus_anlegen(
            self.benutzer_id, "Zuhause", "Musterweg 1", 46.45, 11.28, 60)
        self.raum_id = self.speicher.raum_anlegen(self.haus_id, "Bedroom")
        self.geraet_id = self.speicher.geraet_anlegen(
            self.raum_id, SN, "Schlafzimmer", rolle="Master")

        antwort = self.client.post("/Users/authenticate", json={
            "username": "kunde@example.com", "password": "geheim123"})
        self.token = antwort.json()["jwtToken"]
        self.kopf = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self):
        self.speicher.schliessen()

    def _status_setzen(self, sn=SN, **felder):
        zustand = self.speicher.zustand(sn)
        for k, v in felder.items():
            setattr(zustand, k, v)
        zustand.gesehen = datetime.utcnow()
        zustand.online = True
        return zustand


class TestAnmeldung(ApiBasis):
    def test_anmeldung_liefert_die_erwarteten_felder(self):
        antwort = self.client.post("/Users/authenticate", json={
            "username": "kunde@example.com", "password": "geheim123"})
        self.assertEqual(antwort.status_code, 200)
        daten = antwort.json()
        for feld in ("id", "firstName", "lastName", "completeName", "username",
                     "jwtToken", "expiresAt", "userLevel"):
            self.assertIn(feld, daten, f"Feld {feld} fehlt")
        self.assertEqual(daten["completeName"], "Tobias Mock")

    def test_falsches_passwort_wird_abgewiesen(self):
        antwort = self.client.post("/Users/authenticate", json={
            "username": "kunde@example.com", "password": "falsch"})
        self.assertEqual(antwort.status_code, 401)

    def test_unbekanntes_konto_gibt_dieselbe_meldung(self):
        # Sonst verrät die Antwort, welche Konten existieren.
        a = self.client.post("/Users/authenticate", json={
            "username": "gibtsnicht@example.com", "password": "x"})
        b = self.client.post("/Users/authenticate", json={
            "username": "kunde@example.com", "password": "falsch"})
        self.assertEqual(a.status_code, b.status_code)
        self.assertEqual(a.json()["detail"], b.json()["detail"])

    def test_ohne_token_kein_zugriff(self):
        self.assertEqual(self.client.get("/House/houses").status_code, 401)

    def test_manipuliertes_token_wird_abgewiesen(self):
        kopf = {"Authorization": f"Bearer {self.token[:-4]}xxxx"}
        self.assertEqual(self.client.get("/House/houses", headers=kopf).status_code,
                         401)

    def test_token_erneuern(self):
        antwort = self.client.get("/Users/refresh-token", headers=self.kopf)
        self.assertEqual(antwort.status_code, 200)
        self.assertIn("jwtToken", antwort.json())

    def test_feature_flags_sind_ohne_anmeldung_lesbar(self):
        antwort = self.client.get("/Users/feature-flags")
        self.assertEqual(antwort.status_code, 200)
        for feld in ("rememberMeLogin", "resetDeviceEndpoint", "weeklyScheduler",
                     "improvedRoomList", "turboMode"):
            self.assertIn(feld, antwort.json())

    def test_wochenzeitplan_ist_freigeschaltet(self):
        # Ohne dieses Flag blendet die App den Zeitplan aus.
        self.assertTrue(self.client.get("/Users/feature-flags")
                        .json()["weeklyScheduler"])


class TestHaus(ApiBasis):
    def test_haeuser_auflisten(self):
        daten = self.client.get("/House/houses", headers=self.kopf).json()
        self.assertEqual(len(daten), 1)
        self.assertEqual(daten[0]["name"], "Zuhause")
        self.assertTrue(daten[0]["hasDevices"])

    def test_koordinaten_werden_gespeichert(self):
        # Sie sind die Grundlage der Wetterabfrage für den SMART-Modus.
        haus = self.client.get("/House/house-complete-info",
                               params={"houseId": self.haus_id},
                               headers=self.kopf).json()
        self.assertAlmostEqual(haus["latitude"], 46.45, places=2)
        self.assertAlmostEqual(haus["longitude"], 11.28, places=2)

    def test_fremdes_haus_bleibt_unsichtbar(self):
        fremd = self.speicher.benutzer_anlegen("fremd@example.com", "pw")
        fremdes_haus = self.speicher.haus_anlegen(fremd, "Nicht meins")
        antwort = self.client.get("/House/house-complete-info",
                                  params={"houseId": fremdes_haus},
                                  headers=self.kopf)
        self.assertEqual(antwort.status_code, 404)

    def test_haus_anlegen_und_umbenennen(self):
        neu = self.client.post("/House/add-house",
                               json={"name": "Ferienhaus"},
                               headers=self.kopf).json()
        self.assertEqual(neu["name"], "Ferienhaus")
        self.client.post("/House/rename-house",
                         json={"houseId": neu["id"], "newName": "Berghütte"},
                         headers=self.kopf)
        haeuser = self.client.get("/House/houses", headers=self.kopf).json()
        self.assertIn("Berghütte", [h["name"] for h in haeuser])

    def test_raum_traegt_den_geraetezaehler(self):
        raeume = self.client.get("/House/user-rooms",
                                 params={"houseId": self.haus_id},
                                 headers=self.kopf).json()
        self.assertEqual(raeume[0]["roomDevicesCount"], 1)

    def test_freie_raeume_sind_die_ohne_zone(self):
        frei = self.client.get("/House/user-free-rooms",
                               params={"houseId": self.haus_id},
                               headers=self.kopf).json()
        self.assertEqual(len(frei), 1)
        zone_id = self.speicher.zone_anlegen(self.haus_id, "Obergeschoss")
        self.speicher.raum_zone_zuweisen(self.raum_id, zone_id)
        frei = self.client.get("/House/user-free-rooms",
                               params={"houseId": self.haus_id},
                               headers=self.kopf).json()
        self.assertEqual(frei, [])


class TestGeraetestatus(ApiBasis):
    def test_ohne_statuspaket_gibt_es_404(self):
        # Die Cloud verhält sich genauso; die App liest das als „noch nicht bereit".
        antwort = self.client.get("/Device/device-status",
                                  params={"deviceSerialNumber": SN},
                                  headers=self.kopf)
        self.assertEqual(antwort.status_code, 404)

    def test_statuspaket_traegt_alle_erwarteten_felder(self):
        self._status_setzen(modus_code=3, stufe_code=1, feuchte_code=1,
                            temperatur=21, feuchte=53, luftguete_roh=2,
                            rolle_code=0, signalstaerke=-58)
        daten = self.client.get("/Device/device-status",
                                params={"deviceSerialNumber": SN},
                                headers=self.kopf).json()
        for feld in ("packetType", "deviceSerialNumber", "operatingMode",
                     "fanSpeed", "humidityLevel", "temperature", "humidity",
                     "airQuality", "humidityAlarm", "filtersStatus",
                     "nightAlarm", "deviceRole", "lastOperatingMode",
                     "lightSensorLevel", "signalStrenght", "isScheduled",
                     "isTurboAvailable"):
            self.assertIn(feld, daten, f"Feld {feld} fehlt")

    def test_der_tippfehler_im_feldnamen_bleibt_erhalten(self):
        # signalStrenght statt signalStrength — so heißt es in der Original-API.
        self._status_setzen(signalstaerke=-58)
        daten = self.client.get("/Device/device-status",
                                params={"deviceSerialNumber": SN},
                                headers=self.kopf).json()
        self.assertIn("signalStrenght", daten)
        self.assertNotIn("signalStrength", daten)
        self.assertEqual(daten["signalStrenght"], -58)

    def test_enums_gehen_als_zeichenketten_ueber_die_leitung(self):
        self._status_setzen(modus_code=3, stufe_code=1)
        daten = self.client.get("/Device/device-status",
                                params={"deviceSerialNumber": SN},
                                headers=self.kopf).json()
        self.assertEqual(daten["operatingMode"], "Night")
        self.assertEqual(daten["fanSpeed"], "Medium")
        self.assertIsInstance(daten["operatingMode"], str)

    def test_nicht_bereiter_luftguetesensor_wird_nicht_als_bestwert_gemeldet(self):
        self._status_setzen(luftguete_roh=0)
        daten = self.client.get("/Device/device-status",
                                params={"deviceSerialNumber": SN},
                                headers=self.kopf).json()
        self.assertNotEqual(daten["airQuality"], "VeryGood")

    def test_unbekanntes_geraet_gibt_404(self):
        antwort = self.client.get("/Device/device-status",
                                  params={"deviceSerialNumber": "000000000000"},
                                  headers=self.kopf)
        self.assertEqual(antwort.status_code, 404)


class TestHausuebersicht(ApiBasis):
    """Der Endpunkt, an dem die Startseite der App hängt."""

    def test_haus_ohne_zonen_nutzt_den_einzelzonen_zweig(self):
        self._status_setzen(modus_code=1, temperatur=22)
        daten = self.client.get("/Device/house-devices-status",
                                params={"houseId": self.haus_id},
                                headers=self.kopf).json()
        self.assertIsNotNone(daten["uniqueZoneStatusPacket"])
        self.assertEqual(daten["uniqueZoneDevicesCount"], 1)
        self.assertEqual(daten["masterSn"], SN)
        self.assertIsNone(daten["zoneDevicesInfo"])

    def test_haus_mit_zonen_nutzt_den_zonenzweig(self):
        zone_id = self.speicher.zone_anlegen(self.haus_id, "Obergeschoss")
        self.speicher.raum_zone_zuweisen(self.raum_id, zone_id)
        self._status_setzen(modus_code=1)
        daten = self.client.get("/Device/house-devices-status",
                                params={"houseId": self.haus_id},
                                headers=self.kopf).json()
        self.assertIsNotNone(daten["zoneDevicesInfo"])
        self.assertEqual(len(daten["zoneDevicesInfo"]), 1)
        self.assertEqual(daten["zoneDevicesInfo"][0]["masterSn"], SN)
        self.assertIsNone(daten["uniqueZoneStatusPacket"])

    def test_haus_ohne_geraete_liefert_eine_leere_aber_gueltige_antwort(self):
        leeres_haus = self.speicher.haus_anlegen(self.benutzer_id, "Leer")
        daten = self.client.get("/Device/house-devices-status",
                                params={"houseId": leeres_haus},
                                headers=self.kopf).json()
        self.assertEqual(daten["uniqueZoneDevicesCount"], 0)

    def test_der_master_wird_als_zonenvertreter_gewaehlt(self):
        zone_id = self.speicher.zone_anlegen(self.haus_id, "Zone")
        self.speicher.raum_zone_zuweisen(self.raum_id, zone_id)
        raum2 = self.speicher.raum_anlegen(self.haus_id, "Bathroom", zone_id)
        self.speicher.geraet_anlegen(raum2, SN2, "Bad",
                                     rolle="SlaveOppositeMaster")
        self._status_setzen()
        daten = self.client.get("/Device/house-devices-status",
                                params={"houseId": self.haus_id},
                                headers=self.kopf).json()
        info = daten["zoneDevicesInfo"][0]
        self.assertEqual(info["masterSn"], SN)
        self.assertEqual(info["zoneDevicesCount"], 2)


class TestSteuerung(ApiBasis):
    def test_moduswechsel_erreicht_den_bus(self):
        antwort = self.client.post("/Device/change-mode", headers=self.kopf, json={
            "deviceSerialNumber": SN, "operatingMode": "Night",
            "fanSpeed": "Medium", "humidityLevel": "Normal",
            "lightSensorLevel": "Off", "isScheduleMode": False})
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(self.bus.befehle, [(SN, 3, 1, 1, 1)])

    def test_zeichenketten_werden_in_protokollcodes_uebersetzt(self):
        self.client.post("/Device/change-mode", headers=self.kopf, json={
            "deviceSerialNumber": SN, "operatingMode": "Intake",
            "fanSpeed": "High", "humidityLevel": "Moist",
            "lightSensorLevel": "Medium"})
        sn, modus, stufe, feuchte, licht = self.bus.befehle[0]
        self.assertEqual(modus, 8)      # Intake
        self.assertEqual(stufe, 2)      # High
        self.assertEqual(feuchte, 2)    # Moist
        self.assertEqual(licht, 3)      # Medium

    def test_fehlender_modus_faellt_wie_in_der_cloud_auf_smart(self):
        # OperatingMode.Smart hat den Ordinalwert 0 und ist damit der
        # C#-Standardwert eines nicht gesetzten Feldes.
        self.client.post("/Device/change-mode", headers=self.kopf,
                         json={"deviceSerialNumber": SN})
        self.assertEqual(self.bus.befehle[0][1], 0)

    def test_nicht_verbundenes_geraet_meldet_503(self):
        self.bus.verbundene.discard(SN)
        antwort = self.client.post("/Device/change-mode", headers=self.kopf,
                                   json={"deviceSerialNumber": SN,
                                         "operatingMode": "Off"})
        self.assertEqual(antwort.status_code, 503)

    def test_unbekanntes_geraet_wird_nicht_geschaltet(self):
        antwort = self.client.post("/Device/change-mode", headers=self.kopf,
                                   json={"deviceSerialNumber": "000000000000",
                                         "operatingMode": "Off"})
        self.assertEqual(antwort.status_code, 404)
        self.assertEqual(self.bus.befehle, [])

    def test_filter_reset(self):
        antwort = self.client.get("/Device/reset-filter",
                                  params={"deviceSerialNumber": SN},
                                  headers=self.kopf)
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(self.bus.filter_resets, [SN])

    def test_send_device_config_nutzt_den_abweichenden_parameternamen(self):
        # Die Original-API nennt den Parameter hier deviceSn statt
        # deviceSerialNumber. Diese Inkonsistenz muss nachgebildet werden.
        antwort = self.client.get("/Device/send-device-config",
                                  params={"deviceSn": SN}, headers=self.kopf)
        self.assertEqual(antwort.status_code, 200)
        self.assertEqual(self.bus.konfigurationen, [SN])


class TestKopplungNichtVerfuegbar(ApiBasis):
    """Was ohne das Herstellerverfahren nicht geht, sagt es auch."""

    def test_betroffene_endpunkte_antworten_mit_501(self):
        faelle = [
            ("post", "/House/add-device-room"),
            ("get", "/House/device-info"),
            ("get", "/House/house-config-auto"),
            ("post", "/Device/apply-config"),
            ("post", "/Device/apply-config-force-unique"),
        ]
        for methode, pfad in faelle:
            with self.subTest(pfad=pfad):
                antwort = getattr(self.client, methode)(pfad, headers=self.kopf)
                self.assertEqual(antwort.status_code, 501)
                self.assertIn("encryptedDeviceInfo", antwort.json()["detail"])

    def test_die_meldung_nennt_den_weg(self):
        antwort = self.client.post("/House/add-device-room", headers=self.kopf)
        text = antwort.json()["detail"]
        self.assertIn("Südwind-Server", text)


class TestWochenzeitplan(ApiBasis):
    def test_leerer_zeitplan(self):
        daten = self.client.get(f"/Schedule/{self.geraet_id}",
                                headers=self.kopf).json()
        self.assertEqual(daten["deviceId"], self.geraet_id)
        self.assertIsNone(daten["timeSlots"])

    def test_zeitfenster_anlegen_lesen_aendern_loeschen(self):
        fenster = {"id": 0, "dayOfWeek": "Monday", "startTime": "22:00:00",
                   "endTime": "06:30:00", "operatingMode": "Night",
                   "fanSpeed": "Low", "humidityLevel": "Normal",
                   "lightSensorLevel": "Off", "scheduleId": 0}
        angelegt = self.client.post(f"/Schedule/{self.geraet_id}/timeslots",
                                    json=fenster, headers=self.kopf).json()
        self.assertGreater(angelegt["id"], 0)

        gelesen = self.client.get(f"/Schedule/{self.geraet_id}",
                                  headers=self.kopf).json()
        self.assertEqual(len(gelesen["timeSlots"]), 1)
        self.assertEqual(gelesen["timeSlots"][0]["operatingMode"], "Night")

        geaendert = dict(fenster, id=angelegt["id"], fanSpeed="Medium")
        antwort = self.client.put(
            f"/Schedule/{self.geraet_id}/timeslots/{angelegt['id']}",
            json=geaendert, headers=self.kopf)
        self.assertEqual(antwort.status_code, 200)
        gelesen = self.client.get(f"/Schedule/{self.geraet_id}",
                                  headers=self.kopf).json()
        self.assertEqual(gelesen["timeSlots"][0]["fanSpeed"], "Medium")

        antwort = self.client.delete(
            f"/Schedule/{self.geraet_id}/timeslots/{angelegt['id']}",
            headers=self.kopf)
        self.assertEqual(antwort.status_code, 200)
        gelesen = self.client.get(f"/Schedule/{self.geraet_id}",
                                  headers=self.kopf).json()
        self.assertIsNone(gelesen["timeSlots"])

    def test_zeitangaben_bleiben_im_dotnet_format(self):
        fenster = {"id": 0, "dayOfWeek": "Saturday", "startTime": "08:30:00",
                   "endTime": "12:00:00", "operatingMode": "Auto",
                   "fanSpeed": "Low", "humidityLevel": "Normal",
                   "lightSensorLevel": "NotAvailable", "scheduleId": 0}
        self.client.post(f"/Schedule/{self.geraet_id}/timeslots",
                         json=fenster, headers=self.kopf)
        gelesen = self.client.get(f"/Schedule/{self.geraet_id}",
                                  headers=self.kopf).json()
        self.assertEqual(gelesen["timeSlots"][0]["startTime"], "08:30:00")


class TestEnumUebersetzung(unittest.TestCase):
    """REST spricht Zeichenketten, das Geräteprotokoll spricht Zahlen."""

    def test_ordinalwerte_stimmen_mit_dem_protokoll_ueberein(self):
        self.assertEqual(zu_protokoll(OperatingMode.Smart), 0)
        self.assertEqual(zu_protokoll(OperatingMode.Night), 3)
        self.assertEqual(zu_protokoll(OperatingMode.Intake), 8)
        self.assertEqual(zu_protokoll(OperatingMode.Off), 11)
        self.assertEqual(zu_protokoll(FanSpeed.High), 2)
        self.assertEqual(zu_protokoll(HumidityLevel.Moist), 2)
        self.assertEqual(zu_protokoll(DeviceRole.SlaveOppositeMaster), 2)

    def test_rueckuebersetzung(self):
        for code in range(12):
            self.assertEqual(zu_protokoll(modus_von_protokoll(code)), code)

    def test_luftguete_zaehlt_ab_eins(self):
        self.assertEqual(luftguete_von_protokoll(1).value, "VeryGood")
        self.assertEqual(luftguete_von_protokoll(5).value, "Bad")

    def test_luftguete_null_ist_kein_bestwert(self):
        self.assertNotEqual(luftguete_von_protokoll(0).value, "VeryGood")

    def test_ungueltiger_ordinalwert_wird_abgewiesen(self):
        with self.assertRaises(ValueError):
            modus_von_protokoll(99)


class TestBetriebszustand(ApiBasis):
    def test_health_zaehlt_geraete_und_verbindungen(self):
        daten = self.client.get("/local/health").json()
        self.assertEqual(daten["status"], "ok")
        self.assertEqual(daten["geraete_gesamt"], 1)
        self.assertEqual(daten["geraete_verbunden"], 1)
        self.assertEqual(daten["benutzer"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
