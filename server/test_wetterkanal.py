#!/usr/bin/env python3
"""
Tests des Wetterkanals.

Die wichtigste Eigenschaft: **Ohne bestätigtes Format wird nichts gesendet.**
Ein geratenes Antwortpaket würde das Gerät auf erfundene Außenwerte regeln
lassen, ohne dass es jemandem auffällt — schlimmer als gar keine Antwort.

Ausführen mit:  python3 -m unittest test_wetterkanal -v
"""

from __future__ import annotations

import unittest

from wetter import FesteWerte, Wetterdienst
from wetterkanal import (
    Wetterkanal, kanal_aus_konfiguration, kodierer_aus_mitschnitt,
)

SN = "1C9DC2430444"
#: Ein erfundenes, aber formal glaubwürdiges Antwortpaket — so, wie eines aus
#: einem Mitschnitt aussehen könnte.
VORLAGE = bytes.fromhex("05001c9dc24304440a2d0000")


class TestOhneBestaetigtesFormat(unittest.TestCase):
    def setUp(self):
        self.dienst = Wetterdienst(FesteWerte(8.0, 75.0))
        self.kanal = Wetterkanal(self.dienst)

    def test_kanal_ist_nicht_einsatzbereit(self):
        self.assertFalse(self.kanal.einsatzbereit)

    def test_es_wird_nichts_gesendet(self):
        self.assertIsNone(self.kanal.antwort_bauen(SN, 46.45, 11.28))

    def test_die_anfrage_wird_trotzdem_gezaehlt(self):
        self.kanal.antwort_bauen(SN, 46.45, 11.28)
        self.assertEqual(self.kanal.statistik.anfragen, 1)
        self.assertEqual(self.kanal.statistik.ohne_kodierer, 1)
        self.assertEqual(self.kanal.statistik.beantwortet, 0)

    def test_erste_anfragen_werden_gemeldet_dann_nicht_mehr(self):
        with self.assertLogs("ambientika.wetterkanal", level="INFO") as protokoll:
            for _ in range(10):
                self.kanal.antwort_bauen(SN, 46.45, 11.28)
        # Höchstens drei Meldungen, sonst füllt ein stündliches Paket das Log.
        self.assertLessEqual(len(protokoll.output), 3)
        self.assertEqual(self.kanal.statistik.ohne_kodierer, 10)

    def test_ohne_typbyte_wird_keine_anfrage_erkannt(self):
        self.assertFalse(self.kanal.ist_wetteranfrage(bytes([0x05, 0x00])))


class TestUnbekannteRahmen(unittest.TestCase):
    """Solange das Typbyte unbekannt ist, ist das Log die einzige Spur."""

    def setUp(self):
        self.kanal = Wetterkanal(Wetterdienst(FesteWerte()))

    def test_status_und_firmware_werden_nicht_gemeldet(self):
        self.kanal.unbekannten_rahmen_notieren(bytes([0x01, 0x00]), SN)
        self.kanal.unbekannten_rahmen_notieren(bytes([0x03, 0x00]), SN)
        self.assertEqual(self.kanal.statistik.unbekannte_typen, {})

    def test_unbekannter_typ_wird_mit_rohbytes_gemeldet(self):
        rahmen = bytes([0x05, 0x00, 0x0A, 0x2D])
        with self.assertLogs("ambientika.wetterkanal", level="INFO") as protokoll:
            self.kanal.unbekannten_rahmen_notieren(rahmen, SN)
        ausgabe = "\n".join(protokoll.output)
        self.assertIn("0x05", ausgabe)
        self.assertIn(rahmen.hex(), ausgabe)
        self.assertIn("Wetteranfrage", ausgabe)

    def test_wiederholungen_werden_gezaehlt_aber_nicht_gemeldet(self):
        rahmen = bytes([0x05, 0x00])
        with self.assertLogs("ambientika.wetterkanal", level="INFO") as protokoll:
            for _ in range(20):
                self.kanal.unbekannten_rahmen_notieren(rahmen, SN)
        self.assertLessEqual(len(protokoll.output), 5)
        self.assertEqual(self.kanal.statistik.unbekannte_typen["0x05"], 20)

    def test_leerer_rahmen_stoert_nicht(self):
        self.kanal.unbekannten_rahmen_notieren(b"", SN)
        self.assertEqual(self.kanal.statistik.unbekannte_typen, {})


class TestMitBestaetigtemFormat(unittest.TestCase):
    def setUp(self):
        self.dienst = Wetterdienst(FesteWerte(8.0, 75.0))
        kodierer = kodierer_aus_mitschnitt(0x05, VORLAGE, 8, 9)
        self.kanal = Wetterkanal(self.dienst, kodierer, 0x05)

    def test_kanal_ist_einsatzbereit(self):
        self.assertTrue(self.kanal.einsatzbereit)

    def test_anfrage_wird_am_typbyte_erkannt(self):
        self.assertTrue(self.kanal.ist_wetteranfrage(bytes([0x05, 0x00])))
        self.assertFalse(self.kanal.ist_wetteranfrage(bytes([0x01, 0x00])))

    def test_antwort_traegt_die_aktuellen_werte(self):
        paket = self.kanal.antwort_bauen(SN, 46.45, 11.28)
        self.assertIsNotNone(paket)
        self.assertEqual(paket[8], 8)        # 8 °C
        self.assertEqual(paket[9], 75)       # 75 %

    def test_seriennummer_wird_eingesetzt(self):
        paket = self.kanal.antwort_bauen("AABBCCDDEEFF", 46.45, 11.28)
        self.assertEqual(paket[2:8].hex().upper(), "AABBCCDDEEFF")

    def test_alles_uebrige_bleibt_wie_beobachtet(self):
        # Was man nicht versteht, lässt man in Ruhe.
        paket = self.kanal.antwort_bauen(SN, 46.45, 11.28)
        self.assertEqual(paket[0:2], VORLAGE[0:2])
        self.assertEqual(paket[10:], VORLAGE[10:])
        self.assertEqual(len(paket), len(VORLAGE))

    def test_negative_temperatur_wird_vorzeichenbehaftet_kodiert(self):
        kanal = Wetterkanal(Wetterdienst(FesteWerte(-12.0, 90.0)),
                            kodierer_aus_mitschnitt(0x05, VORLAGE, 8, 9), 0x05)
        paket = kanal.antwort_bauen(SN, 46.45, 11.28)
        self.assertEqual(paket[8], 0xF4)     # -12 als Zweierkomplement

    def test_werte_werden_auf_den_moeglichen_bereich_begrenzt(self):
        kanal = Wetterkanal(Wetterdienst(FesteWerte(200.0, 300.0)),
                            kodierer_aus_mitschnitt(0x05, VORLAGE, 8, 9), 0x05)
        paket = kanal.antwort_bauen(SN, 46.45, 11.28)
        self.assertLessEqual(paket[8], 0xFF)
        self.assertLessEqual(paket[9], 100)

    def test_ohne_aussenwerte_wird_nichts_gesendet(self):
        kanal = Wetterkanal(Wetterdienst(),          # gar keine Quelle
                            kodierer_aus_mitschnitt(0x05, VORLAGE, 8, 9), 0x05)
        with self.assertLogs("ambientika.wetterkanal", level="WARNING"):
            self.assertIsNone(kanal.antwort_bauen(SN, 46.45, 11.28))
        self.assertEqual(kanal.statistik.ohne_daten, 1)

    def test_ein_scheiternder_kodierer_bringt_nichts_zum_absturz(self):
        def kaputt(seriennummer, werte):
            raise ValueError("Offset daneben")

        kanal = Wetterkanal(self.dienst, kaputt, 0x05)
        with self.assertLogs("ambientika.wetterkanal", level="ERROR"):
            self.assertIsNone(kanal.antwort_bauen(SN, 46.45, 11.28))


class TestKonfiguration(unittest.TestCase):
    def setUp(self):
        self.dienst = Wetterdienst(FesteWerte(5.0, 80.0))

    def test_leere_konfiguration_ergibt_untaetigen_kanal(self):
        kanal = kanal_aus_konfiguration(self.dienst, {})
        self.assertFalse(kanal.einsatzbereit)

    def test_vollstaendige_konfiguration_macht_einsatzbereit(self):
        kanal = kanal_aus_konfiguration(self.dienst, {
            "anfragetyp": "0x05", "vorlage": VORLAGE.hex(),
            "offset_temperatur": 8, "offset_feuchte": 9})
        self.assertTrue(kanal.einsatzbereit)
        self.assertEqual(kanal.anfragetyp, 0x05)

    def test_unbrauchbare_konfiguration_sendet_lieber_nichts(self):
        with self.assertLogs("ambientika.wetterkanal", level="ERROR"):
            kanal = kanal_aus_konfiguration(self.dienst, {
                "anfragetyp": "keine Zahl", "vorlage": "zzzz",
                "offset_temperatur": 8, "offset_feuchte": 9})
        self.assertFalse(kanal.einsatzbereit)

    def test_hexadezimale_und_dezimale_typangabe(self):
        for angabe in ("0x05", "5", 5):
            with self.subTest(angabe=angabe):
                kanal = kanal_aus_konfiguration(self.dienst, {
                    "anfragetyp": angabe, "vorlage": VORLAGE.hex(),
                    "offset_temperatur": 8, "offset_feuchte": 9})
                self.assertEqual(kanal.anfragetyp, 5)


class TestBericht(unittest.TestCase):
    def test_bericht_zeigt_den_zustand(self):
        kanal = Wetterkanal(Wetterdienst(FesteWerte()))
        kanal.antwort_bauen(SN, 46.45, 11.28)
        kanal.unbekannten_rahmen_notieren(bytes([0x05, 0x00]), SN)
        bericht = kanal.bericht()
        self.assertFalse(bericht["einsatzbereit"])
        self.assertIsNone(bericht["anfragetyp"])
        self.assertEqual(bericht["anfragen"], 1)
        self.assertEqual(bericht["ohne_kodierer"], 1)
        self.assertEqual(bericht["unbekannte_rahmentypen"], {"0x05": 1})
        self.assertIsNotNone(bericht["letzte_anfrage"])
        self.assertIsNone(bericht["letzte_antwort"])

    def test_bericht_nach_erfolgreicher_antwort(self):
        kanal = Wetterkanal(Wetterdienst(FesteWerte(3.0, 66.0)),
                            kodierer_aus_mitschnitt(0x05, VORLAGE, 8, 9), 0x05)
        kanal.antwort_bauen(SN, 46.45, 11.28)
        bericht = kanal.bericht()
        self.assertTrue(bericht["einsatzbereit"])
        self.assertEqual(bericht["anfragetyp"], "0x05")
        self.assertEqual(bericht["beantwortet"], 1)
        self.assertIsNotNone(bericht["letzte_antwort"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
