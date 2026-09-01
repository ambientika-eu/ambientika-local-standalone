# Ambientika Local App — ohne Südwind-Server-Anbindung

Zweite, eigenständige Fassung der Local App. Sie ersetzt die bestehende nicht,
sie steht daneben. Kunden wählen.

| | Local App (bestehend) | Local App ohne Server-Anbindung |
|---|---|---|
| Oberfläche | im Heimnetz | im Heimnetz |
| Datenquelle | Südwind-Server | Gerät direkt, TCP 11000 |
| Internet im Betrieb | erforderlich | nur für Wetterdaten, optional |
| Südwind-Konto | erforderlich | nach der Inbetriebnahme nicht mehr |
| Erstinbetriebnahme | über die App | **über die App, mit Südwind-Server** |
| Empfehlung | **Standard** | auf Kundenwunsch |
| Reifegrad | im Feld bewährt | neu, Hardwareprüfung offen |

**Das bestehende Repository wird nicht angefasst.** Kein gemeinsamer Code, kein
gemeinsames Deployment, keine geteilte Konfiguration. Wer die bewährte Fassung
betreibt, merkt von diesem Projekt nichts.

## Für wen das gedacht ist

Für Anwender, die ihre Anlage bewusst vollständig im eigenen Netz betreiben
wollen und mit DNS, Docker oder Python umgehen können. Diese Fassung wird
deshalb auf den Produktseiten nicht beworben — sie steht hier für den, der sie
sucht.

Für alle anderen bleibt die Empfehlung die Ambientika-App oder die Local App
mit Server-Anbindung: im Feld bewährt, keine Einrichtung am Netzwerk, kein
Rechner, der durchlaufen muss. Wer nur eine Oberfläche im Heimnetz möchte,
bekommt sie dort ohne diesen Aufwand.

## Der Ablauf: erst offiziell, dann lokal

1. **Inbetriebnahme über die offizielle Ambientika-App**, wie gewohnt, mit dem
   Südwind-Server. Geräte anlernen, Räume und Zonen anlegen, Master- und
   Slave-Rollen setzen.
2. **Danach auf den lokalen Server umstellen.** Ab hier läuft alles im Haus.

Das ist bewusst so gewählt und keine Notlösung. Die Erstinbetriebnahme ist der
Schritt mit den meisten Fehlerquellen — WLAN, Rollen, Zonen, Querlüftung. Der
bewährte Weg erledigt ihn zuverlässig, und der lokale Betrieb setzt auf einer
Anlage auf, die nachweislich funktioniert.

Technisch dahinter: Das Anlernen läuft über `encryptedDeviceInfo`, einen
verschlüsselten Datenblock, dessen Verfahren nicht offenliegt. Der lokale
Server kann deshalb bestehende Geräte weiterbetreiben, aber keine neuen
anlernen. Die betreffenden Endpunkte antworten mit einer klaren Meldung statt
still etwas Falsches zu tun.

**Die Aussage „100 % lokal" bleibt dabei richtig** — sie bezieht sich auf den
Betrieb, und der ist es. Nach der Umstellung verlässt kein Kommando und kein
Messwert das Haus, es braucht kein Konto und keine Internetverbindung. Nur der
einmalige Einrichtungsvorgang lief über den Hersteller, so wie bei jedem Gerät,
das man einmal registriert und danach selbst betreibt.

## Wie die Umlenkung funktioniert

Die Geräte bauen eine ausgehende TCP-Verbindung zu dem Ziel auf, das ihnen bei
der Inbetriebnahme über Bluetooth mitgegeben wurde. In der App steht dieses
Ziel in `config.json` unter `wsDeviceUrl`, und die Kopplung schreibt es als
`H_<host>:<port>` auf die BLE-Charakteristik `C302`.

Drei Wege, das Ziel auf den lokalen Server zu legen:

- **App-Build mit lokalem Ziel.** `wsDeviceUrl` auf die Server-Adresse setzen.
  Sauberster Weg, verlangt aber einen eigenen App-Build.
- **Lokaler DNS-Eintrag.** `app.ambientika.eu` im Router oder Pi-hole auf den
  Server zeigen lassen. Kein Eingriff am Gerät, wirkt für alle Geräte zugleich.
- **Ziel per Bluetooth neu schreiben.** Einzeln je Gerät, ohne Routerrechte.

Die Verbindung ist unverschlüsseltes TCP ohne Zertifikatsprüfung. Genau deshalb
funktioniert die Umlenkung — und genau deshalb gehört der Server ins eigene
Netz und Port 11000 niemals ins Internet.

## Der SMART-Modus

`PacketType` der Cloud-API kennt einen Wert `OutsideWeatherRequest`: Das Gerät
fragt die Außenwetterdaten selbst über die Geräteverbindung an. Es gibt dafür
keinen REST-Endpunkt — der Server nimmt die Hauskoordinaten, holt die Daten und
schiebt sie als Antwortpaket zurück.

Der lokale Server beantwortet dieselbe Anfrage. Zwei Quellen sind vorgesehen:

- **Eigener Außensensor** im Heimnetz — Home Assistant, ESPHome, MQTT oder
  beliebiges JSON über HTTP. Damit ist der Betrieb vollständig offline und die
  Werte stammen von der Luft, die das Haus tatsächlich ansaugt.
- **OpenWeatherMap**, wie es die Cloud tut. Braucht Internet und einen eigenen
  API-Schlüssel — nie den aus der App, der ist im APK für jeden auslesbar.
- **Fester Wert**, nur für Inbetriebnahme und Prüfstand.

Der Sensor ist die bessere Wahl und steht deshalb in der Kette vorn: Eine
Wetterstation im Umkreis von zehn Kilometern sagt wenig über die Luft an der
Nordwand. Die Kette versucht die Quellen der Reihe nach; fällt eine aus, gilt
der letzte bekannte Wert weiter, solange er nicht älter als drei Stunden ist.

**Solange das Paketformat unbestätigt ist, wird nichts an das Gerät gesendet.**
Anfragen werden gezählt und mit ihren Rohbytes protokolliert. Ein geratenes
Antwortpaket würde das Gerät auf erfundene Außenwerte regeln lassen, ohne dass
es jemandem auffällt — das wäre schlechter als keine Antwort.

## Stand der Umsetzung

**Fertig und getestet — 102 Tests, ohne Hardware prüfbar:**

- `protokoll/ambientika_protocol.py` — Codec des Geräteprotokolls auf TCP 11000.
  Erkennt die Rahmenlänge je Gerät, dekodiert Status- und Firmwarerahmen,
  rechnet Sensorkorrekturen ein.
- `protokoll/ambientika_policy.py` — die Regeln, wann überhaupt an ein Gerät
  geschrieben wird: Setup-Sperre, Unterdrückung wirkungsloser Befehle,
  Seriennummern-Freigabeliste, Beobachtungsmodus.
- `protokoll/rs485.py` — Codec des kabelgebundenen Busses für ADVANCED+ und
  ADVANCED B+. Gegen alle 27 Beispielrahmen der Anleitung Juli 2026 verifiziert.
- `protokoll/verify_capture.py` — reiner Mitlese-Server zur Prüfung an echter
  Hardware. Kann technisch nichts senden.
- `protokoll/mitschnitt_proxy.py` — hört bei der echten Unterhaltung zwischen
  Gerät und Südwind-Server mit, ohne ein Byte zu verändern. Damit lässt sich
  das noch fehlende Wetterpaket bestimmen. Anleitung in `MITSCHNITT.md`.

**Serverseite — 133 Tests, lauffähig:**

- `server/modelle.py` — die Datenmodelle der Cloud-API, buchstabengetreu
- `server/api.py` — die REST-Schicht, gegen die die App unverändert läuft
- `server/geraeteserver.py` — nimmt die Geräteverbindungen auf TCP 11000 an
- `server/speicher.py` — Konten, Häuser, Zonen, Räume, Geräte, Zeitfenster
- `server/auth.py` — lokal ausgestellte Zugangstoken
- `server/sensoren.py` — Außensensoren im Heimnetz: Home Assistant, ESPHome,
  MQTT und beliebiges JSON über HTTP
- `server/wetter.py` — Quellenkette mit Zwischenspeicher, OpenWeatherMap
  (`data/2.5/weather`) als Rückfallebene
- `server/wetterkanal.py` — beantwortet die Außenwetter-Anfrage des Geräts,
  sobald das Paketformat bestätigt ist. Bis dahin protokolliert er sie
- `server/haupt.py` — Einstiegspunkt, startet beide Dienste

**Als Referenz vorhanden:**

- `../ambientika-api/API-REFERENZ.md` — alle 48 Endpunkte der Cloud-API mit
  Request- und Response-Schemata, 35 Datenmodelle, 15 Enums. Das ist der
  Vertrag, den der lokale Server erfüllen muss, damit die App unverändert
  gegen ihn läuft.

**Noch zu bauen:**

- Beantwortung des `OutsideWeatherRequest` — sobald das Paketformat aus
  einem Mitschnitt bekannt ist. Bis dahin läuft alles außer SMART.

**Offen und nur an Hardware zu klären:**

- Die Feldbelegung des 19-Byte-Statusrahmens älterer Firmware. Hergeleitet aus
  einem einzigen gemeldeten Rahmen, in sich stimmig, aber unbestätigt.
- Ab welcher Firmwareversion 21 statt 19 Byte gesendet werden.
- Das genaue Format des `OutsideWeatherRequest`-Pakets.
- Ob der Filter-Reset auf RS485 wirklich auf Bit 0 des zweiten Datenbytes
  liegt — die Dokumentrevisionen widersprechen sich hier.

## Zwei Protokolle, nicht eines

Eine Verwechslung, die viel Zeit kostet: RS485 und TCP sind **verschiedene
Protokolle für verschiedene Geräte**.

| | RS485 | TCP 11000 |
|---|---|---|
| Geräte | ADVANCED+, ADVANCED B+ | SMART, OFFICE |
| Kodierung | ASCII-erweiterte Hex-Zeichen | binär |
| Rahmen | Typ + 1–3 Datenbytes + XOR | 19 oder 21 Byte |
| Gerätekennung | keine | MAC-Adresse |
| Messwerte | keine | Temperatur, Feuchte, VOC, RSSI |
| Zustand | 3 Alarmbits | Modus, Stufe, Rolle, Alarme |
| Zweck | KNX, Loxone an verkabelten Anlagen | App und lokaler Server |

Der RS485-Codec liegt hier mit, weil er vollständig verifiziert ist und für die
Gateway-Anwendung gebraucht wird. Für die Local App ist er ohne Bedeutung.

## Prüfung

235 Tests: 102 auf der Protokollseite, 133 auf der Serverseite — darunter
14 Integrationstests, die einen echten TCP-Server, ein simuliertes Gerät und
die echte REST-Schicht gegeneinander laufen lassen.

```bash
cd protokoll && python3 -m unittest    # Codec, Framing, Sicherheit, RS485
cd server   && python3 -m unittest    # REST-Vertrag gegen die App
```

Abhängigkeiten: `fastapi`, `pydantic`, `pyjwt`, `uvicorn`, `pyyaml`.
