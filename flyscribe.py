#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Nigel Gibbs
"""Convert DJI flight records (FlightRecord_*.txt, log format 13/14) to CSV, JSON, KML, GPX or GeoJSON.

    flyscribe logs/*.txt             # exports/<log>.csv for every flight record in logs/
    flyscribe --anon logs/*.txt      # exports/<log>.anon.csv, which can be passed on
    flyscribe --no-ids logs/*.txt    # exports/<log>.no-ids.csv: the real track, without the serial numbers
    flyscribe -f kml -o exports/track.kml 'logs/FlightRecord_2025-06-01_[10-30-00].txt'
    flyscribe --info logs/*.txt      # flight summaries, no key needed

Flight records can be anywhere; logs/ is the conventional place.  The unwrapped keys are kept in
keychains/ and the output goes to exports/, both relative to where the tool is run (--keychains and
--exports name other places).  All of it is private: logs and exports say where and when you fly, and
a keychain unlocks its log.

File layout (little-endian):

    prefix, 100 bytes    records offset u64, details length u16, format version u8
    auxiliary block 0    scrambled flight summary ("details") and its signature
    auxiliary block 1    version category u16 (3 = "13.1", 4 = "13.2"), department u8
    records              type u8, length u16, payload, 0xFF
    trailer              int32 12345, then optional snapshot images

A payload is XOR-scrambled with a key derived from its first byte and the record type; its last byte
is a check byte.  Underneath, the body is AES-256-CBC ciphertext.  Record types are grouped into
"feature points", each with its own AES key, and every record continues the CBC stream of the previous
record of its feature point (its IV is that record's last ciphertext block).  The AES keys are in the
log (KeyStorage records) but wrapped with a key only DJI holds: they are unwrapped once through DJI's
keychain API, which needs a free API key (https://developer.dji.com/user -> CREATE APP -> "Open API"),
and cached as keychains/<log>.keychains.json so later runs work offline.

The CSV has one row per OSD record (10 Hz) with the state the other records reported at that moment,
in the column layout of dji-log-parser's CSV export.  A cell stays empty until the log has reported
that value.  Units: metres, m/s (x north, y east, z down), degrees, volts, amperes, mAh, deg C.
- CUSTOM.dateTime is the app's clock when it logged the row, to within a tick (0.1 s).  It is not the
  time of the sample: the app logs samples up to seconds late when the link or the app stalls.
- OSD.flyTime is the flight controller's own timer: 0.1 s per sample, a jump where samples were lost,
  and it can carry on from the previous flight instead of starting at 0.
- OSD.height is the barometric height above the take-off point.  OSD.altitude adds the home point's
  barometric altitude and is not a height above sea level.  The track formats use OSD.height.
- APP.tip holds the app's tips plus flight mode changes and gimbal end stops, APP.warn its warnings
  (DJI Fly's with their code).  CAMERA.isPhoto marks the frame in which a shot starts.
- DETAILS.* repeat the app's own flight summary as stored (DJI Fly 1.21 stores totalDistance in km).

--anon makes an export that does not say whose flight it was or where.  Names, serial numbers, IDs and
addresses are left out, and so are payloads this tool cannot decode.  Positions are moved as if the globe
had been turned to put the flight's first fix at 0N 0E: the track keeps its shape, size and orientation
(great-circle distances to the nanometre; distances on the WGS84 ellipsoid change by up to 1%).  Dates
and times, heights and the app's messages are kept.  The output goes to exports/<log>.anon.<format>.

--no-ids leaves out only what says whose aircraft it was: names, serial numbers and IDs, and a payload
this tool cannot decode if one of them occurs in it as text.  Positions and all other flight data stay as
recorded, so the export still says where and when the flight took place.  The output goes to
exports/<log>.no-ids.<format>.

Layouts follow DJI's FlightRecordParsingLib (github.com/dji-sdk/FlightRecordParsingLib) and
dji-log-parser (github.com/lvauvillier/dji-log-parser).
"""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import math
import os
import struct
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PREFIX_SIZE = 100
END_OF_RECORD = 0xFF
RECORDS_TRAILER = 12345
KEYCHAIN_API = "https://dev.dji.com/openapi/v1/flight-records/keychains"
KEY_STORAGE_RECOVER, SECURITY, KEY_STORAGE = 50, 55, 56
DAMAGED = -1  # pseudo record type: a stretch of the file that could not be split into records

RECORD_NAMES = {
    1: "OSD", 2: "Home", 3: "Gimbal", 4: "RC", 5: "Custom", 6: "Deform", 7: "CenterBattery", 8: "SmartBattery",
    9: "AppTip", 10: "AppWarn", 11: "RCGPS", 12: "RCDebug", 13: "Recover", 14: "AppGPS", 15: "Firmware",
    16: "OFDMDebug", 17: "VisionGroup", 18: "VisionWarn", 19: "MCParams", 20: "AppOperation", 21: "AGOSD",
    22: "SmartBatteryGroup", 24: "AppSeriousWarn", 25: "Camera", 26: "ADSBFlightData", 27: "ADSBFlightOriginal",
    28: "FlyForbidDBuuid", 29: "AppSpecialControlJoyStick", 30: "AppLowFreqCustom", 31: "NavigationModeGrouped",
    32: "GSMissionStatus", 33: "VirtualStick", 34: "GSMissionEvent", 35: "WaypointMissionUpload",
    36: "WaypointUpload", 38: "WaypointMissionDownload", 39: "WaypointDownload", 40: "ComponentSerial",
    41: "AgricultureDisplayField", 43: "AgricultureRadarPush", 44: "AgricultureSpray", 45: "RTKDifference",
    46: "AgricultureFarmMissionInfo", 47: "AgricultureFarmTaskTeam", 48: "AgricultureGroundStationPush",
    49: "OFDM", 50: "KeyStorageRecover", 51: "FlySafeLimitInfo", 52: "FlySafeUnlockLicenseUserAction",
    53: "FlightHubInfo", 54: "GOBusiness", 55: "Security", 56: "KeyStorage", 57: "GOAirLink", 58: "HealthGroup",
    59: "FCIMUInfo", 60: "Navigation", 61: "PerceptionGroup", 62: "RCDisplayField", 63: "FCCommonOSD",
    253: "SummaryData", 254: "FrameData", DAMAGED: "Damaged",
}

# Feature point n is FEATURES[n - 1]; the keychain API calls it FR_Standardization_Feature_<name>_<n>.
FEATURES = ("Base", "Vision", "Waypoint", "Agriculture", "AirLink", "AfterSales", "DJIFlyCustom", "Plaintext",
            "FlightHub", "Gimbal", "RC", "Camera", "Battery", "FlySafe", "Security")


def _feature_table(**groups: tuple[int, ...]) -> dict[int, int]:
    return {kind: FEATURES.index(name) + 1 for name, kinds in groups.items() for kind in kinds}


# Record type -> feature point whose AES key chain encrypts it.  From FlightRecordParsingLib's maps, plus
# 58, 59, 62 and 63 as in dji-log-parser, plus 57, 253 and 254, which neither knows: found by decrypting a
# DJI Fly 1.21 log (skip 57 and the OFDM record after each one fails; skip 253/254 and the Custom record
# after each one fails).  Types missing here are assigned by _learn_features.
FEATURE_OF_V14 = _feature_table(
    Base=(1, 2, 6, 13, 14, 15, 40, 58, 59, 63),
    Vision=(17, 18),
    Waypoint=(31, 32, 34, 35, 36, 38, 39),
    Agriculture=(21, 41, 43, 44, 45, 46, 47, 48),
    AirLink=(49, 57),
    AfterSales=(12, 16, 19, 26, 27),
    DJIFlyCustom=(5, 9, 10, 20, 24, 30, 54, 253, 254),
    FlightHub=(53,),
    Gimbal=(3,),
    RC=(4, 11, 29, 33, 62),
    Camera=(25,),
    Battery=(7, 8, 22),
    FlySafe=(28, 51, 52),
)
FEATURE_OF_V13 = FEATURE_OF_V14 | _feature_table(Base=(3, 4, 7, 8, 11, 25, 29, 33), AfterSales=(22, 28, 51, 52))


def _names(first: int, names: str) -> dict[int, str]:
    """Enum names for consecutive values starting at `first`."""
    return dict(enumerate(names.split(), first))


FLIGHT_MODE = (
    _names(0, "Manual Atti AttiCourseLock AttiHover Hover GPSBlake GPSAtti GPSCourseLock GPSHomeLock GPSHotPoint "
              "AssistedTakeoff AutoTakeoff AutoLanding AttiLanding GPSWaypoint GoHome ClickGo Joystick "
              "GPSAttiWristband Cinematic")
    | _names(23, "AttiLimited Draw GPSFollowMe ActiveTrack TapFly Pano Farming FPV GPSSport GPSNovice ConfirmLanding")
    | _names(35, "TerrainTracking NaviAdvGoHome NaviAdvLanding Tripod TrackHeadlock")
    | {41: "EngineStart", 43: "GPSGentle"}
)
APP_COMMAND = (
    _names(1, "AutoFly AutoLanding HomePointNow HomePointHot HomePointLock GoHome StartMotor StopMotor Calibration "
              "DeformProtecClose DeformProtecOpen DropGoHome DropTakeOff DropLanding DynamicHomePointOpen "
              "DynamicHomePointClose FollowFunctionOpen FollowFunctionClose IOCOpen IOCClose DropCalibration "
              "PackMode UnPackMode EnterManualMode StopDeform")
    | _names(28, "DownDeform UpDeform ForceLanding ForceLanding2")
)
FLIGHT_ACTION = _names(
    0, "None WarningPowerGoHome WarningPowerLanding SmartPowerGoHome SmartPowerLanding LowVoltageLanding "
       "LowVoltageGoHome SeriousLowVoltageLanding RCOnekeyGoHome RCAssistantTakeoff RCAutoTakeoff RCAutoLanding "
       "AppAutoGoHome AppAutoLanding AppAutoTakeoff OutOfControlGoHome ApiAutoTakeoff ApiAutoLanding ApiAutoGoHome "
       "AvoidGroundLanding AirportAvoidLanding TooCloseGoHomeLanding TooFarGoHomeLanding AppWPMission WPAutoTakeoff "
       "GoHomeAvoid PGoHomeFinish VertLowLimitLanding BatteryForceLanding MCProtectGoHome MotorblockLanding "
       "AppRequestForceLanding FakeBatteryLanding RTHComingObstacleLanding IMUErrorRTH")
MOTOR_START_FAILED_CAUSE = (
    _names(0, "None CompassError AssistantProtected DeviceLocked DistanceLimit IMUNeedCalibration IMUSNError "
              "IMUWarning CompassCalibrating AttiError NoviceProtected BatteryCellError BatteryCommuniteError "
              "SeriousLowVoltage SeriousLowPower LowVoltage TempureVolLow SmartLowToLand BatteryNotReady "
              "SimulatorMode PackMode AttitudeAbnormal UnActive FlyForbiddenError BiasError EscError ImuInitError "
              "SystemUpgrade SimulatorStarted ImuingError AttiAngleOver GyroscopeError AcceleratorError "
              "CompassFailed BarometerError BarometerNegative CompassBig GyroscopeBiasBig AcceleratorBiasBig "
              "CompassNoiseBig BarometerNoiseBig InvalidSn")
    | {44: "FlashOperating", 45: "GPSdisconnect", 47: "SDCardException"}
    | _names(61, "IMUNoconnection RCCalibration RCCalibrationException RCCalibrationUnfinished "
                 "RCCalibrationException2 RCCalibrationException3 AircraftTypeMismatch FoundUnfinishedModule")
    | _names(70, "CyroAbnormal BaroAbnormal CompassAbnormal GPSAbnormal NSAbnormal TopologyAbnormal RCNeedCali "
                 "InvalidFloat M600BatTooLittle M600BatAuthErr M600BatCommErr M600BatDifVoltLarge1 "
                 "M600BatDifVoltLarge2 InvalidVersion GimbalGyroAbnormal GimbalESCPitchNonData GimbalESCRollNonData "
                 "GimbalESCYawNonData GimbalFirmwIsUpdating GimbalDisorder GimbalPitchShock GimbalRollShock "
                 "GimbalYawShock IMUcCalibrationFinished")
    | _names(101, "BattVersionError RTKBadSignal RTKDeviationError")
    | _names(112, "ESCCalibrating GPSSignInvalid GimbalIsCalibrating LockByApp StartFlyHeightError "
                  "ESCVersionNotMatch IMUOriNotMatch StopByApp CompassIMUOriNotMatch")
    | _names(122, "CompassIMUOriNotMatch BatteryOverTemperature BatteryInstallError BeImpact")
)
DRONE_TYPE = (
    _names(0, "None Inspire1 Phantom3Advanced Phantom3Pro Phantom3Standard OpenFrame AceOne WKM Naza A2 A3 Phantom4")
    | _names(14, "Matrice600 Phantom34K MavicPro Inspire2 Phantom4Pro")
    | {20: "N3", 21: "Spark", 23: "Matrice600Pro", 24: "MavicAir", 25: "Matrice200", 27: "Phantom4Advanced",
       28: "Matrice210", 29: "Phantom3SE", 30: "Matrice210RTK", 36: "Phantom4ProV2", 41: "Mavic2",
       51: "Mavic2Enterprise", 58: "MavicAir2", 60: "Matrice300RTK", 63: "Mini2", 77: "Mavic3Enterprise",
       84: "Mavic3Pro", 89: "Matrice350RTK", 93: "Mini4Pro", 94: "Avata2"}
    | {112: "Mini5Pro"}  # in neither reference: read off a log whose header names the aircraft "DJI Mini 5 Pro"
)
GO_HOME_STATUS = _names(0, "Standby Preascending Align Ascending Cruise Braking Bypassing")
BATTERY_TYPE = {1: "NonSmart", 2: "Smart"}
NON_GPS_CAUSE = _names(0, "Already Forbid GpsNumNonEnough GpsHdopLarge GpsPositionNonMatch SpeedErrorLarge "
                          "YawErrorLarge CompassErrorLarge")
IMU_INIT_FAIL_REASON = {0: "MonitorError", 1: "CollectingData"} | _names(
    3, "AcceDead CompassDead BarometerDead BarometerNegative CompassModTooLarge GyroBiasTooLarge AcceBiasTooLarge "
       "CompassNoiseTooLarge BarometerNoiseTooLarge WaitingMcStationary AcceMoveTooLarge McHeaderMoved McVibrated")
GIMBAL_MODE = _names(0, "Free FPV YawFollow")
SD_CARD_STATE = _names(0, "Normal NoCard InvalidCard WriteProtected Unformatted Formatting IllegalFileSys Busy Full "
                          "LowSpeed UnknownError IndexMax Initialize SuggestFormat Repairing")
CAMERA_WORK_MODE = _names(0, "Capture Recording Playback Transcode Tuning PowerSave Download XcodePlayback Broadcast")
COMPASS_CALIBRATION_STATE = _names(0, "NotCalibrating Horizontal Vertical Successful")
IOC_MODE = _names(1, "CourseLock HomeLock HotspotSurround")
FAIL_SAFE = _names(0, "Hover Landing GoHome")
PLATFORM = {1: "IOS", 2: "Android", 6: "DJIFly", 10: "Windows", 11: "Mac", 12: "Linux"}
FIRMWARE_SENDER = {1: "Camera", 3: "FlightController", 4: "Gimbal", 6: "RC", 11: "Battery"}
COMPONENT = _names(1, "Camera FlightController RC Battery SSD Gimbal")


class LogError(Exception):
    """The flight record is malformed, unsupported, or cannot be decrypted."""


# --- container ------------------------------------------------------------------------------------------------


@dataclass
class FlightLog:
    version: int  # file format version from the prefix
    category: int  # 3 = "13.1", 4 = "13.2": selects the record type -> feature point table
    department: int  # app family that wrote the log (3 = DJI Fly)
    details: dict  # flight summary from the header
    records: list[tuple[int, bytes]]  # (record type, unscrambled body) in file order


def _crc64_table() -> list[int]:
    table = []
    for crc in range(256):
        for _ in range(8):
            crc = (crc >> 1) ^ (0x95AC9329AC4BC9B5 if crc & 1 else 0)
        table.append(crc)
    return table


_CRC64 = _crc64_table()


def _scramble_key(seed: int, kind: int) -> bytes:
    """8-byte XOR key: CRC-64/Jones over 0x123456789ABCDEF0 * seed, starting from seed + kind."""
    crc = (seed + kind) & 0xFF
    for byte in ((0x123456789ABCDEF0 * seed) & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little"):
        crc = _CRC64[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc.to_bytes(8, "little")


def unscramble(payload: bytes, kind: int) -> bytes | None:
    """Undo the XOR scrambling of a payload (seed byte, body, check byte); None if the check byte is wrong."""
    if len(payload) < 2:
        return None
    seed, size = payload[0], len(payload) - 1
    key = (_scramble_key(seed, kind) * (size // 8 + 1))[:size]
    body = (int.from_bytes(payload[1:], "little") ^ int.from_bytes(key, "little")).to_bytes(size, "little")
    return body[:-1] if body[-1] == (seed + kind) & 0xFF else None


def _auxiliary(data: bytes, pos: int, kind: int) -> tuple[bytes, int]:
    """The auxiliary block of the given kind at pos, and the offset just past it."""
    end = pos + 3 + int.from_bytes(data[pos + 1 : pos + 3], "little")
    if pos + 3 > len(data) or data[pos] != kind or end > len(data):
        raise LogError(f"corrupt header: auxiliary block {kind} missing at offset {pos}")
    return data[pos + 3 : end], end


def _split_records(data: bytes, pos: int, end: int) -> list[tuple[int, bytes]]:
    """(type, unscrambled body) of each record; one (DAMAGED, b"") per unreadable stretch."""
    records: list[tuple[int, bytes]] = []
    while pos < end:
        kind = data[pos]
        stop = pos + 3 + int.from_bytes(data[pos + 1 : pos + 3], "little")
        framed = stop < end and data[stop] == END_OF_RECORD
        body = unscramble(data[pos + 3 : stop], kind) if framed else None
        if body is not None:
            records.append((kind, body))
        elif not records or records[-1][0] != DAMAGED:
            records.append((DAMAGED, b""))
        # A framed record is skipped whole; otherwise resynchronise after the next end-of-record marker.
        pos = stop + 1 if framed else data.find(END_OF_RECORD, pos + 1, end) + 1 or end
    return records


def _text(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


def _f32(value: float) -> float:
    """Shortest decimal that still identifies a 32-bit float: 0.1, not 0.10000000149011612."""
    packed = struct.pack("<f", value)
    for digits in range(1, 10):
        short = float(f"{value:.{digits}g}")
        if struct.pack("<f", short) == packed:
            return short
    return value


def _iso(milliseconds: int) -> str | None:
    """Epoch milliseconds as ISO 8601 UTC; None outside 2010-2100 (unset or garbage clock)."""
    if not 1262304000000 <= milliseconds < 4102444800000:
        return None
    moment = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=milliseconds)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _details(raw: bytes) -> dict:
    raw = raw.ljust(436, b"\0")
    (lines, _, start, lon, lat, distance, duration, max_height, max_h_speed, max_v_speed, photos,
     video) = struct.unpack_from("<2iq2dfi3fiq", raw, 83)
    take_off_altitude, product_type = struct.unpack_from("<fB", raw, 267)
    return {
        "subStreet": _text(raw[0:20]), "street": _text(raw[20:40]), "city": _text(raw[40:60]),
        "area": _text(raw[60:80]), "recordLineCount": lines, "startTime": _iso(start),
        "longitude": lon, "latitude": lat, "totalDistance": _f32(distance), "totalTime": duration / 1000,
        "maxHeight": _f32(max_height), "maxHorizontalSpeed": _f32(max_h_speed),
        "maxVerticalSpeed": _f32(max_v_speed), "photoNum": photos, "videoTime": video,
        "takeOffAltitude": _f32(take_off_altitude / 10), "productType": product_type,
        "aircraftName": _text(raw[280:312]), "aircraftSerial": _text(raw[312:328]),
        "cameraSerial": _text(raw[328:344]), "rcSerial": _text(raw[344:360]), "batterySerial": _text(raw[360:376]),
        "appPlatform": PLATFORM.get(raw[376], raw[376]), "appVersion": ".".join(map(str, raw[377:380])),
        "uuid": _text(raw[400:436]),
    }


def read_log(data: bytes) -> FlightLog:
    """Parse the container: header summary and the unscrambled (still AES-encrypted) records."""
    if len(data) < PREFIX_SIZE:
        raise LogError("not a DJI flight record: shorter than the 100-byte prefix")
    records_offset, _, version = struct.unpack_from("<QHB", data)
    if version < 13:
        raise LogError(f"log format version {version} is not supported (13 and 14 are)")
    block, pos = _auxiliary(data, PREFIX_SIZE, 0)
    info = unscramble(block, 0)
    if info is None:
        raise LogError("corrupt header: the flight summary fails its check byte")
    summary_block = info[3 : 3 + int.from_bytes(info[1:3], "little")]
    block, pos = _auxiliary(data, pos, 1)
    category, department = struct.unpack_from("<HB", block.ljust(3, b"\0"))
    start = records_offset or pos
    # The records end with a trailer; the snapshot images listed in the summary follow it.
    end = len(data) - sum(struct.unpack_from("<8i", summary_block.ljust(436, b"\0"), 147)) - 4
    if end < start or int.from_bytes(data[end : end + 4], "little") != RECORDS_TRAILER:
        end = len(data)
    return FlightLog(version, category, department, _details(summary_block), _split_records(data, start, end))


# --- key chains -----------------------------------------------------------------------------------------------


def keychain_request(log: FlightLog) -> dict:
    """Body for DJI's keychain API: the wrapped AES keys, one list per key chain segment of the log."""
    segments: list[list[dict]] = [[]]
    for kind, body in log.records:
        if kind == KEY_STORAGE:
            feature, size = struct.unpack_from("<HH", body.ljust(4, b"\0"))
            if 1 <= feature <= len(FEATURES):
                segments[-1].append({
                    "featurePoint": f"FR_Standardization_Feature_{FEATURES[feature - 1]}_{feature}",
                    "aesCiphertext": base64.b64encode(body[4 : 4 + size]).decode(),
                })
        elif kind == KEY_STORAGE_RECOVER:  # the app resumed the log with fresh keys
            segments.append([])
    department = log.department if 1 <= log.department <= 8 else 3
    return {"version": log.category, "department": department, "keychainsArray": segments}


def fetch_keychains(request: dict, api_key: str) -> list[list[dict]]:
    """Ask DJI to unwrap the AES keys; [[{featurePoint, aesKey, aesIv}, ...], ...]."""
    headers = {"Content-Type": "application/json", "Api-Key": api_key, "User-Agent": "flyscribe"}
    http_request = urllib.request.Request(KEYCHAIN_API, json.dumps(request).encode(), headers)
    try:
        with urllib.request.urlopen(http_request, timeout=30) as response:
            reply = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 403:
            raise LogError("DJI rejected the API key (HTTP 403)") from None
        message = error.read(300).decode("utf-8", "replace")
        raise LogError(f"DJI keychain API returned HTTP {error.code}: {message}") from None
    except (OSError, ValueError) as error:
        raise LogError(f"DJI keychain API request failed: {error}") from None
    result = reply.get("result") or {}
    if result.get("code") != 0 or not isinstance(reply.get("data"), list):
        raise LogError(f"DJI keychain API error: {result.get('msg', reply)}")
    return reply["data"]


def load_keychains(cache: Path, log: FlightLog, api_key: str | None) -> list[list[dict]]:
    """Keychains from the cache file, else from DJI (then written to it)."""
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except ValueError:
            raise LogError(f"{cache} is not valid JSON; delete it to fetch the keys again") from None
    if not api_key:
        raise LogError(
            f"the records are AES-encrypted and no keychain is cached for this log (looked for {cache}).\n"
            "  Get a free DJI API key: https://developer.dji.com/user -> CREATE APP -> App Type 'Open API',\n"
            "  activate it from the e-mail, copy the App Key, then pass --api-key KEY or set DJI_API_KEY.\n"
            "  The unwrapped keys are then cached in that file, so the key is used once per log."
        )
    keychains = fetch_keychains(keychain_request(log), api_key)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(keychains, indent=1))
    except OSError as error:
        print(f"warning: keychain not cached: {error}", file=sys.stderr)
    return keychains


def _chains(keychain: list[dict]) -> dict[int, list[bytes]]:
    """Feature point number -> [AES key, IV for its next record]."""
    try:
        return {
            int(entry["featurePoint"].rsplit("_", 1)[1]): [
                base64.b64decode(entry["aesKey"]), base64.b64decode(entry["aesIv"])]
            for entry in keychain
        }
    except (KeyError, TypeError, ValueError, IndexError):
        raise LogError("malformed keychain (expected featurePoint, aesKey and aesIv entries)") from None


def _aes_cbc(key: bytes, iv: bytes, data: bytes) -> bytes:
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    except ValueError as error:
        raise LogError(f"malformed keychain: {error}") from None
    return decryptor.update(data) + decryptor.finalize()


def _unpad(plain: bytes) -> bytes | None:
    """Strip PKCS#7 padding; None if it is malformed, which a wrong key or IV makes near certain."""
    pad = plain[-1]
    return plain[:-pad] if 1 <= pad <= 16 and plain.endswith(bytes([pad]) * pad) else None


def _learn_features(records: list[tuple[int, bytes]], segments: list[dict], table: dict[int, int]) -> dict[int, int]:
    """Assign record types that `table` lacks to the key chain that decrypts them.

    A record type that shares a chain but is skipped breaks the IV of the next record on that chain, so
    unknown types must be placed even though their content is not understood.  The last block of a
    multi-block record needs only the key (its IV is the block before it), so each key can be tested
    without knowing the chain state: the right one yields valid padding on every record of the type, a
    wrong one on 1 record in 255.  A type is placed when exactly one key fits.
    """
    samples = defaultdict(list)
    segment = 0
    for kind, body in records:
        if kind == KEY_STORAGE_RECOVER:
            segment += 1
        elif (kind not in table and kind not in (SECURITY, KEY_STORAGE) and segment < len(segments)
              and len(body) >= 32 and len(body) % 16 == 0):
            samples[kind].append((segments[segment], body))
    learned = {}
    for kind, bodies in samples.items():
        fits = [
            feature for feature in range(1, len(FEATURES) + 1)
            if all(feature in chains and _unpad(_aes_cbc(chains[feature][0], body[-32:-16], body[-16:])) is not None
                   for chains, body in bodies)
        ]
        if len(fits) == 1:
            learned[kind] = fits[0]
    return learned


def decrypt_records(log: FlightLog, keychains: list[list[dict]]) -> tuple[list[tuple[int, bytes | None]], Counter]:
    """AES-decrypt the records: (type, plaintext or None) in file order, and a count of (problem, type)."""
    segments = [_chains(keychain) for keychain in keychains]
    table = FEATURE_OF_V13 if log.category == 3 else FEATURE_OF_V14
    table = table | _learn_features(log.records, segments, table)
    chains = segments[0] if segments else {}
    unsynced: set[int] = set()  # chains whose IV was lost in unreadable data
    plain_records: list[tuple[int, bytes | None]] = []
    problems: Counter = Counter()
    for kind, body in log.records:
        plain = None
        aes_sized = bool(body) and len(body) % 16 == 0
        if kind == DAMAGED:
            unsynced = set(chains)
            problems["unreadable data skipped", kind] += 1
        elif kind in (KEY_STORAGE, KEY_STORAGE_RECOVER):
            plain = body
            if kind == KEY_STORAGE_RECOVER:  # the app resumed the log with fresh keys
                segments = segments[1:]
                chains, unsynced = (segments[0] if segments else {}), set()
        elif kind == SECURITY:
            pass  # signature-like blob, no key decrypts it
        elif kind not in table:
            if aes_sized:
                problems["unknown type on an unidentified key chain; the record after it may be wrong", kind] += 1
            else:
                plain = body
        elif table[kind] not in chains:
            problems[f"no {FEATURES[table[kind] - 1]} key in the keychain", kind] += 1
        elif not aes_sized:
            problems["not AES block sized", kind] += 1
        else:
            feature = table[kind]
            key, iv = chains[feature]
            chains[feature][1] = body[-16:]
            if feature in unsynced:  # its IV is unknown, so its first block cannot be trusted
                unsynced.remove(feature)
                problems["dropped, follows unreadable data", kind] += 1
            else:
                plain = _unpad(_aes_cbc(key, iv, body))
                if plain is None:
                    problems["decryption failed (wrong key or broken chain)", kind] += 1
        plain_records.append((kind, plain))
    return plain_records, problems


# --- record payloads ------------------------------------------------------------------------------------------


def _unpack(layout: str, data: bytes) -> tuple:
    """struct.unpack that tolerates other payload lengths: newer firmware appends fields, older has fewer."""
    return struct.unpack_from(layout, data.ljust(struct.calcsize(layout), b"\0"))


def _name(names: dict[int, str], value: int) -> str | int:
    return names.get(value, value)


def _position(longitude: float, latitude: float) -> tuple[float | None, float | None]:
    """Radians to degrees; (None, None) without a fix: 0/0, or the out-of-range sentinel used before GPS lock."""
    longitude, latitude = longitude * 180.0 / math.pi, latitude * 180.0 / math.pi
    if (longitude or latitude) and abs(longitude) <= 180 and abs(latitude) <= 90:
        return longitude, latitude
    return None, None


def _osd(data: bytes) -> dict:
    (lon, lat, height, x_speed, y_speed, z_speed, pitch, roll, yaw, mode, command, b2, b3, b4, b5, gps_num, action,
     motor_fail, b6, battery, vps_height, fly_time, motor_revolution, _, version_c, drone_type,
     imu_fail) = _unpack("<2d7h12BHBH3B", data)
    longitude, latitude = _position(lon, lat)
    return {
        "longitude": longitude, "latitude": latitude,
        "height": height / 10, "xSpeed": x_speed / 10, "ySpeed": y_speed / 10, "zSpeed": z_speed / 10,
        "pitch": pitch / 10, "roll": roll / 10, "yaw": yaw / 10,
        "flycState": _name(FLIGHT_MODE, mode & 0x7F), "rcOutcontrol": bool(mode & 0x80),
        "flycCommand": _name(APP_COMMAND, command) if command else None,
        "canIOCWork": bool(b2 & 0x01), "isOnGround": (b2 >> 1 & 3) < 2, "isMotorOn": bool(b2 & 0x08),
        "isSwaveWork": bool(b2 & 0x10), "goHomeStatus": _name(GO_HOME_STATUS, b2 >> 5),
        "isVisionUsed": bool(b3 & 0x01), "voltageWarning": b3 >> 1 & 3, "isImuPreheated": bool(b3 & 0x10),
        "modeChannel": b3 >> 5 & 3, "isGPSUsed": bool(b3 & 0x80),
        "isCompassError": bool(b4 & 0x01), "waveError": bool(b4 & 0x02), "gpsLevel": b4 >> 2 & 15,
        "batteryType": _name(BATTERY_TYPE, b4 >> 6),
        "isOutOfLimit": bool(b5 & 0x01), "isGoHomeHeightModified": bool(b5 & 0x02),
        "isPropellerCatapult": bool(b5 & 0x04), "isMotorBlocked": bool(b5 & 0x08),
        "isNotEnoughForce": bool(b5 & 0x10), "isBarometerDeadInAir": bool(b5 & 0x20),
        "isVibrating": bool(b5 & 0x40), "isAcceleratorOverRange": bool(b5 & 0x80),
        "gpsNum": gps_num, "flightAction": _name(FLIGHT_ACTION, action),
        "motorStartFailedCause": _name(MOTOR_START_FAILED_CAUSE, motor_fail & 0x7F),
        "nonGPSCause": _name(NON_GPS_CAUSE, b6 & 15), "waypointLimitMode": bool(b6 & 0x10),
        "battery": battery, "vpsHeight": vps_height / 10, "flyTime": fly_time / 10,
        "motorRevolution": motor_revolution, "versionC": version_c, "droneType": _name(DRONE_TYPE, drone_type),
        "imuInitFailReason": _name(IMU_INIT_FAIL_REASON, imu_fail),
    }


def _home(data: bytes) -> dict:
    (lon, lat, altitude, b1, b2, go_home_height, course_lock_angle, sd_state, sd_percent, sd_left_time, index,
     max_height) = _unpack("<2df2BHh2B2H5xf", data)
    longitude, latitude = _position(lon, lat)
    calibrating, ioc = bool(b2 & 0x04), bool(b2 & 0x10)
    return {
        "longitude": longitude, "latitude": latitude, "altitude": _f32(altitude / 10),
        "isHomeRecord": bool(b1 & 0x01), "goHomeMode": "FixedHeight" if b1 & 0x02 else "Normal",
        "aircraftHeadDirection": b1 >> 2 & 1, "isDynamicHomePointEnabled": bool(b1 & 0x08),
        "isNearDistanceLimit": bool(b1 & 0x10), "isNearHeightLimit": bool(b1 & 0x20),
        "isMultipleModeEnabled": bool(b1 & 0x40), "hasGoHome": bool(b1 & 0x80),
        "isCompassCalibrating": calibrating,
        "compassCalibrationState": _name(COMPASS_CALIBRATION_STATE, b2 & 3) if calibrating else None,
        "isBeginnerMode": bool(b2 & 0x08), "isIOCEnabled": ioc, "IOCMode": _name(IOC_MODE, b2 >> 5) if ioc else None,
        "goHomeHeight": go_home_height, "IOCCourseLockAngle": course_lock_angle if ioc else None,
        "flightRecordSdState": sd_state, "recordSdCapacityPercent": sd_percent, "recordSdLeftTime": sd_left_time,
        "currentFlightRecordIndex": index, "maxAllowedHeight": _f32(max_height),
    }


def _gimbal(data: bytes) -> dict:
    pitch, roll, yaw, b1, roll_adjust, yaw_angle, b2, b3 = _unpack("<3hBbhBB", data)
    return {
        "pitch": pitch / 10, "roll": roll / 10, "yaw": yaw / 10, "mode": _name(GIMBAL_MODE, b1 >> 6),
        "rollAdjust": roll_adjust / 10, "yawAngle": yaw_angle / 10,
        "isPitchAtLimit": bool(b2 & 0x01), "isRollAtLimit": bool(b2 & 0x02), "isYawAtLimit": bool(b2 & 0x04),
        "isAutoCalibration": bool(b2 & 0x08), "isStuck": bool(b2 & 0x40), "version": b3 & 15,
    }


def _rc(data: bytes) -> dict:
    aileron, elevator, throttle, rudder, gimbal, b1, b2, b3 = _unpack("<5H3B", data)
    return {
        "aileron": aileron, "elevator": elevator, "throttle": throttle, "rudder": rudder, "gimbal": gimbal,
        "wheelOffset": (b1 >> 1 & 31) * (1 if b1 & 0x40 else -1), "returnBtn": bool(b2 & 0x08),
        "flightModeSwitch": b2 >> 4 & 3, "shutterBtnDown": bool(b3 & 0x40), "recordBtnDown": bool(b3 & 0x80),
    }


def _rc_display_field(data: bytes) -> dict:
    aileron, elevator, throttle, rudder, gimbal = _unpack("<7x5H", data)
    return {"aileron": aileron, "elevator": elevator, "throttle": throttle, "rudder": rudder, "gimbal": gimbal}


def _custom(data: bytes) -> dict:
    _, _, h_speed, distance, timestamp = _unpack("<2b2fq", data)
    return {"dateTime": _iso(timestamp), "hSpeed": _f32(h_speed), "distance": _f32(distance)}


def _center_battery(data: bytes) -> dict:
    (percent, voltage, capacity, full_capacity, life, discharges, error, current, *cells, serial, _, temperature,
     _, _, _, _) = _unpack("<B3HBHIh6H3HB2HB", data)
    return {
        "chargeLevel": percent, "voltage": voltage / 1000, "current": abs(current) / 1000,
        "currentCapacity": capacity, "fullCapacity": full_capacity, "life": life, "numberOfDischarges": discharges,
        "errorType": error, "serialNumber": serial,
        "temperature": round(temperature / 10 - 273.15, 2) if temperature else None,
        "cellVoltages": [cell / 1000 for cell in cells if cell not in (0, 2) and cell <= 10000],
    }


def _smart_battery(data: bytes) -> dict:
    (useful_time, go_home_time, land_time, go_home_battery, land_battery, safe_fly_radius, _, status, go_home_status,
     go_home_countdown, voltage, percent, low_warning, serious_low_warning) = _unpack("<5H2fI2BH3B", data)
    return {
        "usefulTime": useful_time, "goHomeTime": go_home_time, "landTime": land_time,
        "goHomeBattery": go_home_battery, "landBattery": land_battery, "safeFlyRadius": _f32(safe_fly_radius),
        "status": status, "goHomeStatus": go_home_status, "goHomeCountdown": go_home_countdown,
        "voltage": voltage / 1000, "chargeLevel": percent, "lowWarning": low_warning & 0x7F,
        "seriousLowWarning": serious_low_warning & 0x7F,
    }


def _smart_battery_group(data: bytes) -> dict:
    kind, body = (data[0], data[1:]) if data else (0, b"")
    if kind == 1:
        (index, capacity, cycles, full_voltage, _, serial, firmware, life,
         battery_type) = _unpack("<BxIHI2H15x8s2B", body)
        return {"kind": "Static", "index": index, "designedCapacity": capacity, "loopTimes": cycles,
                "fullVoltage": full_voltage, "serialNumber": serial, "versionNumber": firmware.hex(),
                "batteryLife": life, "batteryType": battery_type}
    if kind == 2:
        (index, voltage, current, full_capacity, capacity, temperature, cell_count, percent,
         state) = _unpack("<B2i2Ih2BQ", body)
        return {"kind": "Dynamic", "index": index, "voltage": voltage / 1000, "current": abs(current) / 1000,
                "fullCapacity": full_capacity, "currentCapacity": capacity, "temperature": temperature / 10,
                "cellCount": cell_count, "chargeLevel": percent, "state": state}
    if kind == 3:
        # index, cell count, cells; some records have a pad byte after the index, as the static record does
        head = body.ljust(3, b"\0")
        start = 3 if len(body) == 3 + 2 * head[2] else 2
        cells = struct.unpack_from(f"<{max(0, min(head[start - 1], (len(body) - start) // 2))}H", body, start)
        return {"kind": "SingleVoltage", "index": head[0], "cellVoltages": [cell / 1000 for cell in cells]}
    return {"kind": kind, "raw": body.hex()}


def _camera(data: bytes) -> dict:
    b0, b1, _, _, mode, capacity, remaining, photos, video_time, record_time, camera_type = _unpack("<5B4IHB", data)
    return {
        "isShootingSinglePhoto": (b0 >> 3 & 7) == 1, "isVideo": bool(b0 >> 6),
        "sdCardIsInserted": bool(b1 & 0x02), "sdCardState": _name(SD_CARD_STATE, b1 >> 2 & 15),
        "workMode": _name(CAMERA_WORK_MODE, mode), "sdCardTotalCapacity": capacity,
        "sdCardRemainCapacity": remaining, "remainPhotoNum": photos, "remainVideoTimer": video_time,
        "recordTime": record_time, "cameraType": camera_type,
    }


def _recover(data: bytes) -> dict:
    (_, platform, major, minor, patch, aircraft_serial, aircraft_name, _, camera_serial, rc_serial,
     battery_serial) = _unpack("<5B16s32sq16s16s16s", data)
    return {
        "appPlatform": _name(PLATFORM, platform), "appVersion": f"{major}.{minor}.{patch}",
        "aircraftName": _text(aircraft_name), "aircraftSerial": _text(aircraft_serial),
        "cameraSerial": _text(camera_serial), "rcSerial": _text(rc_serial), "batterySerial": _text(battery_serial),
    }


def _firmware(data: bytes) -> dict:
    """Firmware version of each component, 6 bytes per entry."""
    return {"versions": [
        {"component": _name(FIRMWARE_SENDER, sender), "index": index, "version": ".".join(map(str, version))}
        for sender, index, *version in struct.iter_unpack("<6B", data[: len(data) // 6 * 6])
    ]}


def _component_serial(data: bytes) -> dict:
    component, index, size = _unpack("<3B", data)
    return {"component": _name(COMPONENT, component), "index": index, "serial": _text(data[3 : 3 + size])}


def _mc_params(data: bytes) -> dict:
    fail_safe, flags = _unpack("<2B", data)
    return {"failSafeProtection": _name(FAIL_SAFE, fail_safe), "mvoFuncEnabled": bool(flags & 0x01),
            "avoidObstacleEnabled": bool(flags & 0x02), "userAvoidEnabled": bool(flags & 0x04)}


def _app_gps(data: bytes) -> dict:
    latitude, longitude, accuracy = _unpack("<2df", data)
    return {"latitude": latitude, "longitude": longitude, "horizontalAccuracy": _f32(accuracy)}


def _ofdm(data: bytes) -> dict:
    (signal,) = _unpack("<B", data)
    return {"signalPercent": signal & 0x7F, "isUp": bool(signal & 0x80)}


def _fc_common_osd(data: bytes) -> dict:
    subtype, fly_time, go_home_time, land_time, go_home_capacity, land_capacity = _unpack("<4H2B", data)
    if subtype:
        return {"subtype": subtype, "raw": data[2:].hex()}
    return {"subtype": "BatteryOSD", "remainFlyTime": fly_time, "needGoHomeTime": go_home_time,
            "needLandTime": land_time, "goHomeCapacity": go_home_capacity, "landCapacity": land_capacity}


def _message(data: bytes) -> dict:
    return {"message": data.decode("utf-8", "replace").rstrip("\0")}


def _key_storage(data: bytes) -> dict:
    (feature,) = _unpack("<H", data)
    return {"featurePoint": FEATURES[feature - 1] if 1 <= feature <= len(FEATURES) else feature}


def _protobuf(data: bytes) -> dict[int, list]:
    """Fields of a protobuf message: number -> its values (int for varints, bytes for the rest)."""
    fields: dict[int, list] = defaultdict(list)
    pos = 0

    def varint() -> int:
        nonlocal pos
        value = shift = 0
        while True:
            byte = data[pos]
            pos += 1
            value |= (byte & 0x7F) << shift
            shift += 7
            if byte < 0x80:
                return value

    while pos < len(data):
        tag = varint()
        if tag & 7 == 0:
            fields[tag >> 3].append(varint())
        else:
            size = varint() if tag & 7 == 2 else {1: 8, 5: 4}[tag & 7]
            if pos + size > len(data):
                raise IndexError("truncated protobuf field")
            fields[tag >> 3].append(data[pos : pos + size])
            pos += size
    return fields


def _go_business(data: bytes) -> dict:
    """DJI Fly's app events: a protobuf message with no published schema.  Decodes the two shapes seen in
    DJI Fly 1.21 logs, field 3 = warning shown to the pilot {2: code, 3: text} and field 2 = {1: status
    text such as "In flight"}; anything else stays raw."""
    fields = {}
    try:
        event = _protobuf(data)
        for status in event[2]:
            fields["status"] = _protobuf(status)[1][0].decode()
        for warning in map(_protobuf, event[3]):
            fields.update(code=warning[2][0].decode(), warning=warning[3][0].decode())
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        fields = {}
    return fields or {"raw": data.hex()}


DECODERS = {
    "OSD": _osd, "Home": _home, "Gimbal": _gimbal, "RC": _rc, "Custom": _custom, "CenterBattery": _center_battery,
    "SmartBattery": _smart_battery, "AppTip": _message, "AppWarn": _message, "Recover": _recover,
    "AppGPS": _app_gps, "Firmware": _firmware, "MCParams": _mc_params, "SmartBatteryGroup": _smart_battery_group,
    "AppSeriousWarn": _message, "Camera": _camera, "ComponentSerial": _component_serial, "OFDM": _ofdm,
    "GOBusiness": _go_business, "KeyStorage": _key_storage, "RCDisplayField": _rc_display_field,
    "FCCommonOSD": _fc_common_osd,
}


def decode_records(plain_records: list[tuple[int, bytes | None]]) -> list[dict]:
    """Records as dicts: "type" plus the decoded fields, or "raw" (hex, null if not decrypted) for the rest."""
    records = []
    for kind, plain in plain_records:
        name = RECORD_NAMES.get(kind, kind)
        if plain is not None and name in DECODERS:
            records.append({"type": name, **DECODERS[name](plain)})
        else:
            records.append({"type": name, "raw": None if plain is None else plain.hex()})
    return records


# --- anonymizing ----------------------------------------------------------------------------------------------

# Decoded fields that name the aircraft, one of its parts, the pilot's account or the flight ...
IDS = frozenset({"aircraftName", "aircraftSerial", "cameraSerial", "rcSerial", "batterySerial", "serial",
                 "serialNumber", "uuid"})
# ... and those plus the ones that name a place.
IDENTIFYING = IDS | {"subStreet", "street", "city", "area"}
FLIGHT_RANGE = 100 / 6371  # radians of arc for 100 km: no flight gets that far from its first fix


def _relocate(latitude: float, longitude: float, origin: tuple[float, float]) -> tuple[float | None, float | None]:
    """Where a position lies once the globe is turned to bring `origin` (latitude, longitude) to 0N 0E.

    North at the origin stays north, and a turn keeps every great-circle distance and angle.  A position
    out of the flight's range is a glitch or a placeholder; left in, one at a known place would give the
    turn away, so it becomes (None, None).
    """
    lat0, lat, dlon = (math.radians(angle) for angle in (origin[0], latitude, longitude - origin[1]))
    # The position as a unit vector along the origin's up, east and north.
    up = math.cos(lat0) * math.cos(lat) * math.cos(dlon) + math.sin(lat0) * math.sin(lat)
    east = math.cos(lat) * math.sin(dlon)
    north = math.cos(lat0) * math.sin(lat) - math.sin(lat0) * math.cos(lat) * math.cos(dlon)
    if math.atan2(math.hypot(east, north), up) > FLIGHT_RANGE:
        return None, None
    # To 14 decimal places, about a nanometre: the digits beyond are rounding noise.
    return round(math.degrees(math.asin(north)), 14), round(math.degrees(math.atan2(east, up)), 14)


def anonymize(details: dict, records: list[dict]) -> tuple[dict, list[dict]]:
    """Copies of the flight summary and the records that do not say whose flight it was or where.

    Names, serial numbers, IDs and addresses become None, and so do payloads that are not decoded: what
    they hold is unknown.  The positions of the aircraft and of the home point are turned with the globe
    so that the first one lies at 0N 0E (see _relocate); any other position becomes None.  Dates and
    times, heights and the app's messages are kept.
    """
    def has_fix(fields: dict) -> bool:  # an aircraft or home position, which _position has checked
        return fields.get("type") in ("OSD", "Home") and fields.get("latitude") is not None

    origin = next(((record["latitude"], record["longitude"]) for record in records if has_fix(record)), None)

    def clean(fields: dict) -> dict:
        fields = {key: None if key in IDENTIFYING or key == "raw" else value for key, value in fields.items()}
        if "latitude" in fields:
            position = _relocate(fields["latitude"], fields["longitude"], origin) if has_fix(fields) else (None, None)
            fields["latitude"], fields["longitude"] = position
        return fields

    return clean(details), [clean(record) for record in records]


def strip_ids(details: dict, records: list[dict]) -> tuple[dict, list[dict]]:
    """Copies of the flight summary and the records that do not say whose aircraft it was.

    Names, serial numbers and IDs become None, and so does a payload that is not decoded if one of them
    occurs in it as text.  Positions, addresses and every other field stay as recorded.
    """
    ids = {value.encode() for fields in (details, *records) for key, value in fields.items()
           if key in IDS and value and isinstance(value, str)}

    def clean(fields: dict) -> dict:
        payload = bytes.fromhex(fields.get("raw") or "")
        return {key: None if key in IDS or (key == "raw" and any(text in payload for text in ids)) else value
                for key, value in fields.items()}

    return clean(details), [clean(record) for record in records]


# Command-line option -> what it does to the summary and the records before any output is made.
PRIVACY = {"anon": anonymize, "no-ids": strip_ids}


# --- frames ---------------------------------------------------------------------------------------------------

CELLS = "BATTERY.cellVoltages"  # a list; written as BATTERY.cellVoltage1..N


def _columns(group: str, names: str) -> list[str]:
    return [f"{group}.{name}" for name in names.split()]


COLUMNS = [
    "CUSTOM.dateTime",
    *_columns("OSD", "flyTime latitude longitude height heightMax vpsHeight altitude xSpeed xSpeedMax ySpeed "
                     "ySpeedMax zSpeed zSpeedMax pitch roll yaw flycState flycCommand flightAction isGPSUsed "
                     "nonGPSCause gpsNum gpsLevel droneType isSwaveWork waveError goHomeStatus batteryType "
                     "isOnGround isMotorOn isMotorBlocked motorStartFailedCause isImuPreheated imuInitFailReason "
                     "isAcceleratorOverRange isBarometerDeadInAir isCompassError isGoHomeHeightModified canIOCWork "
                     "isNotEnoughForce isOutOfLimit isPropellerCatapult isVibrating isVisionUsed voltageWarning"),
    *_columns("GIMBAL", "mode pitch roll yaw isPitchAtLimit isRollAtLimit isYawAtLimit isStuck"),
    *_columns("CAMERA", "isPhoto isVideo sdCardIsInserted sdCardState"),
    *_columns("RC", "downlinkSignal uplinkSignal aileron elevator throttle rudder"),
    *_columns("BATTERY", "chargeLevel voltage current currentCapacity fullCapacity cellNum isCellVoltageEstimated"),
    CELLS,
    *_columns("BATTERY", "cellVoltageDeviation maxCellVoltageDeviation temperature minTemperature maxTemperature"),
    *_columns("HOME", "latitude longitude altitude heightLimit isHomeRecord goHomeMode isDynamicHomePointEnabled "
                      "isNearDistanceLimit isNearHeightLimit isCompassCalibrating compassCalibrationState "
                      "isMultipleModeEnabled isBeginnerMode isIOCEnabled IOCMode goHomeHeight IOCCourseLockAngle "
                      "maxAllowedHeight currentFlightRecordIndex"),
    *_columns("RECOVER", "appPlatform appVersion aircraftName aircraftSerial cameraSerial rcSerial batterySerial"),
    *_columns("APP", "tip warn"),
    *_columns("DETAILS", "totalTime totalDistance maxHeight maxHorizontalSpeed maxVerticalSpeed photoNum videoTime "
                         "aircraftName aircraftSerial cameraSerial rcSerial appPlatform appVersion"),
]


def _finish(frame: dict) -> dict:
    """Fill the columns derived from the rest of the frame."""
    for column in ("OSD.height", "OSD.xSpeed", "OSD.ySpeed", "OSD.zSpeed"):
        frame[f"{column}Max"] = max(frame[f"{column}Max"] or 0.0, frame[column])
    if frame["HOME.altitude"] is not None:
        frame["OSD.altitude"] = _f32(frame["OSD.height"] + frame["HOME.altitude"])
    cells, temperature = frame[CELLS], frame["BATTERY.temperature"]
    if cells:
        deviation = round((max(cells) - min(cells)) * 1000) / 1000
        frame.update({
            "BATTERY.cellNum": len(cells), "BATTERY.isCellVoltageEstimated": False,
            "BATTERY.cellVoltageDeviation": deviation,
            "BATTERY.maxCellVoltageDeviation": max(frame["BATTERY.maxCellVoltageDeviation"] or 0.0, deviation),
        })
    if temperature is not None:
        low, high = frame["BATTERY.minTemperature"], frame["BATTERY.maxTemperature"]
        frame["BATTERY.minTemperature"] = temperature if low is None else min(low, temperature)
        frame["BATTERY.maxTemperature"] = temperature if high is None else max(high, temperature)
    return frame


def build_frames(records: list[dict], details: dict) -> list[dict]:
    """One frame per OSD record: that sample plus the state the other records report until the next one.

    Frames are dicts keyed by COLUMNS; None means the log has not reported the value yet.  The aircraft's
    other records follow the OSD record of their sample, so their latest report counts.  The app's clock,
    the sticks and the link quality are logged by the controller, which carries on while telemetry is late
    or lost: of those a frame takes the first report, and further ones belong to the next frame.
    CAMERA.isPhoto and the APP messages are events of the frame they occur in.
    """
    # Multi-battery aircraft log one SmartBatteryGroup stream per battery; index 1 carries the pack values.
    indexes = {r["index"] for r in records if r["type"] == "SmartBatteryGroup" and r.get("kind") == "Dynamic"}
    frames: list[dict] = []
    frame = dict.fromkeys(COLUMNS)
    frame.update({CELLS: [], "APP.tip": "", "APP.warn": ""})
    reported: set[str] = set()  # controller-side columns already reported in this frame
    later: dict = {}  # and their newer values, for the next frame
    shooting = False  # single-photo flag of the previous Camera record

    def put(group: str, fields: dict) -> None:
        frame.update({f"{group}.{key}": value for key, value in fields.items() if f"{group}.{key}" in frame})

    def put_first(group: str, fields: dict) -> None:
        for key, value in fields.items():
            column = f"{group}.{key}"
            if column in frame:
                (later if column in reported else frame)[column] = value
                reported.add(column)

    def say(column: str, message: str) -> None:
        frame[column] = f"{frame[column]}; {message}" if frame[column] else message

    put("DETAILS", details)
    for record in records:
        kind = record["type"]
        if "raw" in record:
            continue
        if kind == "OSD":
            if frame["OSD.flyTime"] is not None:
                frames.append(_finish(frame))
                frame = frame | {"APP.tip": "", "APP.warn": ""}
                if frame["CAMERA.isPhoto"]:
                    frame["CAMERA.isPhoto"] = False
            frame.update(later)
            later.clear()
            reported.clear()
            mode = record["flycState"]
            if mode != frame["OSD.flycState"]:
                say("APP.tip", f"Flight mode changed to {mode if isinstance(mode, str) else f'Unknown({mode})'}.")
            put("OSD", record)
        elif kind == "Home":
            if record["latitude"] is None:  # no fix yet: keep the last valid home point
                record = {key: value for key, value in record.items() if key not in ("latitude", "longitude")}
            put("HOME", record | {"heightLimit": record["maxAllowedHeight"]})
        elif kind == "Gimbal":
            for axis in ("Pitch", "Roll", "Yaw"):
                if record[f"is{axis}AtLimit"] and not frame[f"GIMBAL.is{axis}AtLimit"]:
                    say("APP.tip", f"Gimbal {axis.lower()} axis endpoint reached.")
            put("GIMBAL", record)
        elif kind == "Camera":
            photo = record["isShootingSinglePhoto"] and not shooting  # the flag is held for seconds per photo
            shooting = record["isShootingSinglePhoto"]
            put("CAMERA", record | {"isPhoto": photo or bool(frame["CAMERA.isPhoto"])})
        elif kind in ("RC", "RCDisplayField"):
            put_first("RC", record)
        elif kind in ("CenterBattery", "SmartBattery"):
            put("BATTERY", record)
        elif kind == "SmartBatteryGroup":
            if record["kind"] in ("Dynamic", "SingleVoltage") and (len(indexes) < 2 or record["index"] == 1):
                put("BATTERY", record)
        elif kind == "OFDM":
            put_first("RC", {"uplinkSignal" if record["isUp"] else "downlinkSignal": record["signalPercent"]})
        elif kind == "Custom":
            if record["dateTime"]:
                put_first("CUSTOM", record)
        elif kind == "Recover":
            put("RECOVER", record)
        elif kind == "AppTip":
            say("APP.tip", record["message"])
        elif kind in ("AppWarn", "AppSeriousWarn"):
            say("APP.warn", record["message"])
        elif kind == "GOBusiness":
            if "warning" in record:
                say("APP.warn", f"{record['warning']} ({record['code']})")
    if frame["OSD.flyTime"] is not None:
        frames.append(_finish(frame))
    return frames


# --- output ---------------------------------------------------------------------------------------------------


def to_csv(frames: list[dict]) -> str:
    cell_count = max((len(frame[CELLS]) for frame in frames), default=0)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(
        name for column in COLUMNS
        for name in ([f"BATTERY.cellVoltage{n}" for n in range(1, cell_count + 1)] if column == CELLS else [column])
    )
    for frame in frames:
        row: list = []
        for column in COLUMNS:
            if column == CELLS:
                row += frame[CELLS] + [None] * (cell_count - len(frame[CELLS]))
            else:
                row.append(frame[column])
        writer.writerow("" if cell is None else str(cell).lower() if isinstance(cell, bool) else cell for cell in row)
    return out.getvalue()


def to_json(version: int, details: dict, records: list[dict]) -> str:
    return json.dumps({"version": version, "details": details, "records": records}, indent=1)


def _track(frames: list[dict]) -> list[tuple[str | None, float, float, float]]:
    """(time, longitude, latitude, height above take-off) of every frame with a position fix."""
    return [(frame["CUSTOM.dateTime"], frame["OSD.longitude"], frame["OSD.latitude"], frame["OSD.height"])
            for frame in frames if frame["OSD.latitude"] is not None]


def _title(details: dict) -> str:
    return " ".join(filter(None, (details["aircraftName"], details["startTime"]))) or "DJI flight"


def _decimal(value: float) -> str:
    """The shortest digits that read back as `value`, never with an exponent: GPX numbers may not have one."""
    return format(Decimal(repr(value)), "f")


def _xml(root: ET.Element) -> str:
    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode() + "\n"


def to_kml(frames: list[dict], details: dict) -> str:
    kml = ET.Element("kml", xmlns="http://www.opengis.net/kml/2.2")
    placemark = ET.SubElement(ET.SubElement(kml, "Document"), "Placemark")
    ET.SubElement(placemark, "name").text = _title(details)
    line = ET.SubElement(placemark, "LineString")
    ET.SubElement(line, "altitudeMode").text = "relativeToGround"
    ET.SubElement(line, "coordinates").text = " ".join(
        ",".join(map(_decimal, point)) for _, *point in _track(frames))
    return _xml(kml)


def to_gpx(frames: list[dict], details: dict) -> str:
    gpx = ET.Element("gpx", xmlns="http://www.topografix.com/GPX/1/1", version="1.1", creator="flyscribe")
    track = ET.SubElement(gpx, "trk")
    ET.SubElement(track, "name").text = _title(details)
    ET.SubElement(track, "desc").text = "ele is the barometric height above the take-off point"
    segment = ET.SubElement(track, "trkseg")
    for time, longitude, latitude, height in _track(frames):
        point = ET.SubElement(segment, "trkpt", lat=_decimal(latitude), lon=_decimal(longitude))
        ET.SubElement(point, "ele").text = _decimal(height)
        if time:
            ET.SubElement(point, "time").text = time
    return _xml(gpx)


def to_geojson(frames: list[dict], details: dict) -> str:
    track = _track(frames)
    feature = {
        "type": "Feature",
        "properties": details | {"name": _title(details), "coordTimes": [time for time, *_ in track]},
        "geometry": {"type": "LineString", "coordinates": [list(point) for _, *point in track]} if track else None,
    }
    return json.dumps({"type": "FeatureCollection", "features": [feature]})


# --- command line ---------------------------------------------------------------------------------------------

FORMATS = ("csv", "json", "kml", "gpx", "geojson")


def summary(log: FlightLog, privacy: str | None) -> dict:
    """What the log reveals without AES keys."""
    counts = Counter(RECORD_NAMES.get(kind, str(kind)) for kind, _ in log.records)
    request = keychain_request(log)
    return {
        "version": log.version, "category": log.category, "department": log.department,
        "details": PRIVACY[privacy](log.details, [])[0] if privacy else log.details,
        "records": dict(counts.most_common()),
        "keyChains": [[entry["featurePoint"] for entry in segment] for segment in request["keychainsArray"]],
    }


def convert(path: Path, fmt: str, keychain_dir: Path, api_key: str | None,
            privacy: str | None) -> tuple[str, list[str]]:
    """The log converted to `fmt`, after PRIVACY[privacy] if any, and warnings about records that could not be used."""
    log = read_log(path.read_bytes())
    cache = keychain_dir / f"{path.stem}.keychains.json"
    plain_records, problems = decrypt_records(log, load_keychains(cache, log, api_key))
    warnings = [f"{RECORD_NAMES.get(kind, kind)} x{count}: {problem}"
                for (problem, kind), count in sorted(problems.items())]
    details, records = log.details, decode_records(plain_records)
    if privacy:
        details, records = PRIVACY[privacy](details, records)
    if fmt == "json":
        return to_json(log.version, details, records), warnings
    frames = build_frames(records, details)
    if not frames:
        raise LogError("no OSD records could be decoded" + "".join(f"\n  {warning}" for warning in warnings))
    if fmt == "csv":
        return to_csv(frames), warnings
    return {"kml": to_kml, "gpx": to_gpx, "geojson": to_geojson}[fmt](frames, details), warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert DJI flight records (FlightRecord_*.txt, format 13/14) to CSV, JSON, KML, GPX or "
                    "GeoJSON.",
        epilog="The first conversion of a log needs a DJI API key (free: https://developer.dji.com/user -> CREATE "
               "APP -> 'Open API'); the unwrapped keys are then cached in the keychains directory.")
    parser.add_argument("logs", nargs="+", type=Path, metavar="LOG", help="flight record .txt file")
    parser.add_argument("-f", "--format", choices=FORMATS, default="csv",
                        help="csv: 10 Hz telemetry table (default); json: every decoded record; "
                             "kml, gpx, geojson: flight track")
    parser.add_argument("-o", "--output", type=Path,
                        help="output file, '-' for stdout (default: LOG.FORMAT in the exports directory, "
                             "LOG.anon.FORMAT with --anon, LOG.no-ids.FORMAT with --no-ids)")
    parser.add_argument("--exports", type=Path, default=Path("exports"), metavar="DIR",
                        help="directory the output files go to (default: exports)")
    parser.add_argument("--keychains", type=Path, default=Path("keychains"), metavar="DIR",
                        help="directory the unwrapped keys are cached in (default: keychains)")
    parser.add_argument("--api-key", default=os.environ.get("DJI_API_KEY"),
                        help="DJI API key (default: $DJI_API_KEY)")
    parser.add_argument("--info", action="store_true",
                        help="print the unencrypted flight summary as JSON and exit; needs no key")
    privacy = parser.add_mutually_exclusive_group()
    privacy.add_argument("--anon", dest="privacy", action="store_const", const="anon",
                         help="leave out what says whose flight it was or where: names, serial numbers, IDs and "
                              "undecoded data are dropped, positions moved so that the flight starts at 0N 0E")
    privacy.add_argument("--no-ids", dest="privacy", action="store_const", const="no-ids",
                         help="leave out what says whose aircraft it was: names, serial numbers and IDs are "
                              "dropped, positions and all other flight data stay as recorded")
    args = parser.parse_args(argv)
    if args.output and len(args.logs) > 1:
        parser.error("-o/--output takes a single LOG")
    failed = False
    for path in args.logs:
        try:
            if args.info:
                print(json.dumps(summary(read_log(path.read_bytes()), args.privacy), indent=2))
                continue
            text, warnings = convert(path, args.format, args.keychains, args.api_key, args.privacy)
            for warning in warnings:
                print(f"{path}: warning: {warning}", file=sys.stderr)
            if args.output == Path("-"):
                sys.stdout.write(text)
                continue
            target = args.output or args.exports / ".".join(filter(None, (path.stem, args.privacy, args.format)))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="")
            print(f"{path} -> {target}", file=sys.stderr)
        except (LogError, OSError) as error:
            print(f"{path}: {error}", file=sys.stderr)
            failed = True
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
