# Mitschnitt für den Pilotbetrieb

Eine Frage ist noch offen: Wie sieht das Paket aus, mit dem ein Gerät die
Außenwetterdaten anfragt und der Server sie beantwortet? Ohne dieses Format
läuft alles außer dem SMART-Modus.

Erraten lässt es sich nicht. Man muss es einmal sehen.

## Was Sie tun

**Ihre Anlage bleibt dabei unverändert am Südwind-Server.** Das Werkzeug setzt
sich nur dazwischen, reicht jedes Byte unverändert weiter und schreibt mit. Es
verändert nichts und kann nichts schalten. Fällt es aus, verbinden sich die
Geräte von selbst wieder direkt.

**1. Werkzeug starten** — auf einem Rechner im Heimnetz, der durchlaufen kann
(Raspberry Pi, NAS, PC):

```bash
python3 mitschnitt_proxy.py --ziel 195.39.253.2
```

Die Angabe hinter `--ziel` ist die echte Serveradresse. Sie muss als Zahl
dastehen, nicht als Name — sonst schickt das Werkzeug die Verbindung im Kreis
zu sich selbst.

**2. Umleitung im Router setzen.** Im Router, in Pi-hole oder AdGuard einen
Eintrag anlegen, der `app.ambientika.eu` auf den Rechner aus Schritt 1 zeigen
lässt. Ein Eintrag, mehr nicht.

**3. Ein bis zwei Tage laufen lassen.** Das Wetterpaket kommt vermutlich nur
etwa stündlich, deshalb reicht eine Stunde nicht.

**4. Umleitung wieder entfernen** und die Datei `mitschnitt.jsonl`
zurückschicken.

## Woran Sie sehen, dass es läuft

```
>> 14:22:03  Gerät 192.168.1.44:51233 verbunden, öffne Weg zu 195.39.253.2:11000
    14:22:03  → Status          19 B  01001c9dc24304440301011b35...
  ★ 14:51:18  SERVER -> GERÄT  Typ 0x05  12 Byte  05001c9dc24304440a2d0000
```

Die Zeilen mit **★** sind das Gesuchte: Pakete vom Server, die kein
Modusbefehl sind. Kommt nach einem Tag keine einzige davon, sagen Sie bitte
Bescheid — dann liegt der Wetterkanal woanders, und wir suchen an anderer
Stelle weiter.

Beim Beenden mit Strg+C steht die Zusammenfassung auf dem Bildschirm.

## Wenn etwas hakt

**„Ziel nicht erreichbar"** — hinter `--ziel` steht ein Name statt einer
IP-Adresse, oder die Umleitung greift auch für den Proxy-Rechner selbst.

**„Kann nicht auf Port 11000 lauschen"** — dort läuft schon der lokale Server.
Beides zugleich geht nicht; erst den einen beenden.

**Keine Verbindung erscheint** — die Geräte hängen noch an der alten
Verbindung. Sie bauen sie von selbst neu auf, ein Stromlos-Schalten für zehn
Sekunden beschleunigt es.

## Was in der Datei steht

Zeitpunkt, Richtung, Länge und die Rohbytes jedes Rahmens. Dazu die
Seriennummern Ihrer Geräte — behandeln Sie die Datei wie ein Logfile. Was
darin **nicht** steht: Ihr Passwort, Ihre Adresse, Ihr Konto. Der Kanal
zwischen Gerät und Server überträgt nichts davon.
