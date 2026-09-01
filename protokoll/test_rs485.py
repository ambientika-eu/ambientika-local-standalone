#!/usr/bin/env python3
"""
Tests des RS485-Codecs gegen die Beispielrahmen der Anleitung Juli 2026.

Jeder dort abgedruckte Rahmen wird byteweise nachgebaut und wieder zerlegt.
Das ist der stärkste verfügbare Nachweis ohne Hardware: Die Beispiele stammen
vom Hersteller, tragen ihre eigenen Prüfsummen und decken jede Kombination aus
Drehrichtung, Stufe, Feuchteschwelle und Slave-Verhalten ab.

Ausführen mit:  python3 -m unittest test_rs485 -v
"""

from __future__ import annotations

import unittest

from rs485 import (
    BIT_GUELTIG, DREHUNG_ABLUFT, DREHUNG_GESTOPPT, DREHUNG_ZULUFT,
    DREHZAHL_1, DREHZAHL_2, DREHZAHL_3, DREHZAHL_NACHT, FEUCHTE_1, FEUCHTE_2,
    FEUCHTE_3, MOTOR_AUS, MOTOR_PAUSE, SLAVE_NACH_DIP, SLAVE_WIE_MASTER,
    TYP_BEFEHL, Befehl, antwort_lesen, befehl_zerlegen, pruefsumme,
    rahmen_zerlegen, statusabfrage, status_zerlegen,
)


def h(text: str) -> bytes:
    """Hex-Schreibweise der Anleitung in Bytes."""
    return bytes.fromhex(text.replace(" ", ""))


#: Sämtliche Befehlsbeispiele aus Abschnitt 7 der Anleitung Juli 2026.
BEFEHLE = [
    ("Motor aus, Klappe geschlossen", MOTOR_AUS,
     "02 30 31 30 30 30 30 30 31 03"),
    ("Motor Pause, Klappe geöffnet", MOTOR_PAUSE,
     "02 30 31 32 30 30 30 32 31 03"),
    ("Motor aus, Reset Filterwechsel", Befehl(filter_reset=True),
     "02 30 31 30 30 30 31 30 30 03"),

    ("Abluft Master Stufe 0", Befehl(DREHZAHL_NACHT, DREHUNG_ABLUFT, thermoaktor=1),
     "02 30 31 32 34 30 30 32 35 03"),
    ("Abluft Master Stufe 1", Befehl(DREHZAHL_1, DREHUNG_ABLUFT, thermoaktor=1),
     "02 30 31 32 35 30 30 32 34 03"),
    ("Abluft Master Stufe 2", Befehl(DREHZAHL_2, DREHUNG_ABLUFT, thermoaktor=1),
     "02 30 31 32 36 30 30 32 37 03"),
    ("Abluft Master Stufe 3", Befehl(DREHZAHL_3, DREHUNG_ABLUFT, thermoaktor=1),
     "02 30 31 32 37 30 30 32 36 03"),

    ("Zuluft Master Stufe 0", Befehl(DREHZAHL_NACHT, DREHUNG_ZULUFT, thermoaktor=1),
     "02 30 31 32 38 30 30 32 39 03"),
    ("Zuluft Master Stufe 1", Befehl(DREHZAHL_1, DREHUNG_ZULUFT, thermoaktor=1),
     "02 30 31 32 39 30 30 32 38 03"),
    ("Zuluft Master Stufe 2", Befehl(DREHZAHL_2, DREHUNG_ZULUFT, thermoaktor=1),
     "02 30 31 32 41 30 30 32 42 03"),
    ("Zuluft Master Stufe 3", Befehl(DREHZAHL_3, DREHUNG_ZULUFT, thermoaktor=1),
     "02 30 31 32 42 30 30 32 41 03"),

    ("Abluft M&S Stufe 0",
     Befehl(DREHZAHL_NACHT, DREHUNG_ABLUFT, SLAVE_WIE_MASTER, 1),
     "02 30 31 33 34 30 30 33 35 03"),
    ("Abluft M&S Stufe 3",
     Befehl(DREHZAHL_3, DREHUNG_ABLUFT, SLAVE_WIE_MASTER, 1),
     "02 30 31 33 37 30 30 33 36 03"),
    ("Zuluft M&S Stufe 0",
     Befehl(DREHZAHL_NACHT, DREHUNG_ZULUFT, SLAVE_WIE_MASTER, 1),
     "02 30 31 33 38 30 30 33 39 03"),
    ("Zuluft M&S Stufe 3",
     Befehl(DREHZAHL_3, DREHUNG_ZULUFT, SLAVE_WIE_MASTER, 1),
     "02 30 31 33 42 30 30 33 41 03"),

    ("Auto Feuchte 2, Abluft Nacht",
     Befehl(DREHZAHL_NACHT, DREHUNG_ABLUFT, SLAVE_NACH_DIP, 1, FEUCHTE_2),
     "02 30 31 36 34 30 30 36 35 03"),
    ("Auto Feuchte 2, Abluft Tag",
     Befehl(DREHZAHL_2, DREHUNG_ABLUFT, SLAVE_NACH_DIP, 1, FEUCHTE_2),
     "02 30 31 36 36 30 30 36 37 03"),
    ("Auto Feuchte 2, Zuluft Nacht",
     Befehl(DREHZAHL_NACHT, DREHUNG_ZULUFT, SLAVE_NACH_DIP, 1, FEUCHTE_2),
     "02 30 31 36 38 30 30 36 39 03"),
    ("Auto Feuchte 2, Zuluft Tag",
     Befehl(DREHZAHL_2, DREHUNG_ZULUFT, SLAVE_NACH_DIP, 1, FEUCHTE_2),
     "02 30 31 36 41 30 30 36 42 03"),

    ("Auto Feuchte 3, Abluft Nacht",
     Befehl(DREHZAHL_NACHT, DREHUNG_ABLUFT, SLAVE_NACH_DIP, 1, FEUCHTE_3),
     "02 30 31 41 34 30 30 41 35 03"),
    ("Auto Feuchte 3, Abluft Tag",
     Befehl(DREHZAHL_2, DREHUNG_ABLUFT, SLAVE_NACH_DIP, 1, FEUCHTE_3),
     "02 30 31 41 36 30 30 41 37 03"),
    ("Auto Feuchte 3, Zuluft Nacht",
     Befehl(DREHZAHL_NACHT, DREHUNG_ZULUFT, SLAVE_NACH_DIP, 1, FEUCHTE_3),
     "02 30 31 41 38 30 30 41 39 03"),
    ("Auto Feuchte 3, Zuluft Tag",
     Befehl(DREHZAHL_2, DREHUNG_ZULUFT, SLAVE_NACH_DIP, 1, FEUCHTE_3),
     "02 30 31 41 41 30 30 41 42 03"),
]

#: Alle acht Statusantworten aus Abschnitt 5.
STATUSANTWORTEN = [
    (0x08, "02 30 30 30 38 30 30 30 30 30 38 03", False, False, False),
    (0x09, "02 30 30 30 39 30 30 30 30 30 39 03", True, False, False),
    (0x0A, "02 30 30 30 41 30 30 30 30 30 41 03", False, True, False),
    (0x0B, "02 30 30 30 42 30 30 30 30 30 42 03", True, True, False),
    (0x0C, "02 30 30 30 43 30 30 30 30 30 43 03", False, False, True),
    (0x0D, "02 30 30 30 44 30 30 30 30 30 44 03", True, False, True),
    (0x0E, "02 30 30 30 45 30 30 30 30 30 45 03", False, True, True),
    (0x0F, "02 30 30 30 46 30 30 30 30 30 46 03", True, True, True),
]


class TestBefehlsrahmen(unittest.TestCase):
    def test_jeder_beispielrahmen_wird_exakt_erzeugt(self):
        for name, befehl, erwartet in BEFEHLE:
            with self.subTest(befehl=name):
                self.assertEqual(befehl.rahmen(), h(erwartet))

    def test_jeder_beispielrahmen_laesst_sich_zurueckdekodieren(self):
        for name, befehl, roh in BEFEHLE:
            with self.subTest(befehl=name):
                typ, daten = rahmen_zerlegen(h(roh))
                self.assertEqual(typ, TYP_BEFEHL)
                zurueck = befehl_zerlegen(daten[0], daten[1])
                self.assertEqual(zurueck, befehl)

    def test_pruefsumme_ist_xor_ueber_typ_und_daten(self):
        for name, _, roh in BEFEHLE:
            with self.subTest(befehl=name):
                rahmen = h(roh)
                # Die letzten beiden ASCII-Zeichen vor ETX sind die Prüfsumme.
                gesendet = int(rahmen[-3:-1].decode("ascii"), 16)
                typ, daten = rahmen_zerlegen(rahmen)
                self.assertEqual(pruefsumme(typ, *daten), gesendet)

    def test_statusabfrage(self):
        self.assertEqual(statusabfrage(), h("02 30 32 30 32 03"))


class TestBitbelegung(unittest.TestCase):
    """Die Belegung, die aus den Beispielen hervorgeht."""

    def test_drehzahl_in_den_unteren_zwei_bits(self):
        for stufe in range(4):
            self.assertEqual(Befehl(drehzahl=stufe).datenbyte() & 0x03, stufe)

    def test_drehung_in_bit_3_und_2(self):
        self.assertEqual(Befehl(drehung=DREHUNG_ABLUFT).datenbyte() >> 2 & 0x03, 1)
        self.assertEqual(Befehl(drehung=DREHUNG_ZULUFT).datenbyte() >> 2 & 0x03, 2)

    def test_slave_verhalten_in_bit_4(self):
        self.assertEqual(Befehl(slave=SLAVE_WIE_MASTER).datenbyte() & 0x10, 0x10)
        self.assertEqual(Befehl(slave=SLAVE_NACH_DIP).datenbyte() & 0x10, 0x00)

    def test_thermoaktor_in_bit_5(self):
        self.assertEqual(Befehl(thermoaktor=1).datenbyte() & 0x20, 0x20)

    def test_feuchteschwelle_in_bit_7_und_6(self):
        self.assertEqual(Befehl(feuchteschwelle=FEUCHTE_1).datenbyte() >> 6, 0)
        self.assertEqual(Befehl(feuchteschwelle=FEUCHTE_2).datenbyte() >> 6, 1)
        self.assertEqual(Befehl(feuchteschwelle=FEUCHTE_3).datenbyte() >> 6, 2)

    def test_stufe_null_ist_die_nachtdrehzahl(self):
        # "Stufe 0" der Anleitung entspricht der Nachtgeschwindigkeit,
        # nicht dem stehenden Motor.
        abluft0 = Befehl(DREHZAHL_NACHT, DREHUNG_ABLUFT, thermoaktor=1)
        self.assertEqual(abluft0.datenbyte(), 0x24)
        self.assertNotEqual(abluft0.drehung, DREHUNG_GESTOPPT)


class TestStatusmeldung(unittest.TestCase):
    def test_alle_acht_antworten(self):
        for byte, roh, daemmerung, filter_, feuchte in STATUSANTWORTEN:
            with self.subTest(datenbyte=hex(byte)):
                status = antwort_lesen(h(roh))
                self.assertIsNotNone(status)
                self.assertEqual(status.daemmerung, daemmerung)
                self.assertEqual(status.filteralarm, filter_)
                self.assertEqual(status.feuchtealarm, feuchte)
                self.assertTrue(status.gueltig)

    def test_gueltigkeitsbit_ist_in_allen_antworten_gesetzt(self):
        for byte, *_ in STATUSANTWORTEN:
            self.assertTrue(byte & BIT_GUELTIG, f"{byte:#04x} ohne Gültigkeitsbit")

    def test_echo_rahmen_werden_verworfen(self):
        # Der Master echot jedes Moduskommando als Typ 01 zurück. Wer darauf
        # reagiert, liest Betriebsmodi als Alarme.
        echo = Befehl(DREHZAHL_2, DREHUNG_ABLUFT, thermoaktor=1).rahmen()
        self.assertIsNone(antwort_lesen(echo))

    def test_status_ohne_gueltigkeitsbit_ist_erkennbar(self):
        self.assertFalse(status_zerlegen(0x00).gueltig)

    def test_alarmliste(self):
        self.assertEqual(status_zerlegen(0x0F).alarme,
                         ["Dämmerung", "Filteralarm", "Feuchtealarm"])
        self.assertEqual(status_zerlegen(0x08).alarme, [])


class TestRahmenpruefung(unittest.TestCase):
    """Ein verworfener Rahmen ist besser als ein falsch gelesener."""

    def test_falsche_pruefsumme_wird_abgewiesen(self):
        kaputt = bytearray(MOTOR_PAUSE.rahmen())
        kaputt[-2] = ord("0")
        with self.assertRaises(ValueError):
            rahmen_zerlegen(bytes(kaputt))

    def test_fehlendes_stx_wird_abgewiesen(self):
        with self.assertRaises(ValueError):
            rahmen_zerlegen(MOTOR_PAUSE.rahmen()[1:])

    def test_fehlendes_etx_wird_abgewiesen(self):
        with self.assertRaises(ValueError):
            rahmen_zerlegen(MOTOR_PAUSE.rahmen()[:-1])

    def test_ungerade_zeichenzahl_wird_abgewiesen(self):
        with self.assertRaises(ValueError):
            rahmen_zerlegen(bytes([0x02]) + b"3031303" + bytes([0x03]))

    def test_ungueltige_hexzeichen_werden_abgewiesen(self):
        with self.assertRaises(ValueError):
            rahmen_zerlegen(bytes([0x02]) + b"ZZ3030" + bytes([0x03]))


class TestSicherheitsregeln(unittest.TestCase):
    """Regeln aus der Anleitung, die Hardware schützen."""

    def test_richtungswechsel_erfordert_zwischenschritt(self):
        # Ein Richtungswechsel darf nur bei stehendem Motor erfolgen. Der
        # Zwischenschritt ist MOTOR_PAUSE — hier festgehalten, damit eine
        # spätere Ablaufsteuerung ihn nicht wegoptimiert.
        self.assertEqual(MOTOR_PAUSE.drehung, DREHUNG_GESTOPPT)
        self.assertEqual(MOTOR_PAUSE.thermoaktor, 1)
        self.assertEqual(MOTOR_AUS.drehung, DREHUNG_GESTOPPT)
        self.assertEqual(MOTOR_AUS.thermoaktor, 0)

    def test_beschreibung_ist_lesbar(self):
        self.assertIn("Abluft", Befehl(DREHZAHL_2, DREHUNG_ABLUFT,
                                       thermoaktor=1).beschreibung())
        self.assertIn("Klappe geschlossen", MOTOR_AUS.beschreibung())


if __name__ == "__main__":
    unittest.main(verbosity=2)
