# Änderungen

## 0.1.0 — erste Fassung

Erste lauffähige Fassung des lokalen Servers. **Beobachtungsmodus als
Auslieferungszustand** — siehe `INSTALLATION.md`, Abschnitt *Erst beobachten,
dann steuern*.

**Enthalten**

- Geräteserver auf TCP 11000, nimmt die Verbindungen der Lüfter an
- REST-Schnittstelle, gegen die die bestehende Ambientika-App unverändert läuft
- Anmeldung mit lokal ausgestellten Zugangstoken
- Häuser, Zonen, Räume, Geräte, Wochenzeitplan
- Außensensoren: Home Assistant, ESPHome, MQTT, JSON über HTTP
- OpenWeatherMap als Rückfallebene, mit Zwischenspeicher
- RS485-Codec für ADVANCED+ und ADVANCED B+, gegen alle 27 Beispielrahmen
  der Anleitung Juli 2026 verifiziert
- Mitlese-Server und Mitschnitt-Proxy für die Prüfung an echter Hardware

**Bewusste Entscheidungen**

- Es wird **kein Setup-Rahmen** an Geräte gesendet. Rolle, Zone und Haus-ID
  werden aus dem Statusrahmen gelesen, nicht hineingeschrieben. Ein pauschal
  gesetzter Rahmen würde in einer Anlage mit mehreren Mastern die Querlüftung
  zerstören.
- Ein Befehl, der den bereits aktiven Zustand wiederholt, wird nicht gesendet.
  Jeder angenommene Befehl löst am Gerät den Quittungston aus.
- Ein Statusrahmen, der physikalisch unmöglich ist, wird verworfen statt als
  Messwert veröffentlicht.
- Der Wetterkanal antwortet nicht, solange das Paketformat unbestätigt ist.

**Noch offen**

- Das Format des `OutsideWeatherRequest`-Pakets. Bis dahin läuft alles außer
  dem SMART-Modus. Siehe `MITSCHNITT.md`.
- Die Feldbelegung des 19-Byte-Statusrahmens älterer Firmware ist aus einem
  einzigen gemeldeten Rahmen hergeleitet und an Hardware unbestätigt.
- Ab welcher Firmwareversion 21 statt 19 Byte gesendet werden.
- Ob der Filter-Reset auf RS485 auf Bit 0 des zweiten Datenbytes liegt — die
  Dokumentrevisionen widersprechen sich.

**Prüfstand**

235 Tests, davon 14 Integrationstests über einen echten TCP-Server, ein
simuliertes Gerät und die echte REST-Schicht. Kein Test lief bisher gegen ein
physisches Lüftungsgerät.
