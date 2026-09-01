#!/usr/bin/env python3
"""
haupt.py — startet den lokalen Server.

Zwei Dienste in einem Prozess:

* der **Geräteserver** auf TCP 11000, zu dem sich die Lüfter verbinden,
* die **REST-Schnittstelle**, gegen die die Ambientika-App läuft.

    python3 haupt.py --konfig konfiguration.yaml

Beim allerersten Start wird ein Konto angelegt, weil sich sonst niemand
anmelden kann. Benutzername und Passwort stehen dann einmalig auf der Konsole —
danach nie wieder.
"""

from __future__ import annotations

import argparse
import logging
import secrets
import sys
from pathlib import Path

import uvicorn

from api import anwendung_bauen
from auth import Tokendienst
from geraeteserver import Geraeteserver
from modelle import FeatureFlagsResponse
from speicher import Speicher
from sensoren import sensor_aus_konfiguration
from wetter import FesteWerte, OpenWeatherMap, Wetterdienst
from wetterkanal import kanal_aus_konfiguration

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "protokoll"))
from ambientika_protocol import parse_calibration  # noqa: E402

STANDARD = {
    "datenbank": "ambientika.db",
    "geheimnis_datei": "jwt-geheimnis.txt",
    "rest": {"host": "0.0.0.0", "port": 4521},
    "geraete": {"host": "0.0.0.0", "port": 11000},
    "erlaubte_serien": [],
    "nur_beobachten": True,
    "noop_unterdruecken": True,
    "kalibrierung": {},
    "sensor": {},
    "wetter": {"fester_wert": {"temperatur": 10.0, "feuchte": 70.0}},
    "wetterkanal": {},
    "feature_flags": {},
}


def wetterdienst_bauen(konfig: dict) -> Wetterdienst:
    """Baut die Quellenkette in der empfohlenen Reihenfolge.

    Der eigene Sensor steht vorn: Er misst die Luft, die das Haus ansaugt, und
    hält den Betrieb offline. OpenWeatherMap ist die Rückfallebene, der feste
    Wert die letzte Notlösung für Inbetriebnahme und Prüfstand.
    """
    quellen = []
    sensor = sensor_aus_konfiguration(konfig.get("sensor") or {})
    if sensor is not None:
        quellen.append(sensor)

    wetter = konfig.get("wetter") or {}
    if wetter.get("openweathermap_schluessel"):
        quellen.append(OpenWeatherMap(wetter["openweathermap_schluessel"]))
    fest = wetter.get("fester_wert")
    if fest:
        quellen.append(FesteWerte(float(fest.get("temperatur", 10.0)),
                                  float(fest.get("feuchte", 70.0))))
    return Wetterdienst(*quellen)


def konfiguration_laden(pfad: str | None) -> dict:
    konfig = {**STANDARD}
    if not pfad:
        return konfig
    datei = Path(pfad)
    if not datei.exists():
        sys.exit(f"Konfigurationsdatei nicht gefunden: {pfad}")
    try:
        import yaml
    except ImportError:
        sys.exit("Für Konfigurationsdateien wird PyYAML benötigt: pip install pyyaml")
    geladen = yaml.safe_load(datei.read_text(encoding="utf-8")) or {}
    for schluessel, wert in geladen.items():
        if isinstance(wert, dict) and isinstance(konfig.get(schluessel), dict):
            konfig[schluessel] = {**konfig[schluessel], **wert}
        else:
            konfig[schluessel] = wert
    return konfig


def erstes_konto_anlegen(speicher: Speicher) -> None:
    """Legt beim ersten Start ein Konto an und zeigt die Zugangsdaten einmalig.

    Ein fest eingebautes Standardpasswort wäre bequemer und genau deshalb
    gefährlich: Es stünde in diesem Quelltext und gälte für jede Installation.
    """
    if speicher.benutzer_anzahl() > 0:
        return
    benutzername = "admin@local"
    passwort = secrets.token_urlsafe(12)
    speicher.benutzer_anlegen(benutzername, passwort, "Lokaler", "Zugang")
    rahmen = "=" * 62
    print(f"\n{rahmen}\n  Erstes Konto angelegt — diese Angaben jetzt notieren:\n"
          f"\n    Benutzername: {benutzername}\n    Passwort:     {passwort}\n"
          f"\n  Sie werden nicht erneut angezeigt.\n{rahmen}\n")


def main() -> int:
    zerleger = argparse.ArgumentParser(description=__doc__)
    zerleger.add_argument("--konfig", default=None)
    zerleger.add_argument("--log", default="INFO")
    argumente = zerleger.parse_args()

    logging.basicConfig(
        level=getattr(logging, argumente.log.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s")
    log = logging.getLogger("ambientika")

    konfig = konfiguration_laden(argumente.konfig)

    speicher = Speicher(konfig["datenbank"])
    erstes_konto_anlegen(speicher)

    tokendienst = Tokendienst(geheimnis_pfad=konfig["geheimnis_datei"])
    wetterdienst = wetterdienst_bauen(konfig)
    log.info("Wetterquellen in dieser Reihenfolge: %s",
             " -> ".join(q.name for q in wetterdienst.quellen) or "keine")
    if not any(q.name not in ("fest",) for q in wetterdienst.quellen):
        log.warning("Nur ein fester Wert als Wetterquelle. Für den SMART-Modus "
                    "einen Außensensor eintragen — ein erfundener Außenwert ist "
                    "keine Regelgrundlage.")

    wetterkanal = kanal_aus_konfiguration(wetterdienst,
                                          konfig.get("wetterkanal") or {})
    if not wetterkanal.einsatzbereit:
        log.info("Wetterkanal noch ohne bestätigtes Paketformat — Anfragen "
                 "werden protokolliert, aber nicht beantwortet. Siehe "
                 "MITSCHNITT.md.")

    erlaubte = {s.upper() for s in konfig.get("erlaubte_serien", [])}
    if not erlaubte:
        log.warning("Keine Freigabeliste gesetzt — der Geräteserver nimmt jede "
                    "Seriennummer an. Für den Dauerbetrieb erlaubte_serien füllen.")

    geraeteserver = Geraeteserver(
        speicher,
        host=konfig["geraete"]["host"], port=konfig["geraete"]["port"],
        erlaubte_serien=erlaubte,
        nur_beobachten=bool(konfig.get("nur_beobachten")),
        noop_unterdruecken=bool(konfig.get("noop_unterdruecken", True)),
        kalibrierung=parse_calibration(konfig.get("kalibrierung", {})),
        wetterkanal=wetterkanal)
    geraeteserver.starten()

    if konfig.get("nur_beobachten"):
        log.warning(
            "BEOBACHTUNGSMODUS — es wird nichts an die Geräte gesendet.\n"
            "        Der Server liest mit und zeigt an; Ihre Anlage läuft "
            "weiter wie bisher.\n"
            "        Vergleichen Sie die angezeigten Werte ein bis zwei Tage "
            "mit dem Display\n"
            "        Ihres Geräts. Stimmen sie überein, in der Konfiguration "
            "nur_beobachten\n"
            "        auf false setzen. Weichen sie ab: bitte melden.")
    else:
        log.warning(
            "STEUERBETRIEB — dieser Server schaltet Ihre Lüftung.\n"
            "        Diese Fassung ist noch nicht an Geräten aller "
            "Firmwarestände geprüft.\n"
            "        Bei ungewohntem Verhalten: nur_beobachten auf true "
            "setzen und melden.")

    app = anwendung_bauen(speicher, tokendienst, geraeteserver,
                          FeatureFlagsResponse(**konfig.get("feature_flags", {})))
    app.state.wetter = wetterdienst
    app.state.wetterkanal = wetterkanal

    try:
        uvicorn.run(app, host=konfig["rest"]["host"], port=konfig["rest"]["port"],
                    log_level=argumente.log.lower())
    finally:
        geraeteserver.beenden()
        speicher.schliessen()
    return 0


if __name__ == "__main__":
    sys.exit(main())
