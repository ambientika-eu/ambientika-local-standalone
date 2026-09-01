#!/usr/bin/env python3
"""
geraeteserver.py — nimmt die Verbindungen der Lüftungsgeräte entgegen.

Die Geräte sind ausgehende TCP-Clients. Sie verbinden sich zu dem Ziel, das
ihnen bei der Inbetriebnahme über Bluetooth mitgegeben wurde, und halten die
Verbindung offen. Dieser Server ist die Gegenstelle.

Er läuft in einem eigenen Thread mit eigenem asyncio-Loop, damit die
REST-Schicht ihn synchron ansprechen kann, ohne dass beide sich gegenseitig
blockieren. Befehle werden über ``run_coroutine_threadsafe`` in den Loop
gereicht und dort serialisiert.

Sicherheit
----------
Die Verbindung ist unverschlüsseltes TCP ohne Authentifizierung — genau das
macht die Umlenkung auf einen lokalen Server möglich, und genau deshalb gehört
dieser Server ins eigene Netz. Port 11000 darf nicht aus dem Internet
erreichbar sein. Beim Start wird gewarnt, wenn auf alle Schnittstellen
gebunden wird.

Der Setup-Rahmen, der Rolle, Zone und Haus-ID in ein Gerät schreibt, wird hier
**nicht** gesendet. In einer Anlage mit mehreren Mastern und gegenläufigen
Slaves würde ein pauschal gesetzter Rahmen die Querlüftung zerstören. Die
Rolle wird aus dem Statusrahmen gelesen, nicht hineingeschrieben.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime
from typing import Dict, Optional

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "protokoll"))

from ambientika_protocol import (  # noqa: E402
    FrameReader, NO_CALIBRATION, decode_firmware, decode_status,
    encode_filter_reset, encode_mode_command, implausible_fields, parse_serial,
    status_raw_codes, status_role_code,
)
from ambientika_policy import (  # noqa: E402
    command_is_noop, serial_allowed, write_refusal,
)
from api import Geraetebus  # noqa: E402
from speicher import Speicher  # noqa: E402
from wetterkanal import Wetterkanal  # noqa: E402

log = logging.getLogger("ambientika.geraete")

#: So lange wird auf weitere Bytes gewartet, bevor ein
#: unvollständiger unbekannter Rahmen ausgeliefert wird.
STILLE_SEKUNDEN = 1.0


class Verbindung:
    def __init__(self, seriennummer: str, writer: asyncio.StreamWriter):
        self.seriennummer = seriennummer
        self.writer = writer
        self.leser = FrameReader()
        self.sperre = asyncio.Lock()


class Geraeteserver(Geraetebus):
    def __init__(self, speicher: Speicher, host: str = "0.0.0.0", port: int = 11000,
                 erlaubte_serien: Optional[set] = None,
                 nur_beobachten: bool = False,
                 noop_unterdruecken: bool = True,
                 kalibrierung: Optional[dict] = None,
                 wetterkanal: Optional[Wetterkanal] = None):
        self.speicher = speicher
        self.wetterkanal = wetterkanal
        self.host = host
        self.port = port
        self.erlaubte_serien = erlaubte_serien or set()
        self.nur_beobachten = nur_beobachten
        self.noop_unterdruecken = noop_unterdruecken
        self.kalibrierung = kalibrierung or {}
        self.verbindungen: Dict[str, Verbindung] = {}
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[asyncio.AbstractServer] = None
        self._bereit = threading.Event()

    # -- Lebenszyklus -------------------------------------------------------
    def starten(self) -> None:
        if self.host in ("0.0.0.0", "::"):
            log.warning(
                "Geräteserver lauscht auf %s:%s — das Protokoll ist "
                "unverschlüsselt und ohne Anmeldung. Auf die LAN-Adresse "
                "binden und Port %s niemals aus dem Internet erreichbar machen.",
                self.host, self.port, self.port)
        self._thread = threading.Thread(target=self._loop_starten,
                                        name="geraeteserver", daemon=True)
        self._thread.start()
        if not self._bereit.wait(timeout=10):
            raise RuntimeError("Geräteserver ist nicht gestartet")

    def _loop_starten(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._server_starten())
        self._bereit.set()
        try:
            self.loop.run_forever()
        finally:
            # Den Loop selbst schließen, sonst bleibt bei jedem Neustart ein
            # Dateideskriptor zurück.
            try:
                self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            except Exception:                            # noqa: BLE001
                pass
            self.loop.close()

    async def _server_starten(self) -> None:
        self._server = await asyncio.start_server(self._verbindung_behandeln,
                                                  self.host, self.port)
        adressen = ", ".join(str(s.getsockname()) for s in self._server.sockets)
        log.info("Geräteserver lauscht auf %s", adressen)

    def beenden(self) -> None:
        """Fährt den Server herunter und schließt offene Verbindungen.

        Ohne das bleiben beim Beenden Aufgaben im Loop hängen und Python
        meldet unsaubere Sockets — im Test lärmend, im Dauerbetrieb ein
        langsames Leck bei jedem Neustart.
        """
        if self.loop and self.loop.is_running():
            def aufraeumen():
                for verbindung in list(self.verbindungen.values()):
                    try:
                        verbindung.writer.close()
                    except Exception:                    # noqa: BLE001
                        pass
                self.verbindungen.clear()
                if self._server is not None:
                    self._server.close()
                for aufgabe in asyncio.all_tasks(self.loop):
                    aufgabe.cancel()
                self.loop.call_later(0.1, self.loop.stop)

            self.loop.call_soon_threadsafe(aufraeumen)
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def gebundener_port(self) -> int:
        """Der tatsächlich belegte Port — bei Port 0 vom System vergeben."""
        if self._server and self._server.sockets:
            return self._server.sockets[0].getsockname()[1]
        return self.port

    # -- Empfang ------------------------------------------------------------
    async def _verbindung_behandeln(self, reader: asyncio.StreamReader,
                                    writer: asyncio.StreamWriter) -> None:
        gegenstelle = writer.get_extra_info("peername")
        log.info("Gerät verbunden von %s", gegenstelle)
        seriennummer: Optional[str] = None
        # emit_unknown: Die Außenwetter-Anfrage hat einen noch
        # unbekannten Typ. Ohne diese Einstellung würde sie still
        # verworfen und wäre nicht auffindbar.
        frame_reader = FrameReader(emit_unknown=True)
        try:
            while True:
                try:
                    # Wird es still, kann ein unbekannter Rahmen im Puffer
                    # liegen, dem die Abgrenzung zum nächsten fehlt. Genau das
                    # wäre bei einer Wetteranfrage der Fall: Das Gerät wartet
                    # auf Antwort und schickt bis dahin nichts mehr.
                    brocken = await asyncio.wait_for(reader.read(256),
                                                     timeout=STILLE_SEKUNDEN)
                except asyncio.TimeoutError:
                    for art, rahmen in frame_reader.flush():
                        await self._unbekannten_rahmen_behandeln(
                            seriennummer or "", rahmen, writer)
                    continue
                if not brocken:
                    break
                for art, rahmen in frame_reader.feed(brocken):
                    if art == "unknown":
                        # Zuerst behandeln: In einem unbekannten Rahmen steht
                        # an Position 2..8 nicht zwangsläufig eine
                        # Seriennummer. Würde sie hier gelesen, käme Unsinn
                        # heraus und die Freigabeliste träfe eine Entscheidung
                        # über einen Wert, den es gar nicht gibt.
                        await self._unbekannten_rahmen_behandeln(
                            seriennummer or "", rahmen, writer)
                        continue

                    sn = parse_serial(rahmen)
                    if not serial_allowed(sn, self.erlaubte_serien):
                        log.warning("Seriennummer %s steht nicht auf der "
                                    "Freigabeliste — Verbindung von %s beendet",
                                    sn, gegenstelle)
                        return
                    seriennummer = sn
                    self._verbindung_merken(sn, writer, frame_reader)

                    if art == "firmware":
                        info = decode_firmware(rahmen)
                        frame_reader.set_radio_fw(info["radioFw"])
                        zustand = self.speicher.zustand(sn)
                        zustand.radio_fw = info["radioFw"]
                        zustand.micro_fw = info["microFw"]
                        zustand.radio_at_fw = info["radioAtFw"]
                        log.info("%s meldet Firmware radio=%s micro=%s",
                                 sn, info["radioFw"], info["microFw"])
                        continue

                    self._status_uebernehmen(sn, rahmen)
        except asyncio.CancelledError:
            raise
        except Exception as fehler:                      # noqa: BLE001
            log.warning("Verbindungsfehler (%s): %s", seriennummer, fehler)
        finally:
            if seriennummer and self.verbindungen.get(seriennummer):
                if self.verbindungen[seriennummer].writer is writer:
                    self.verbindungen.pop(seriennummer, None)
                    self.speicher.zustand(seriennummer).online = False
                    log.info("Gerät %s getrennt", seriennummer)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:                            # noqa: BLE001
                pass

    def _verbindung_merken(self, sn: str, writer: asyncio.StreamWriter,
                           leser: FrameReader) -> Verbindung:
        vorhanden = self.verbindungen.get(sn)
        if vorhanden is None or vorhanden.writer is not writer:
            vorhanden = Verbindung(sn, writer)
            vorhanden.leser = leser
            self.verbindungen[sn] = vorhanden
        return vorhanden

    async def _unbekannten_rahmen_behandeln(self, sn: str, rahmen: bytes,
                                            writer: asyncio.StreamWriter) -> None:
        """Alles, was weder Status noch Firmware ist.

        Hier taucht die Außenwetter-Anfrage auf. Solange ihr Format nicht
        bestätigt ist, wird sie protokolliert und nicht beantwortet — ein
        geratenes Paket würde das Gerät auf erfundene Außenwerte regeln lassen.
        """
        if self.wetterkanal is None:
            return

        if self.wetterkanal.ist_wetteranfrage(rahmen) and sn:
            breite, laenge = self._koordinaten(sn)
            antwort = self.wetterkanal.antwort_bauen(sn, breite, laenge)
            if antwort is not None:
                await self._senden_im_loop(sn, antwort)
            return

        self.wetterkanal.unbekannten_rahmen_notieren(rahmen, sn)

    def _koordinaten(self, sn: str) -> tuple:
        """Breite und Länge des Hauses, zu dem das Gerät gehört."""
        haus_id = self.speicher.haus_von_geraet(sn)
        if haus_id is None:
            return (None, None)
        zeile = self.speicher.db.execute(
            "SELECT breite, laenge FROM haus WHERE id = ?", (haus_id,)).fetchone()
        if zeile is None:
            return (None, None)
        return (zeile["breite"], zeile["laenge"])

    def _status_uebernehmen(self, sn: str, rahmen: bytes) -> None:
        kal = self.kalibrierung.get(sn.upper(), NO_CALIBRATION)
        try:
            dekodiert = decode_status(rahmen, kal)
        except ValueError as fehler:
            log.warning("%s: Statusrahmen nicht dekodierbar (%s): %s",
                        sn, fehler, rahmen.hex())
            return

        probleme = implausible_fields(dekodiert)
        if probleme:
            # Solange die Feldbelegung älterer Firmware unbestätigt ist, wird
            # ein unplausibler Rahmen nicht als Messwert veröffentlicht.
            log.warning("%s: unplausibler Status, nicht übernommen: %s | roh %s",
                        sn, "; ".join(probleme), rahmen.hex())
            return

        codes = status_raw_codes(rahmen)
        zustand = self.speicher.zustand(sn)
        zustand.modus_code = codes["mode"]
        zustand.stufe_code = codes["speed"]
        zustand.feuchte_code = codes["humidity"]
        zustand.licht_code = codes["light"]
        zustand.temperatur = int(dekodiert["temperature"])
        zustand.feuchte = int(dekodiert["humidity"])
        zustand.luftguete_roh = dekodiert["airQuality"] + 1 \
            if dekodiert["airQualityLabel"] != "UNKNOWN_SENSOR" else 0
        zustand.feuchtealarm = bool(dekodiert["humidityAlarm"])
        zustand.filter_code = 2 if dekodiert["filterAlarm"] else 0
        zustand.nachtalarm = bool(dekodiert["nightAlarm"])
        zustand.rolle_code = status_role_code(rahmen)
        zustand.signalstaerke = dekodiert.get("rssi")
        zustand.gesehen = datetime.utcnow()
        zustand.online = True

        # Die Rolle wird gelesen, nicht geschrieben: Das Gerät weiß am besten,
        # ob es Master oder Slave ist.
        rollen = ["Master", "SlaveEqualMaster", "SlaveOppositeMaster",
                  "NotConfigured"]
        if 0 <= zustand.rolle_code < len(rollen):
            if self.speicher.geraet_nach_seriennummer(sn) is not None:
                self.speicher.geraet_rolle_setzen(sn, rollen[zustand.rolle_code])

    # -- Senden -------------------------------------------------------------
    async def _senden_im_loop(self, sn: str, rahmen: bytes) -> bool:
        """Sendet aus dem Event-Loop heraus.

        Wird von Codepfaden benutzt, die ohnehin schon im Loop laufen — etwa
        der Antwort auf eine Wetteranfrage. Der Umweg über
        ``run_coroutine_threadsafe`` wäre hier fatal: Er würde auf eine
        Coroutine warten, die nur derselbe Loop ausführen kann, den er dabei
        blockiert. Der Server bliebe stehen.
        """
        verweigerung = write_refusal(self.nur_beobachten)
        if verweigerung:
            log.info("%s: %s", sn, verweigerung)
            return False
        verbindung = self.verbindungen.get(sn.upper())
        if verbindung is None:
            return False
        try:
            async with verbindung.sperre:
                verbindung.writer.write(rahmen)
                await verbindung.writer.drain()
            return True
        except Exception as fehler:                      # noqa: BLE001
            log.warning("Senden an %s fehlgeschlagen: %s", sn, fehler)
            return False

    def _senden(self, sn: str, rahmen: bytes) -> bool:
        """Sendet aus einem fremden Thread — so ruft die REST-Schicht an."""
        if self.loop is None:
            return False
        zukunft = asyncio.run_coroutine_threadsafe(
            self._senden_im_loop(sn, rahmen), self.loop)
        try:
            return bool(zukunft.result(timeout=5))
        except Exception as fehler:                      # noqa: BLE001
            log.warning("Senden an %s fehlgeschlagen: %s", sn, fehler)
            return False

    # -- Geraetebus ---------------------------------------------------------
    def verbunden(self, seriennummer: str) -> bool:
        return seriennummer.upper() in self.verbindungen

    def modus_setzen(self, seriennummer: str, modus: int, stufe: int,
                     feuchte: int, licht: int) -> bool:
        sn = seriennummer.upper()
        zustand = self.speicher.zustand(sn)
        aktuell = {"mode": zustand.modus_code, "speed": zustand.stufe_code,
                   "humidity": zustand.feuchte_code, "light": zustand.licht_code}
        if self.noop_unterdruecken and command_is_noop(aktuell, modus, stufe,
                                                       feuchte, licht):
            # Jeder angenommene Befehl löst am Gerät den Quittungston aus.
            # Ein Befehl, der nichts ändert, ist nachts reiner Lärm.
            log.debug("%s steht bereits auf modus=%s stufe=%s — nicht erneut "
                      "gesendet", sn, modus, stufe)
            return True
        return self._senden(sn, encode_mode_command(sn, modus, stufe,
                                                    feuchte, licht))

    def filter_zuruecksetzen(self, seriennummer: str) -> bool:
        sn = seriennummer.upper()
        return self._senden(sn, encode_filter_reset(sn))

    def konfiguration_senden(self, seriennummer: str) -> bool:
        """Sendet den zuletzt bekannten Zustand erneut ans Gerät.

        Bewusst kein Setup-Rahmen: Rolle, Zone und Haus-ID werden hier nie
        geschrieben. Siehe Modulkopf.
        """
        sn = seriennummer.upper()
        zustand = self.speicher.zustand(sn)
        return self._senden(sn, encode_mode_command(
            sn, zustand.modus_code, zustand.stufe_code,
            zustand.feuchte_code, zustand.licht_code))
