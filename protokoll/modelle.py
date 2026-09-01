#!/usr/bin/env python3
"""
modelle.py — Datenmodelle der Ambientika-Cloud-API, exakt nachgebildet.

Grundlage ist die veröffentlichte OpenAPI-Spezifikation von
``app.ambientika.eu:4521``. Die Feldnamen sind bewusst **nicht** eingedeutscht
und behalten ihr englisches camelCase: Die bestehende App liest sie so, und
jede Abweichung — auch nur in der Groß-/Kleinschreibung — lässt eine Kachel
leer bleiben, ohne dass irgendwo ein Fehler erscheint.

Zwei Eigenheiten, die man kennen muss
-------------------------------------
**Enums gehen als Zeichenketten über die Leitung.** Die Spezifikation deklariert
sie als ``type: string``. Das Geräteprotokoll auf TCP 11000 dagegen überträgt
numerische Codes. Beide Kodierungen existieren nebeneinander; ``zu_protokoll``
und ``von_protokoll`` übersetzen zwischen ihnen. Wer die beiden verwechselt,
schaltet den Lüfter in den falschen Modus.

**``OperatingMode.Smart`` hat den Ordinalwert 0.** In C# ist das der Standardwert
eines nicht gesetzten Feldes. Ein ``ChangeModeRequest`` ohne ``operatingMode``
landet in der Cloud deshalb auf *Smart*. Damit sich der lokale Server bei
unvollständigen Anfragen genauso verhält, ist dieser Standard hier ausdrücklich
gesetzt und nicht etwa als Pflichtfeld deklariert.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums — Reihenfolge = Ordinalwert, Wert = das, was über REST übertragen wird
# ---------------------------------------------------------------------------
class _OrdinalEnum(str, Enum):
    """Zeichenketten-Enum, das seinen C#-Ordinalwert kennt."""

    @property
    def ordinal(self) -> int:
        return list(type(self)).index(self)

    @classmethod
    def von_ordinal(cls, wert: int) -> "_OrdinalEnum":
        werte = list(cls)
        if not 0 <= wert < len(werte):
            raise ValueError(f"{cls.__name__}: Ordinalwert {wert} liegt außerhalb "
                             f"von 0..{len(werte) - 1}")
        return werte[wert]


class OperatingMode(_OrdinalEnum):
    Smart = "Smart"                              # Ordinal 0 — siehe Modulkopf
    Auto = "Auto"
    ManualHeatRecovery = "ManualHeatRecovery"
    Night = "Night"
    AwayHome = "AwayHome"
    Surveillance = "Surveillance"
    TimedExpulsion = "TimedExpulsion"
    Expulsion = "Expulsion"
    Intake = "Intake"
    MasterSlaveFlow = "MasterSlaveFlow"
    SlaveMasterFlow = "SlaveMasterFlow"
    Off = "Off"


class FanSpeed(_OrdinalEnum):
    Low = "Low"
    Medium = "Medium"
    High = "High"
    Night = "Night"          # modusgebunden, keine frei wählbare Stufe
    Turbo = "Turbo"          # nur wenn das Feature-Flag turboMode aktiv ist


class HumidityLevel(_OrdinalEnum):
    Dry = "Dry"
    Normal = "Normal"
    Moist = "Moist"


class AirQuality(_OrdinalEnum):
    VeryGood = "VeryGood"
    Good = "Good"
    Medium = "Medium"
    Poor = "Poor"
    Bad = "Bad"


class FilterStatus(_OrdinalEnum):
    Good = "Good"
    Medium = "Medium"
    Bad = "Bad"


class DeviceRole(_OrdinalEnum):
    Master = "Master"
    SlaveEqualMaster = "SlaveEqualMaster"
    SlaveOppositeMaster = "SlaveOppositeMaster"
    NotConfigured = "NotConfigured"


class DeviceType(_OrdinalEnum):
    Ghost = "Ghost"
    Diamond = "Diamond"
    Icon = "Icon"
    Gemini = "Gemini"


class DeviceSubtype(_OrdinalEnum):
    NoneSubtype = "None"
    Version100 = "Version100"
    Version160 = "Version160"
    Version200Pwm = "Version200Pwm"
    Version200 = "Version200"


class LightSensorLevelEnum(_OrdinalEnum):
    NotAvailable = "NotAvailable"
    Off = "Off"
    Low = "Low"
    Medium = "Medium"


class PacketType(_OrdinalEnum):
    Connection = "Connection"
    Status = "Status"
    Command = "Command"
    FwVersions = "FwVersions"
    OutsideWeatherRequest = "OutsideWeatherRequest"   # der Wetterkanal
    Unknown = "Unknown"


class ScheduleState(_OrdinalEnum):
    NotAvailable = "NotAvailable"
    Off = "Off"
    On = "On"


class ResetType(_OrdinalEnum):
    ConnectionReset = "ConnectionReset"
    DeviceReset = "DeviceReset"


class DayOfWeek(_OrdinalEnum):
    Sunday = "Sunday"
    Monday = "Monday"
    Tuesday = "Tuesday"
    Wednesday = "Wednesday"
    Thursday = "Thursday"
    Friday = "Friday"
    Saturday = "Saturday"


class RoomNames(_OrdinalEnum):
    Kitchen = "Kitchen"
    LivingRoom = "LivingRoom"
    Bedroom = "Bedroom"
    Bathroom = "Bathroom"
    DinningRoom = "DinningRoom"          # Schreibweise wie im Original
    ChildrenRoom = "ChildrenRoom"
    Bathroom2 = "Bathroom2"
    Bathroom3 = "Bathroom3"
    Bedroom2 = "Bedroom2"
    Bedroom3 = "Bedroom3"
    Bedroom4 = "Bedroom4"
    Study = "Study"
    Laundry = "Laundry"
    Garage = "Garage"
    Basement = "Basement"
    Attic = "Attic"
    GenericRoom1 = "GenericRoom1"
    GenericRoom2 = "GenericRoom2"


class UserOperationType(_OrdinalEnum):
    ConfirmAccount = "ConfirmAccount"
    RecoverPassword = "RecoverPassword"
    ChangeUsername = "ChangeUsername"


# ---------------------------------------------------------------------------
# Übersetzung REST <-> Geräteprotokoll
# ---------------------------------------------------------------------------
def zu_protokoll(wert: _OrdinalEnum) -> int:
    """REST-Zeichenkette -> numerischer Code des Geräteprotokolls."""
    return wert.ordinal


def modus_von_protokoll(code: int) -> OperatingMode:
    return OperatingMode.von_ordinal(code)


def stufe_von_protokoll(code: int) -> FanSpeed:
    return FanSpeed.von_ordinal(code)


def feuchte_von_protokoll(code: int) -> HumidityLevel:
    return HumidityLevel.von_ordinal(code)


def rolle_von_protokoll(code: int) -> DeviceRole:
    return DeviceRole.von_ordinal(code)


def licht_von_protokoll(code: int) -> LightSensorLevelEnum:
    return LightSensorLevelEnum.von_ordinal(code)


def luftguete_von_protokoll(roh: int) -> AirQuality:
    """Der Rohwert 0 bedeutet „Sensor noch nicht bereit", nicht „sehr gut".

    Das Geräteprotokoll zählt ab 1. Ein unbesehen durchgereichter Nullwert
    würde als bester Messwert erscheinen und eine Automatik auf eine Messung
    reagieren lassen, die es nicht gibt.
    """
    if roh <= 0:
        return AirQuality.Medium
    return AirQuality.von_ordinal(min(roh - 1, len(list(AirQuality)) - 1))


def filter_von_protokoll(code: int) -> FilterStatus:
    return FilterStatus.von_ordinal(min(code, 2))


# ---------------------------------------------------------------------------
# Anfragemodelle
# ---------------------------------------------------------------------------
class AuthenticateRequest(BaseModel):
    username: str
    password: Optional[str] = None


class ChangeModeRequest(BaseModel):
    deviceSerialNumber: Optional[str] = None
    # Kein Pflichtfeld: Die Cloud fällt hier auf den C#-Standardwert zurück,
    # und das ist bei OperatingMode ausgerechnet Smart.
    operatingMode: OperatingMode = OperatingMode.Smart
    fanSpeed: FanSpeed = FanSpeed.Low
    humidityLevel: HumidityLevel = HumidityLevel.Dry
    lightSensorLevel: LightSensorLevelEnum = LightSensorLevelEnum.NotAvailable
    isScheduleMode: bool = False


class AddHouseRequest(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timezone: Optional[int] = None


class RenameHouseRequest(BaseModel):
    houseId: int
    newName: Optional[str] = None


class RenameZoneRequest(BaseModel):
    zoneId: int
    newName: Optional[str] = None


class RenameDeviceRequest(BaseModel):
    deviceId: int
    newName: Optional[str] = None


class SetHouseTimezoneRequest(BaseModel):
    houseId: int
    timezone: int


class NewZoneWithRoomsRequest(BaseModel):
    zoneName: Optional[str] = None
    houseId: int
    roomsId: Optional[List[int]] = None


class ResetDeviceRequest(BaseModel):
    deviceSerialNumber: Optional[str] = None
    resetType: ResetType = ResetType.ConnectionReset


# ---------------------------------------------------------------------------
# Antwortmodelle
# ---------------------------------------------------------------------------
class AuthenticateResponse(BaseModel):
    id: int
    firstName: Optional[str] = None
    lastName: Optional[str] = None
    completeName: Optional[str] = None
    username: Optional[str] = None
    jwtToken: Optional[str] = None
    expiresAt: datetime
    userLevel: int = 0


class TokenRefreshResponse(BaseModel):
    jwtToken: Optional[str] = None
    expiresAt: datetime


class TokenInfoResponse(BaseModel):
    username: Optional[str] = None
    expiresAt: datetime


class UserDetailsResponse(BaseModel):
    firstName: Optional[str] = None
    lastName: Optional[str] = None
    username: Optional[str] = None


class FeatureFlagsResponse(BaseModel):
    """Steuert, was die App überhaupt anzeigt.

    Ohne diese Antwort blendet die Oberfläche Funktionen einfach aus — ohne
    Fehlermeldung. Der Wochenzeitplan und die Turbo-Stufe hängen daran.
    """
    rememberMeLogin: bool = True
    resetDeviceEndpoint: bool = True
    weeklyScheduler: bool = True
    improvedRoomList: bool = True
    turboMode: bool = False


class Device(BaseModel):
    id: int
    deviceType: DeviceType = DeviceType.Diamond
    deviceSubtype: DeviceSubtype = DeviceSubtype.Version160
    serialNumber: str
    userId: int = 1
    name: str
    role: DeviceRole = DeviceRole.NotConfigured
    zoneIndex: Optional[int] = None
    installation: datetime = Field(default_factory=datetime.utcnow)
    radioFwVersion: Optional[str] = None
    microFwVersion: Optional[str] = None
    radioAtCommandsFwVersion: Optional[str] = None
    roomId: int = 0


class Room(BaseModel):
    id: int
    name: RoomNames = RoomNames.GenericRoom1
    houseId: int = 0
    userId: int = 1
    devices: Optional[List[Device]] = None
    roomDevicesCount: int = 0


class Zone(BaseModel):
    id: int
    name: Optional[str] = None
    rooms: Optional[List[Room]] = None


class TimeSlot(BaseModel):
    id: int
    dayOfWeek: DayOfWeek = DayOfWeek.Monday
    startTime: str = "00:00:00"          # .NET TimeSpan als Zeichenkette
    endTime: str = "00:00:00"
    operatingMode: OperatingMode = OperatingMode.Smart
    fanSpeed: FanSpeed = FanSpeed.Low
    humidityLevel: HumidityLevel = HumidityLevel.Normal
    lightSensorLevel: LightSensorLevelEnum = LightSensorLevelEnum.NotAvailable
    scheduleId: int = 0


class Schedule(BaseModel):
    id: int = 0
    zoneId: Optional[int] = None
    houseId: Optional[int] = None
    deviceId: Optional[int] = None
    timeSlots: Optional[List[TimeSlot]] = None


class House(BaseModel):
    userId: int = 1
    id: int
    name: str
    zones: Optional[List[Zone]] = None
    rooms: Optional[List[Room]] = None
    schedule: Optional[Schedule] = None
    hasZones: bool = False
    hasDevices: bool = False
    address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    timezone: Optional[int] = None
    ianaTimezone: Optional[str] = None
    currentHouseTime: datetime = Field(default_factory=datetime.utcnow)


class StatusPacket(BaseModel):
    packetType: PacketType = PacketType.Status
    deviceType: DeviceType = DeviceType.Diamond
    deviceSubtype: DeviceSubtype = DeviceSubtype.Version160
    deviceSerialNumber: Optional[str] = None
    operatingMode: OperatingMode = OperatingMode.Smart
    fanSpeed: FanSpeed = FanSpeed.Low
    humidityLevel: HumidityLevel = HumidityLevel.Normal
    temperature: int = 0
    humidity: int = 0
    airQuality: AirQuality = AirQuality.Medium
    humidityAlarm: bool = False
    filtersStatus: FilterStatus = FilterStatus.Good
    nightAlarm: bool = False
    deviceRole: DeviceRole = DeviceRole.Master
    lastOperatingMode: OperatingMode = OperatingMode.Smart
    lightSensorLevel: LightSensorLevelEnum = LightSensorLevelEnum.NotAvailable
    signalStrenght: int = 0              # Tippfehler steht so in der Original-API
    isScheduled: ScheduleState = ScheduleState.NotAvailable
    isTurboAvailable: bool = False


class ZoneDeviceInfo(BaseModel):
    zone: Zone
    statusPacket: StatusPacket
    zoneDevicesCount: int = 0
    masterSn: Optional[str] = None


class HouseDevicesInfo(BaseModel):
    """Der Zustand einer Anlage, wie die App ihn erwartet.

    Zwei Fälle, die beide bedient werden müssen: Häuser **mit** Zonen liefern
    ``zoneDevicesInfo``, Häuser **ohne** Zonen stattdessen
    ``uniqueZoneStatusPacket`` und ``uniqueZoneDevicesCount``. Wer nur einen
    der beiden Zweige füllt, bekommt in der App leere Kacheln.
    """
    zoneDevicesInfo: Optional[List[ZoneDeviceInfo]] = None
    uniqueZoneStatusPacket: Optional[StatusPacket] = None
    uniqueZoneDevicesCount: int = 0
    masterSn: Optional[str] = None
    geminiDevicesInfo: Optional[List[dict]] = None


class HouseInfo(BaseModel):
    houseId: int
    houseName: Optional[str] = None
    houseZonesCount: int = 0
    houseDevicesCount: int = 0
    nonGeminiZones: Optional[List[Zone]] = None
    nonGeminiDevices: Optional[List[Device]] = None
    roomsWithGeminiDevices: Optional[List[Room]] = None
    geminiDevices: Optional[List[Device]] = None


class ProblemDetails(BaseModel):
    type: Optional[str] = None
    title: Optional[str] = None
    status: Optional[int] = None
    detail: Optional[str] = None
    instance: Optional[str] = None
