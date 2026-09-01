# Installation

Der Server läuft auf allem, was Python 3.10 oder neuer hat: Raspberry Pi, NAS,
kleiner Linux-Rechner, Windows-PC. Zwei Wege — Docker oder direkt.

**Vorher lesen:** Der Server wird im **Beobachtungsmodus** ausgeliefert. Er
liest mit und zeigt an, schaltet aber nichts. Warum das so ist und wann Sie
umstellen sollten, steht im Abschnitt *Erst beobachten, dann steuern*.

## Was Sie brauchen

- Eine Ambientika-Anlage mit SMART- oder OFFICE-Geräten, **bereits über die
  offizielle App in Betrieb genommen**. Das Anlernen neuer Geräte kann dieser
  Server nicht übernehmen.
- Einen Rechner im selben Netz, der durchlaufen kann.
- Zugriff auf Ihren Router oder DNS-Dienst, um die Geräte umzulenken.

## Weg 1: Docker

```bash
mkdir -p daten
cp konfiguration.beispiel.yaml daten/konfiguration.yaml
docker compose up -d
docker compose logs -f
```

Beim ersten Start erscheinen Benutzername und Passwort für das lokale Konto —
**einmalig**. Notieren.

`network_mode: host` in der Compose-Datei ist kein Komfort, sondern
Voraussetzung: Die Geräte stimmen sich untereinander per UDP-Broadcast im
selben Subnetz ab. Hinter einer Container-Bridge finden sie sich nicht.

## Weg 2: Direkt

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp konfiguration.beispiel.yaml konfiguration.yaml
cd server && python3 haupt.py --konfig ../konfiguration.yaml
```

### Als Dienst unter Linux

`/etc/systemd/system/ambientika-local.service`:

```ini
[Unit]
Description=Ambientika Local Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ambientika
WorkingDirectory=/opt/ambientika-local/server
ExecStart=/opt/ambientika-local/.venv/bin/python3 haupt.py \
          --konfig /opt/ambientika-local/konfiguration.yaml
Restart=on-failure
RestartSec=10

# Der Dienst braucht nichts außer seinem eigenen Verzeichnis.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/ambientika-local

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now ambientika-local
journalctl -u ambientika-local -f
```

## Die Geräte auf den Server umlenken

Die Geräte verbinden sich zu dem Ziel, das ihnen bei der Inbetriebnahme
mitgegeben wurde. Der einfachste Weg, das zu ändern: ein DNS-Eintrag.

Im Router, in Pi-hole oder AdGuard `app.ambientika.eu` auf die Adresse Ihres
Servers zeigen lassen. Ein Eintrag, alle Geräte auf einmal.

Danach verbinden sich die Geräte innerhalb weniger Minuten neu. Ein
Stromlos-Schalten für zehn Sekunden beschleunigt es. Im Log erscheint dann:

```
Gerät verbunden von 192.168.1.44:51233
1C9DC2430444 meldet Firmware radio=0.0.11 micro=0.0.11
```

**Rückweg:** DNS-Eintrag entfernen, Geräte kurz stromlos. Danach sprechen sie
wieder mit dem Südwind-Server, als wäre nichts gewesen.

## Erst beobachten, dann steuern

Diese Fassung ist **noch nicht an echten Geräten geprüft**. Die Belegung der
Statusfelder älterer Firmware ist aus einem einzigen gemeldeten Rahmen
hergeleitet: in sich stimmig, aber unbestätigt. Deshalb der Beobachtungsmodus
als Auslieferungszustand.

**Schritt 1 — mitlaufen lassen.** Ein bis zwei Tage. Der Server liest, zeigt
an und schaltet nichts. Ihre Anlage läuft unverändert weiter.

**Schritt 2 — vergleichen.** Stimmen Temperatur, Feuchte, Betriebsmodus und
Lüfterstufe mit dem Display Ihres Geräts überein?

- **Ja** → Die Belegung stimmt für Ihre Firmware. In der Konfiguration
  `nur_beobachten: false` setzen, Dienst neu starten. Ab jetzt steuert der
  Server.
- **Nein** → Bitte melden, mit Firmwarestand und den abweichenden Werten. Genau
  diese Rückmeldung fehlt noch.

**Schritt 3 — Freigabeliste füllen.** Tragen Sie unter `erlaubte_serien` die
Seriennummern Ihrer Geräte ein. Das Geräteprotokoll kennt keine Anmeldung; die
Liste ist die einzige Zugangskontrolle auf der Geräteseite.

## Sicherheit

Die Verbindung zwischen Gerät und Server ist **unverschlüsseltes TCP ohne
Anmeldung**. Genau das macht die Umlenkung möglich — und genau deshalb:

- Port **11000 niemals aus dem Internet erreichbar** machen. Keine
  Portweiterleitung, kein Reverse Proxy davor.
- Sobald es läuft, in der Konfiguration unter `geraete.host` und `rest.host`
  die LAN-Adresse eintragen statt `0.0.0.0`.
- `erlaubte_serien` füllen.

Der Server warnt beim Start, wenn er auf allen Schnittstellen lauscht oder
keine Freigabeliste gesetzt ist.

## Der Außensensor

Für den SMART-Modus braucht es Außenluftwerte. Empfohlen ist ein eigener Sensor
im Heimnetz — er misst die Luft, die das Haus tatsächlich ansaugt, und hält den
Betrieb offline. Vier Anbindungen stehen bereit: Home Assistant, ESPHome, MQTT
und beliebiges JSON über HTTP. Die Beispiele stehen in
`konfiguration.beispiel.yaml`.

Ohne Sensor lässt sich OpenWeatherMap als Rückfallebene eintragen. Dafür einen
**eigenen** Schlüssel anlegen — nicht den aus der Ambientika-App.

## Prüfen, ob alles läuft

```bash
curl http://localhost:4521/local/health
```

```json
{"status": "ok", "geraete_gesamt": 10, "geraete_verbunden": 10, "benutzer": 1}
```

## Wenn etwas hakt

**Kein Gerät verbindet sich** — greift die DNS-Umleitung? Von einem anderen
Rechner im Netz prüfen: `nslookup app.ambientika.eu` muss Ihre Serveradresse
liefern. Geräte danach kurz stromlos schalten.

**Port 11000 belegt** — läuft dort noch der Mitschnitt-Proxy? Beides zugleich
geht nicht.

**Die App zeigt leere Kacheln** — der Server hat noch keinen Statusrahmen von
dem Gerät bekommen. Ein bis zwei Minuten warten; im Log steht, was ankommt.

**Werte weichen vom Gerätedisplay ab** — im Beobachtungsmodus bleiben, melden.
Für einen bekannten, gleichbleibenden Versatz gibt es `kalibrierung`.

## Was dieser Server nicht kann

Neue Geräte anlernen. Das läuft über einen verschlüsselten Datenblock, dessen
Verfahren nicht offenliegt. Die betreffenden Endpunkte antworten mit einer
klaren Meldung. Erstinbetriebnahme über die offizielle App, danach hierher.
