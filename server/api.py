#!/usr/bin/env python3
"""
api.py — die REST-Schicht, gegen die die bestehende App unverändert läuft.

Pfade, Feldnamen und Enum-Schreibweisen folgen der veröffentlichten
Cloud-Spezifikation. Abweichungen fallen nicht als Fehler auf, sondern als
leere Kachel in der App — deshalb ist hier nichts „aufgeräumt" worden, auch
nicht der Tippfehler ``signalStrenght``.

Was dieser Server nicht kann, und warum
---------------------------------------
Die Gerätekopplung läuft über ``encryptedDeviceInfo``, einen verschlüsselten
Datenblock, dessen Verfahren nicht offenliegt. Die betreffenden Endpunkte
antworten deshalb mit 501 und einem erklärenden Text, statt still etwas
Falsches zu tun. Die Erstinbetriebnahme läuft über den Südwind-Server; danach
kann die Anlage dauerhaft hier betrieben werden.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query
from fastapi.responses import JSONResponse

from modelle import (
    AddHouseRequest, AuthenticateRequest, AuthenticateResponse, ChangeModeRequest,
    Device, DeviceRole, DeviceSubtype, DeviceType, FeatureFlagsResponse, House,
    HouseDevicesInfo, HouseInfo, NewZoneWithRoomsRequest, RenameDeviceRequest,
    RenameHouseRequest, RenameZoneRequest, Room, RoomNames, Schedule,
    SetHouseTimezoneRequest, StatusPacket, TimeSlot, TokenInfoResponse,
    TokenRefreshResponse, UserDetailsResponse, Zone, ZoneDeviceInfo,
    filter_von_protokoll, feuchte_von_protokoll, licht_von_protokoll,
    luftguete_von_protokoll, modus_von_protokoll, rolle_von_protokoll,
    stufe_von_protokoll, zu_protokoll,
)
from speicher import Livezustand, Speicher

#: Meldung für alles, was ohne das Kopplungsverfahren nicht geht.
KOPPLUNG_NICHT_VERFUEGBAR = (
    "Diese Funktion braucht das Kopplungsverfahren des Herstellers "
    "(encryptedDeviceInfo) und steht im lokalen Betrieb nicht zur Verfügung. "
    "Neue Geräte werden einmalig über die Ambientika-App und den "
    "Südwind-Server angelernt; danach läuft die Anlage hier weiter."
)


class Geraetebus:
    """Schnittstelle zur Geräteebene.

    Die REST-Schicht kennt keine Bytes und keine Sockets — sie ruft hier an.
    Für Tests genügt eine Attrappe, die sich merkt, was verlangt wurde.
    """

    def modus_setzen(self, seriennummer: str, modus: int, stufe: int,
                     feuchte: int, licht: int) -> bool:
        raise NotImplementedError

    def filter_zuruecksetzen(self, seriennummer: str) -> bool:
        raise NotImplementedError

    def konfiguration_senden(self, seriennummer: str) -> bool:
        raise NotImplementedError

    def verbunden(self, seriennummer: str) -> bool:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Umwandlung Datenbank -> API-Modelle
# ---------------------------------------------------------------------------
def _raumname(text: str) -> RoomNames:
    """Der Raumname ist in der API ein Enum, kein Freitext.

    Unbekannte Namen landen auf GenericRoom1, statt die Antwort scheitern zu
    lassen — ein Raum mit ungewohntem Namen ist kein Grund, die ganze Anlage
    unsichtbar zu machen.
    """
    try:
        return RoomNames(text)
    except ValueError:
        return RoomNames.GenericRoom1


def _geraet_modell(zeile, zustand: Optional[Livezustand] = None) -> Device:
    return Device(
        id=zeile["id"],
        deviceType=DeviceType(zeile["typ"]),
        deviceSubtype=DeviceSubtype(zeile["subtyp"]),
        serialNumber=zeile["seriennummer"],
        name=zeile["name"],
        role=DeviceRole(zeile["rolle"]),
        zoneIndex=zeile["zonen_index"],
        installation=datetime.fromisoformat(zeile["installiert"]),
        radioFwVersion=zustand.radio_fw if zustand else None,
        microFwVersion=zustand.micro_fw if zustand else None,
        radioAtCommandsFwVersion=zustand.radio_at_fw if zustand else None,
        roomId=zeile["raum_id"],
    )


def _status_modell(sn: str, zustand: Livezustand) -> StatusPacket:
    """Übersetzt den Livezustand in das Paket, das die App erwartet.

    Hier findet die Umrechnung zwischen den beiden Kodierungen statt: Das
    Geräteprotokoll liefert Zahlen, die REST-Schnittstelle erwartet
    Zeichenketten.
    """
    return StatusPacket(
        deviceSerialNumber=sn,
        operatingMode=modus_von_protokoll(zustand.modus_code),
        fanSpeed=stufe_von_protokoll(zustand.stufe_code),
        humidityLevel=feuchte_von_protokoll(zustand.feuchte_code),
        temperature=zustand.temperatur,
        humidity=zustand.feuchte,
        airQuality=luftguete_von_protokoll(zustand.luftguete_roh),
        humidityAlarm=zustand.feuchtealarm,
        filtersStatus=filter_von_protokoll(zustand.filter_code),
        nightAlarm=zustand.nachtalarm,
        deviceRole=rolle_von_protokoll(zustand.rolle_code),
        lastOperatingMode=modus_von_protokoll(zustand.letzter_modus_code),
        lightSensorLevel=licht_von_protokoll(zustand.licht_code),
        signalStrenght=zustand.signalstaerke or 0,
        isTurboAvailable=False,
    )


def _haus_modell(zeile, zonen: List[Zone], raeume: List[Room]) -> House:
    return House(
        userId=zeile["benutzer_id"],
        id=zeile["id"],
        name=zeile["name"],
        zones=zonen or None,
        rooms=raeume or None,
        hasZones=bool(zonen),
        hasDevices=any(r.roomDevicesCount for r in raeume),
        address=zeile["adresse"],
        latitude=zeile["breite"],
        longitude=zeile["laenge"],
        timezone=zeile["zeitzone"],
        ianaTimezone=zeile["iana_zeitzone"],
        currentHouseTime=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Anwendung
# ---------------------------------------------------------------------------
def anwendung_bauen(speicher: Speicher, tokendienst, bus: Geraetebus,
                    feature_flags: Optional[FeatureFlagsResponse] = None) -> FastAPI:
    app = FastAPI(title="Ambientika Local — ohne Südwind-Server-Anbindung",
                  version="0.1.0")
    flags = feature_flags or FeatureFlagsResponse()

    # -- Anmeldung ----------------------------------------------------------
    def angemeldet(authorization: Optional[str] = Header(default=None)) -> int:
        benutzer_id = tokendienst.benutzer_id_aus_header(authorization)
        if benutzer_id is None:
            raise HTTPException(status_code=401, detail="Nicht angemeldet")
        return benutzer_id

    def _haus_oder_404(haus_id: int, benutzer_id: int):
        zeile = speicher.haus(haus_id, benutzer_id)
        if zeile is None:
            raise HTTPException(status_code=404, detail="Haus nicht gefunden")
        return zeile

    def _raeume_mit_geraeten(haus_id: int, zone_id: Optional[int] = None,
                             nur_freie: bool = False) -> List[Room]:
        ergebnis = []
        for r in speicher.raeume(haus_id, zone_id=zone_id, nur_freie=nur_freie):
            geraete = [_geraet_modell(g, speicher.zustand(g["seriennummer"]))
                       for g in speicher.geraete(raum_id=r["id"])]
            ergebnis.append(Room(id=r["id"], name=_raumname(r["name"]),
                                 houseId=r["haus_id"], devices=geraete or None,
                                 roomDevicesCount=len(geraete)))
        return ergebnis

    def _zonen_modelle(haus_id: int) -> List[Zone]:
        return [Zone(id=z["id"], name=z["name"],
                     rooms=_raeume_mit_geraeten(haus_id, zone_id=z["id"]) or None)
                for z in speicher.zonen(haus_id)]

    # =======================================================================
    # Users
    # =======================================================================
    @app.post("/Users/authenticate", response_model=AuthenticateResponse)
    def authenticate(anfrage: AuthenticateRequest):
        zeile = speicher.benutzer_pruefen(anfrage.username, anfrage.password or "")
        if zeile is None:
            # Bewusst dieselbe Meldung für unbekanntes Konto und falsches
            # Passwort — sonst verrät die Antwort, welche Konten existieren.
            raise HTTPException(status_code=401,
                                detail="Benutzername oder Passwort falsch")
        token, ablauf = tokendienst.ausstellen(zeile["id"], zeile["username"])
        name = " ".join(t for t in (zeile["vorname"], zeile["nachname"]) if t)
        return AuthenticateResponse(
            id=zeile["id"], firstName=zeile["vorname"], lastName=zeile["nachname"],
            completeName=name or zeile["username"], username=zeile["username"],
            jwtToken=token, expiresAt=ablauf, userLevel=zeile["level"])

    @app.get("/Users/feature-flags", response_model=FeatureFlagsResponse)
    def feature_flags():
        # Ohne diese Antwort blendet die App den Wochenzeitplan und weitere
        # Funktionen aus, ohne dass irgendwo ein Fehler erscheint.
        return flags

    @app.get("/Users/refresh-token", response_model=TokenRefreshResponse)
    def refresh_token(benutzer_id: int = Depends(angemeldet)):
        # Gleitende Verlängerung, genau wie in der Cloud: kein eigenes
        # Refresh-Token, sondern ein neues Zugangstoken gegen ein gültiges.
        zeile = speicher.db.execute("SELECT * FROM benutzer WHERE id = ?",
                                    (benutzer_id,)).fetchone()
        token, ablauf = tokendienst.ausstellen(benutzer_id, zeile["username"])
        return TokenRefreshResponse(jwtToken=token, expiresAt=ablauf)

    @app.get("/Users/token-info", response_model=TokenInfoResponse)
    def token_info(authorization: Optional[str] = Header(default=None)):
        nutzdaten = tokendienst.pruefen((authorization or "").split(" ")[-1])
        if not nutzdaten:
            raise HTTPException(status_code=401, detail="Token ungültig")
        return TokenInfoResponse(
            username=nutzdaten.get("username"),
            expiresAt=datetime.fromtimestamp(nutzdaten["exp"], tz=timezone.utc))

    @app.get("/Users/user-details", response_model=UserDetailsResponse)
    def user_details(benutzer_id: int = Depends(angemeldet)):
        zeile = speicher.db.execute("SELECT * FROM benutzer WHERE id = ?",
                                    (benutzer_id,)).fetchone()
        return UserDetailsResponse(firstName=zeile["vorname"],
                                   lastName=zeile["nachname"],
                                   username=zeile["username"])

    # =======================================================================
    # House
    # =======================================================================
    @app.get("/House/houses", response_model=List[House])
    def houses(benutzer_id: int = Depends(angemeldet)):
        return [_haus_modell(h, _zonen_modelle(h["id"]),
                             _raeume_mit_geraeten(h["id"]))
                for h in speicher.haeuser(benutzer_id)]

    @app.get("/House/configured-houses", response_model=List[House])
    def configured_houses(benutzer_id: int = Depends(angemeldet)):
        # „Konfiguriert" heißt: mindestens ein Gerät mit Master-Rolle.
        ergebnis = []
        for h in speicher.haeuser(benutzer_id):
            geraete = speicher.geraete(haus_id=h["id"])
            if any(g["rolle"] == DeviceRole.Master.value for g in geraete):
                ergebnis.append(_haus_modell(h, _zonen_modelle(h["id"]),
                                             _raeume_mit_geraeten(h["id"])))
        return ergebnis

    @app.get("/House/houses-info", response_model=List[HouseInfo])
    def houses_info(benutzer_id: int = Depends(angemeldet)):
        ergebnis = []
        for h in speicher.haeuser(benutzer_id):
            zonen = speicher.zonen(h["id"])
            geraete = speicher.geraete(haus_id=h["id"])
            ergebnis.append(HouseInfo(houseId=h["id"], houseName=h["name"],
                                      houseZonesCount=len(zonen),
                                      houseDevicesCount=len(geraete)))
        return ergebnis

    @app.get("/House/house-info", response_model=HouseInfo)
    def house_info(houseId: int = Query(...), benutzer_id: int = Depends(angemeldet)):
        h = _haus_oder_404(houseId, benutzer_id)
        zonen = speicher.zonen(houseId)
        geraete = speicher.geraete(haus_id=houseId)
        return HouseInfo(
            houseId=h["id"], houseName=h["name"], houseZonesCount=len(zonen),
            houseDevicesCount=len(geraete),
            nonGeminiZones=_zonen_modelle(houseId) or None,
            nonGeminiDevices=[_geraet_modell(g, speicher.zustand(g["seriennummer"]))
                              for g in geraete] or None)

    @app.get("/House/house-complete-info", response_model=House)
    def house_complete_info(houseId: int = Query(...),
                            benutzer_id: int = Depends(angemeldet)):
        h = _haus_oder_404(houseId, benutzer_id)
        return _haus_modell(h, _zonen_modelle(houseId),
                            _raeume_mit_geraeten(houseId))

    @app.get("/House/house-devices", response_model=House)
    def house_devices(houseId: int = Query(...),
                      benutzer_id: int = Depends(angemeldet)):
        # Trotz des Namens liefert dieser Endpunkt ein House-Objekt, kein
        # Geräte-Array. So steht es in der Spezifikation, und die App liest es so.
        h = _haus_oder_404(houseId, benutzer_id)
        return _haus_modell(h, _zonen_modelle(houseId),
                            _raeume_mit_geraeten(houseId))

    @app.post("/House/add-house", response_model=House)
    def add_house(anfrage: AddHouseRequest, benutzer_id: int = Depends(angemeldet)):
        if not (anfrage.name or "").strip():
            raise HTTPException(status_code=400, detail="Name fehlt")
        haus_id = speicher.haus_anlegen(benutzer_id, anfrage.name.strip(),
                                        anfrage.address, anfrage.latitude,
                                        anfrage.longitude, anfrage.timezone)
        return _haus_modell(speicher.haus(haus_id, benutzer_id), [], [])

    @app.post("/House/rename-house")
    def rename_house(anfrage: RenameHouseRequest,
                     benutzer_id: int = Depends(angemeldet)):
        _haus_oder_404(anfrage.houseId, benutzer_id)
        speicher.haus_umbenennen(anfrage.houseId, benutzer_id, anfrage.newName or "")
        return {"ok": True}

    @app.post("/House/set-house-timezone")
    def set_house_timezone(anfrage: SetHouseTimezoneRequest,
                           benutzer_id: int = Depends(angemeldet)):
        _haus_oder_404(anfrage.houseId, benutzer_id)
        speicher.haus_zeitzone_setzen(anfrage.houseId, benutzer_id, anfrage.timezone)
        return {"ok": True}

    @app.delete("/House/house")
    def delete_house(houseId: int = Query(...),
                     benutzer_id: int = Depends(angemeldet)):
        if not speicher.haus_loeschen(houseId, benutzer_id):
            raise HTTPException(status_code=404, detail="Haus nicht gefunden")
        return {"ok": True}

    @app.get("/House/user-zones", response_model=List[Zone])
    def user_zones(houseId: int = Query(...),
                   benutzer_id: int = Depends(angemeldet)):
        _haus_oder_404(houseId, benutzer_id)
        return _zonen_modelle(houseId)

    @app.get("/House/zone-devices", response_model=List[Device])
    def zone_devices(zoneId: int = Query(...),
                     benutzer_id: int = Depends(angemeldet)):
        return [_geraet_modell(g, speicher.zustand(g["seriennummer"]))
                for g in speicher.geraete(zone_id=zoneId)]

    @app.get("/House/user-rooms", response_model=List[Room])
    def user_rooms(houseId: int = Query(...),
                   benutzer_id: int = Depends(angemeldet)):
        _haus_oder_404(houseId, benutzer_id)
        return _raeume_mit_geraeten(houseId)

    @app.get("/House/user-free-rooms", response_model=List[Room])
    def user_free_rooms(houseId: int = Query(...),
                        benutzer_id: int = Depends(angemeldet)):
        _haus_oder_404(houseId, benutzer_id)
        return _raeume_mit_geraeten(houseId, nur_freie=True)

    @app.post("/House/add-zone", response_model=Zone)
    def add_zone(anfrage: NewZoneWithRoomsRequest,
                 benutzer_id: int = Depends(angemeldet)):
        _haus_oder_404(anfrage.houseId, benutzer_id)
        zone_id = speicher.zone_anlegen(anfrage.houseId, anfrage.zoneName or "Zone")
        for raum_id in anfrage.roomsId or []:
            speicher.raum_zone_zuweisen(raum_id, zone_id)
        return Zone(id=zone_id, name=anfrage.zoneName,
                    rooms=_raeume_mit_geraeten(anfrage.houseId, zone_id=zone_id)
                    or None)

    @app.post("/House/rename-zone")
    def rename_zone(anfrage: RenameZoneRequest,
                    benutzer_id: int = Depends(angemeldet)):
        speicher.zone_umbenennen(anfrage.zoneId, anfrage.newName or "")
        return {"ok": True}

    @app.delete("/House/delete-zone")
    def delete_zone(zoneId: int = Query(...),
                    benutzer_id: int = Depends(angemeldet)):
        speicher.zone_loeschen(zoneId)
        return {"ok": True}

    @app.post("/House/rename-device")
    def rename_device(anfrage: RenameDeviceRequest,
                      benutzer_id: int = Depends(angemeldet)):
        if not speicher.geraet_umbenennen(anfrage.deviceId, anfrage.newName or ""):
            raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
        return {"ok": True}

    # -- Kopplung: nicht verfügbar ------------------------------------------
    def _kopplung_fehlt():
        return JSONResponse(status_code=501,
                            content={"title": "Kopplung nicht verfügbar",
                                     "status": 501,
                                     "detail": KOPPLUNG_NICHT_VERFUEGBAR})

    @app.post("/House/add-device-room")
    def add_device_room():
        return _kopplung_fehlt()

    @app.get("/House/device-info")
    def device_info():
        return _kopplung_fehlt()

    @app.get("/House/house-config-auto")
    def house_config_auto():
        return _kopplung_fehlt()

    @app.post("/Device/apply-config")
    def apply_config():
        return _kopplung_fehlt()

    @app.post("/Device/apply-config-force-unique")
    def apply_config_force_unique():
        return _kopplung_fehlt()

    # =======================================================================
    # Device
    # =======================================================================
    @app.get("/Device/device-status", response_model=StatusPacket)
    def device_status(deviceSerialNumber: str = Query(...),
                      benutzer_id: int = Depends(angemeldet)):
        if speicher.geraet_nach_seriennummer(deviceSerialNumber) is None:
            raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
        zustand = speicher.zustand(deviceSerialNumber)
        if zustand.gesehen is None:
            # Die Cloud antwortet hier mit 404, wenn noch kein Statuspaket
            # eingetroffen ist. Die App behandelt das als „noch nicht bereit".
            raise HTTPException(status_code=404,
                                detail="Noch kein Statuspaket empfangen")
        return _status_modell(deviceSerialNumber.upper(), zustand)

    @app.get("/Device/house-devices-status", response_model=HouseDevicesInfo)
    def house_devices_status(houseId: int = Query(...),
                             benutzer_id: int = Depends(angemeldet)):
        """Der Endpunkt, den die Übersicht der App abfragt.

        Zwei Zweige, die beide bedient werden müssen: Häuser mit Zonen liefern
        ``zoneDevicesInfo``, Häuser ohne Zonen ``uniqueZoneStatusPacket``.
        """
        _haus_oder_404(houseId, benutzer_id)
        zonen = speicher.zonen(houseId)

        if zonen:
            infos = []
            for z in zonen:
                geraete = speicher.geraete(zone_id=z["id"])
                master = next((g for g in geraete
                               if g["rolle"] == DeviceRole.Master.value),
                              geraete[0] if geraete else None)
                if master is None:
                    continue
                sn = master["seriennummer"]
                infos.append(ZoneDeviceInfo(
                    zone=Zone(id=z["id"], name=z["name"],
                              rooms=_raeume_mit_geraeten(houseId, zone_id=z["id"])
                              or None),
                    statusPacket=_status_modell(sn, speicher.zustand(sn)),
                    zoneDevicesCount=len(geraete), masterSn=sn))
            return HouseDevicesInfo(zoneDevicesInfo=infos or None,
                                    masterSn=infos[0].masterSn if infos else None)

        geraete = speicher.geraete(haus_id=houseId)
        if not geraete:
            return HouseDevicesInfo(uniqueZoneDevicesCount=0)
        master = next((g for g in geraete
                       if g["rolle"] == DeviceRole.Master.value), geraete[0])
        sn = master["seriennummer"]
        return HouseDevicesInfo(
            uniqueZoneStatusPacket=_status_modell(sn, speicher.zustand(sn)),
            uniqueZoneDevicesCount=len(geraete), masterSn=sn)

    @app.post("/Device/change-mode")
    def change_mode(anfrage: ChangeModeRequest,
                    benutzer_id: int = Depends(angemeldet)):
        sn = (anfrage.deviceSerialNumber or "").upper()
        if speicher.geraet_nach_seriennummer(sn) is None:
            raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
        if not bus.verbunden(sn):
            raise HTTPException(status_code=503,
                                detail="Gerät ist derzeit nicht verbunden")
        erfolg = bus.modus_setzen(sn, zu_protokoll(anfrage.operatingMode),
                                  zu_protokoll(anfrage.fanSpeed),
                                  zu_protokoll(anfrage.humidityLevel),
                                  zu_protokoll(anfrage.lightSensorLevel))
        if not erfolg:
            raise HTTPException(status_code=502,
                                detail="Befehl konnte nicht zugestellt werden")
        return {"ok": True}

    @app.get("/Device/reset-filter")
    def reset_filter(deviceSerialNumber: str = Query(...),
                     benutzer_id: int = Depends(angemeldet)):
        sn = deviceSerialNumber.upper()
        if speicher.geraet_nach_seriennummer(sn) is None:
            raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
        if not bus.filter_zuruecksetzen(sn):
            raise HTTPException(status_code=503,
                                detail="Gerät ist derzeit nicht verbunden")
        return {"ok": True}

    @app.get("/Device/send-device-config")
    def send_device_config(deviceSn: str = Query(...),
                           benutzer_id: int = Depends(angemeldet)):
        # Der Parameter heißt hier deviceSn, nicht deviceSerialNumber —
        # Inkonsistenz der Original-API, die nachgebildet werden muss.
        sn = deviceSn.upper()
        if speicher.geraet_nach_seriennummer(sn) is None:
            raise HTTPException(status_code=404, detail="Gerät nicht gefunden")
        if not bus.konfiguration_senden(sn):
            raise HTTPException(status_code=503,
                                detail="Gerät ist derzeit nicht verbunden")
        return {"ok": True}

    # =======================================================================
    # Schedule
    # =======================================================================
    def _zeitfenster_modell(zeile) -> TimeSlot:
        return TimeSlot(id=zeile["id"], dayOfWeek=zeile["wochentag"],
                        startTime=zeile["beginn"], endTime=zeile["ende"],
                        operatingMode=zeile["modus"], fanSpeed=zeile["stufe"],
                        humidityLevel=zeile["feuchtestufe"],
                        lightSensorLevel=zeile["lichtsensor"],
                        scheduleId=zeile["geraet_id"])

    @app.get("/Schedule/{deviceId}", response_model=Schedule)
    def schedule_lesen(deviceId: int = Path(...),
                       benutzer_id: int = Depends(angemeldet)):
        fenster = [_zeitfenster_modell(z) for z in speicher.zeitfenster(deviceId)]
        return Schedule(id=deviceId, deviceId=deviceId, timeSlots=fenster or None)

    @app.post("/Schedule/{deviceId}/timeslots", response_model=TimeSlot)
    def zeitfenster_anlegen(anfrage: TimeSlot, deviceId: int = Path(...),
                            benutzer_id: int = Depends(angemeldet)):
        neu_id = speicher.zeitfenster_anlegen(
            deviceId, anfrage.dayOfWeek.value, anfrage.startTime, anfrage.endTime,
            anfrage.operatingMode.value, anfrage.fanSpeed.value,
            anfrage.humidityLevel.value, anfrage.lightSensorLevel.value)
        return anfrage.model_copy(update={"id": neu_id, "scheduleId": deviceId})

    @app.put("/Schedule/{deviceId}/timeslots/{timeSlotId}")
    def zeitfenster_aendern(anfrage: TimeSlot, deviceId: int = Path(...),
                            timeSlotId: int = Path(...),
                            benutzer_id: int = Depends(angemeldet)):
        geaendert = speicher.zeitfenster_aendern(
            timeSlotId, wochentag=anfrage.dayOfWeek.value,
            beginn=anfrage.startTime, ende=anfrage.endTime,
            modus=anfrage.operatingMode.value, stufe=anfrage.fanSpeed.value,
            feuchtestufe=anfrage.humidityLevel.value,
            lichtsensor=anfrage.lightSensorLevel.value)
        if not geaendert:
            raise HTTPException(status_code=404, detail="Zeitfenster nicht gefunden")
        return {"ok": True}

    @app.delete("/Schedule/{deviceId}/timeslots/{timeSlotId}")
    def zeitfenster_loeschen(deviceId: int = Path(...),
                             timeSlotId: int = Path(...),
                             benutzer_id: int = Depends(angemeldet)):
        if not speicher.zeitfenster_loeschen(timeSlotId):
            raise HTTPException(status_code=404, detail="Zeitfenster nicht gefunden")
        return {"ok": True}

    # -- Betriebszustand des Servers selbst ---------------------------------
    @app.get("/local/health")
    def health():
        """Nicht Teil der Cloud-API — für Betrieb und Fehlersuche."""
        bekannt = speicher.geraete()
        return {
            "status": "ok",
            "geraete_gesamt": len(bekannt),
            "geraete_verbunden": sum(1 for g in bekannt
                                     if bus.verbunden(g["seriennummer"])),
            "benutzer": speicher.benutzer_anzahl(),
        }

    return app
