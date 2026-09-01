#!/usr/bin/env python3
"""
mitschnitt_proxy.py — hört bei der echten Unterhaltung mit.

Wozu
----
Ein Punkt am Geräteprotokoll ist noch offen: das Paket, mit dem ein Gerät die
Außenwetterdaten anfragt und die Cloud sie beantwortet. Es lässt sich nicht
erraten — man muss es einmal sehen.

Der reine Mitlese-Server (``verify_capture.py``) hilft dabei nicht: Er ersetzt
die Cloud, also gibt es keine Cloud-Antwort zu sehen. Dieses Werkzeug macht das
Gegenteil — es setzt sich **dazwischen**, reicht alles unverändert weiter und
schreibt beide Richtungen mit.

Für die Anlage ändert sich dabei nichts. Die Geräte sprechen weiterhin mit dem
Südwind-Server, alles läuft wie immer, nur eben über einen Zuhörer. Fällt der
Proxy aus, verbinden sich die Geräte von selbst neu.

So läuft es
-----------
1. Proxy auf einem Rechner im Heimnetz starten::

       python3 mitschnitt_proxy.py --ziel 195.39.253.2

2. Im Router ``app.ambientika.eu`` auf diesen Rechner zeigen lassen.
   **Wichtig:** Der Proxy selbst braucht das echte Ziel als IP-Adresse, sonst
   schickt er die Verbindung im Kreis zu sich selbst zurück. Genau dafür ist
   ``--ziel`` da.

3. Ein bis zwei Tage laufen lassen. Danach die Umleitung im Router entfernen.

4. Die entstandene Datei ``mitschnitt.jsonl`` zurückschicken.

Interessant sind Rahmen vom Server zum Gerät, die kein Modusbefehl sind — das
Wetterpaket sollte dort auftauchen, vermutlich einmal pro Stunde.

Sicherheit
----------
Das Werkzeug verändert kein einziges Byte. Es schreibt mit, was ohnehin über
die Leitung geht — auf einem Kanal, der unverschlüsselt und ohne Anmeldung
arbeitet. Trotzdem gehört die entstehende Datei behandelt wie ein Logfile:
Sie enthält die Seriennummern der Geräte.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import socket
import socketserver
import sys
import threading

ZIEL_STANDARD_PORT = 11000

#: Rahmentypen des Geräteprotokolls, soweit bekannt.
BEKANNT_VOM_GERAET = {0x01: "Status", 0x03: "Firmware"}
BEKANNT_VOM_SERVER = {0x02: "Befehl/Setup"}

ZUSTAND = {
    "verbindungen": 0,
    "bytes_geraet": 0,
    "bytes_server": 0,
    "unbekannte_servertypen": {},
    "datei": None,
    "sperre": threading.Lock(),
}


def _zeit() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")


def _notieren(richtung: str, rohdaten: bytes, gegenstelle: str) -> None:
    if not rohdaten:
        return
    typ = rohdaten[0]
    if richtung == "geraet->server":
        bezeichnung = BEKANNT_VOM_GERAET.get(typ)
    else:
        bezeichnung = BEKANNT_VOM_SERVER.get(typ)

    eintrag = {
        "zeit": dt.datetime.now().isoformat(),
        "richtung": richtung,
        "gegenstelle": gegenstelle,
        "laenge": len(rohdaten),
        "typ": f"0x{typ:02X}",
        "bekannt": bezeichnung,
        "roh": rohdaten.hex(),
    }

    with ZUSTAND["sperre"]:
        if ZUSTAND["datei"]:
            ZUSTAND["datei"].write(json.dumps(eintrag) + "\n")
            ZUSTAND["datei"].flush()
        if richtung == "geraet->server":
            ZUSTAND["bytes_geraet"] += len(rohdaten)
        else:
            ZUSTAND["bytes_server"] += len(rohdaten)
            if bezeichnung is None:
                # Genau danach wird gesucht: alles, was vom Server kommt und
                # kein Modusbefehl ist.
                zaehler = ZUSTAND["unbekannte_servertypen"]
                zaehler[eintrag["typ"]] = zaehler.get(eintrag["typ"], 0) + 1
                print(f"  ★ {_zeit()}  SERVER -> GERÄT  Typ {eintrag['typ']}  "
                      f"{len(rohdaten)} Byte  {rohdaten.hex()}")
                return

    pfeil = "→" if richtung == "geraet->server" else "←"
    print(f"    {_zeit()}  {pfeil} {bezeichnung or 'unbekannt':<14} "
          f"{len(rohdaten):>3} B  {rohdaten.hex()[:60]}")


def _weiterleiten(quelle: socket.socket, ziel: socket.socket,
                  richtung: str, gegenstelle: str) -> None:
    """Reicht Bytes unverändert weiter und schreibt sie mit."""
    try:
        while True:
            daten = quelle.recv(4096)
            if not daten:
                break
            ziel.sendall(daten)          # unverändert — das ist der ganze Punkt
            _notieren(richtung, daten, gegenstelle)
    except OSError:
        pass
    finally:
        for s in (quelle, ziel):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class Vermittler(socketserver.BaseRequestHandler):
    ziel_host = ""
    ziel_port = ZIEL_STANDARD_PORT

    def handle(self):
        gegenstelle = f"{self.client_address[0]}:{self.client_address[1]}"
        with ZUSTAND["sperre"]:
            ZUSTAND["verbindungen"] += 1
        print(f"\n>> {_zeit()}  Gerät {gegenstelle} verbunden, "
              f"öffne Weg zu {self.ziel_host}:{self.ziel_port}")
        try:
            nach_oben = socket.create_connection((self.ziel_host, self.ziel_port),
                                                 timeout=15)
        except OSError as fehler:
            print(f"!! {_zeit()}  Ziel nicht erreichbar: {fehler}")
            print("   Zeigt --ziel wirklich auf die echte IP und nicht auf "
                  "den umgeleiteten Namen?")
            return

        nach_oben.settimeout(None)
        self.request.settimeout(None)

        hoch = threading.Thread(target=_weiterleiten, daemon=True,
                                args=(self.request, nach_oben,
                                      "geraet->server", gegenstelle))
        runter = threading.Thread(target=_weiterleiten, daemon=True,
                                  args=(nach_oben, self.request,
                                        "server->geraet", gegenstelle))
        hoch.start()
        runter.start()
        hoch.join()
        runter.join()
        print(f"<< {_zeit()}  Gerät {gegenstelle} getrennt")


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    zerleger = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    zerleger.add_argument("--ziel", required=True,
                          help="IP-Adresse des echten Servers, z. B. 195.39.253.2")
    zerleger.add_argument("--ziel-port", type=int, default=ZIEL_STANDARD_PORT)
    zerleger.add_argument("--host", default="0.0.0.0")
    zerleger.add_argument("--port", type=int, default=11000)
    zerleger.add_argument("--aus", default="mitschnitt.jsonl")
    argumente = zerleger.parse_args()

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass

    if not argumente.ziel.replace(".", "").isdigit():
        print("Hinweis: --ziel sollte eine IP-Adresse sein, kein Name. Ein Name\n"
              "         würde durch die eigene Umleitung wieder hierher zeigen.\n")

    ZUSTAND["datei"] = open(argumente.aus, "a", encoding="utf-8")
    Vermittler.ziel_host = argumente.ziel
    Vermittler.ziel_port = argumente.ziel_port

    print("Ambientika Mitschnitt-Proxy")
    print("Reicht alles unverändert weiter. Für die Anlage ändert sich nichts.\n")
    print(f"lauscht auf {argumente.host}:{argumente.port}")
    print(f"leitet weiter an {argumente.ziel}:{argumente.ziel_port}")
    print(f"schreibt nach {argumente.aus}")
    print("\nMit ★ markierte Zeilen sind das Gesuchte: Rahmen vom Server,")
    print("die kein Modusbefehl sind. Strg+C beendet.\n")

    try:
        with Server((argumente.host, argumente.port), Vermittler) as srv:
            srv.serve_forever()
    except KeyboardInterrupt:
        pass
    except OSError as fehler:
        print(f"\nKann nicht auf {argumente.host}:{argumente.port} lauschen: {fehler}")
        print("Läuft dort schon der lokale Server? Beides zugleich geht nicht.")
        return 1
    finally:
        print("\n--- Zusammenfassung ---")
        print(f"Verbindungen: {ZUSTAND['verbindungen']}")
        print(f"vom Gerät:    {ZUSTAND['bytes_geraet']} Byte")
        print(f"vom Server:   {ZUSTAND['bytes_server']} Byte")
        if ZUSTAND["unbekannte_servertypen"]:
            print("\nUnbekannte Rahmentypen vom Server — das ist der Fund:")
            for typ, anzahl in sorted(ZUSTAND["unbekannte_servertypen"].items()):
                print(f"  {typ}: {anzahl}×")
        else:
            print("\nKeine unbekannten Servertypen gesehen. Länger laufen lassen —")
            print("das Wetterpaket kommt vermutlich nur etwa stündlich.")
        if ZUSTAND["datei"]:
            ZUSTAND["datei"].close()
            print(f"\nMitschnitt: {argumente.aus}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
