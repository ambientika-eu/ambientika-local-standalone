#!/usr/bin/env python3
"""
auth.py — lokal ausgestellte Zugangstoken.

Die App schickt ``Authorization: Bearer <token>`` und erwartet von
``/Users/authenticate`` ein Feld ``jwtToken`` samt ``expiresAt``. Mehr verlangt
sie nicht: Sie prüft die Signatur nicht und liest den Inhalt nicht aus. Der
lokale Server stellt deshalb eigene Token aus, die niemand außer ihm selbst
prüfen muss.

Ein echtes Refresh-Token gibt es in der Cloud-API nicht. ``/Users/refresh-token``
nimmt keine Parameter und stellt mit dem noch gültigen Token ein neues aus —
gleitende Verlängerung. Läuft es ab, muss die App sich neu anmelden. Dieses
Verhalten wird hier nachgebildet, damit die App nicht in einen Zustand gerät,
den sie von der Cloud nicht kennt.

Das Signaturgeheimnis wird beim ersten Start erzeugt und in einer Datei neben
der Datenbank abgelegt. Es darf nicht im Quelltext stehen: Sonst könnte jeder,
der dieses Projekt kennt, für jede Installation gültige Token ausstellen.
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import jwt

#: Gültigkeitsdauer eines Tokens. Die App verlängert selbstständig über
#: /Users/refresh-token, solange sie läuft.
GUELTIGKEIT = timedelta(days=30)

ALGORITHMUS = "HS256"


def geheimnis_laden(pfad: str = "jwt-geheimnis.txt") -> str:
    """Lädt das Signaturgeheimnis oder legt beim ersten Start eines an."""
    datei = Path(pfad)
    if datei.exists():
        vorhanden = datei.read_text(encoding="utf-8").strip()
        if vorhanden:
            return vorhanden
    neu = secrets.token_urlsafe(48)
    datei.write_text(neu, encoding="utf-8")
    try:
        # Nur für den Eigentümer lesbar. Unter Windows wirkungslos, dort
        # schützen die Dateisystemrechte des Benutzerprofils.
        os.chmod(datei, 0o600)
    except OSError:
        pass
    return neu


class Tokendienst:
    def __init__(self, geheimnis: Optional[str] = None,
                 geheimnis_pfad: str = "jwt-geheimnis.txt"):
        self.geheimnis = geheimnis or geheimnis_laden(geheimnis_pfad)

    def ausstellen(self, benutzer_id: int, username: str) -> tuple:
        """Liefert (token, ablaufzeitpunkt)."""
        ablauf = datetime.now(timezone.utc) + GUELTIGKEIT
        nutzdaten = {
            "sub": str(benutzer_id),
            "username": username,
            "exp": int(ablauf.timestamp()),
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "iss": "ambientika-local",
        }
        return jwt.encode(nutzdaten, self.geheimnis, algorithm=ALGORITHMUS), ablauf

    def pruefen(self, token: str) -> Optional[dict]:
        """Gibt die Nutzdaten zurück, oder None bei ungültigem Token.

        Abgelaufene, manipulierte und fremd signierte Token führen alle zum
        selben Ergebnis — die Unterscheidung geht niemanden etwas an, der von
        außen anfragt.
        """
        try:
            return jwt.decode(token, self.geheimnis, algorithms=[ALGORITHMUS],
                              issuer="ambientika-local")
        except jwt.PyJWTError:
            return None

    def benutzer_id_aus_header(self, autorisierung: Optional[str]) -> Optional[int]:
        if not autorisierung:
            return None
        teile = autorisierung.split(None, 1)
        if len(teile) != 2 or teile[0].lower() != "bearer":
            return None
        nutzdaten = self.pruefen(teile[1].strip())
        if not nutzdaten:
            return None
        try:
            return int(nutzdaten["sub"])
        except (KeyError, TypeError, ValueError):
            return None
