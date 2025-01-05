#!/usr/bin/env python3 -u
# -*- coding:utf-8 -*-

"""
PiAware e-Paper Status
"""

import os
import sys
import time
import signal
import socket
import logging

from math import asin, cos, radians, sin, sqrt
from typing import Union, NamedTuple
from datetime import datetime, timedelta
from urllib.parse import urlparse
from urllib3.util import Retry

import epaper
import requests
import slack_sdk
import sentry_sdk
from RPi import GPIO
from requests.adapters import HTTPAdapter
from PIL import Image, ImageDraw, ImageFont


LOGLEVEL = os.getenv("LOGLEVEL", "INFO").upper()
PATH_ROOT = os.path.dirname(os.path.realpath(__file__))
SENTRY_DSN = os.getenv("SENTRY_DSN")
PIAWARE_HOST = os.getenv("PIAWARE_HOST", "http://127.0.0.1:8080")
PIAWARE_BACKOFF = os.getenv("PIAWARE_BACKOFF", "1.0")
PIAWARE_RETRIES = os.getenv("PIAWARE_RETRIES", "10")
FLIGHTRADAR_HOST = os.getenv("FLIGHTRADAR_HOST", "http://127.0.0.1:8754")
RUNNING_IN_DOCKER = os.path.exists('/.dockerenv')
EMERGENCY_SQUAWK = {
    "7500": "Unlawful interference (hijacking)",
    "7600": "Aircraft has lost verbal communication",
    "7700": "General emergency",
}
ICAO_OF_SPECIAL_INTEREST = {
    "3EA12C": "Luftwaffe A350-900 VIP 10+01 Konrad Adenauer",
    "3F5D91": "Luftwaffe A350-900 VIP 10+02 Theodor Heuss",
    "3E854F": "Luftwaffe A350-900 VIP 10+03 Kurt Schumacher",
}
REGISTRATION_OF_SPECIAL_INTEREST = {
    # 'A7BHN': 'QTR85 Qatar DUS (REG)'
}


class Position(NamedTuple):
    """
    An arbitrary Position in Latitude / Longitude
    """

    latitude: float
    longitude: float


class PiAware:
    """
    PiAware Status e-Paper Module
    """

    receiver_position = None
    """ Own Receiver Position """

    class Helpers:
        @staticmethod
        def trim(string: str) -> str:
            """Trim a String (remove leading/trailing spaces)"""
            return str(string).strip()

        @staticmethod
        def ucfirst(string: str) -> str:
            """Capitalize first Letter of String"""
            return string[0].upper() + string[1:]

        @staticmethod
        def format_slack_field(key: str, value: str) -> str:
            """Format a Key/Value to be represented as a Slack Field"""
            return f"*{key}:*\n{value}"

        @staticmethod
        def check_supported(needle: str, haystack: list) -> bool:
            """Find a Value in a List or throw Error"""
            if needle in haystack:
                return True

            raise ValueError(f'Value "{needle}" unsupported. Supported: {haystack}')

        @staticmethod
        def contains_any(needle: str, haystack: list):
            """Check if `haystack` contains `needle`, compared in UPPER case"""
            return any(element.upper() == needle.upper() for element in haystack)

        @staticmethod
        def bool_from_env(needle: str = "HOME") -> bool:
            """Build a Boolean from an Environment Variable"""
            wanted = str(needle).upper()

            return os.getenv(wanted, "False").lower() in ("true", "1", "t")

        @staticmethod
        def is_valid_url(url: str) -> bool:
            """Check if URL is valid"""
            try:
                result = urlparse(url)
                return all([result.scheme, result.netloc])
            except:
                return False

        @staticmethod
        def to_kilometers(meters: float, decimals: int = 2) -> float:
            return round(float(meters) / 1000, decimals)

        @staticmethod
        def get_local_ip():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("192.168.255.255", 1))
                IP = s.getsockname()[0]
            except:
                IP = "127.0.0.1"
            finally:
                s.close()

            return IP

        @staticmethod
        def haversine_distance(
            pos1: Position, pos2: Position, radius: float = 6371.0e3
        ) -> float:
            """
            Calculate the distance between two points on a sphere (e.g. Earth).
            If no radius is provided then the default Earth radius, in meters, is
            used.

            The haversine formula provides great-circle distances between two points
            on a sphere from their latitudes and longitudes using the law of
            haversines, relating the sides and angles of spherical triangles.

            `Reference <https://en.wikipedia.org/wiki/Haversine_formula>`_

            :param pos1: a Position tuple defining (lat, lon) in decimal degrees
            :param pos2: a Position tuple defining (lat, lon) in decimal degrees
            :param radius: radius of sphere in meters.

            :returns: distance between two points in meters.
            :rtype: float
            """
            lat1, lon1, lat2, lon2 = [radians(x) for x in (*pos1, *pos2)]

            hav = (
                sin((lat2 - lat1) / 2.0) ** 2
                + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2.0) ** 2
            )
            distance = 2 * radius * asin(sqrt(hav))

            return distance

        @staticmethod
        def download(
            url: str, retry: int = None, backoff: float = None, bust: bool = True
        ) -> Union[requests.Response, bool]:
            """Download File through Requests (mainly used with json)"""

            if not PiAware.Helpers.is_valid_url(url):
                raise RuntimeError(f"URL {url} is invalid")

            session = requests.Session()

            if bust is True:
                cache_bust = {"ts": time.time()}
            else:
                cache_bust = None

            if retry is None:
                retry = int(PIAWARE_RETRIES)

            if backoff is None:
                backoff = float(PIAWARE_BACKOFF)

            session.mount(
                PIAWARE_HOST,
                HTTPAdapter(max_retries=Retry(total=retry, backoff_factor=backoff)),
            )

            try:
                content = session.get(url, params=cache_bust)

                if content.status_code == 200:
                    return content
                else:
                    return False
            except Exception as e:
                logging.error('Error GETing "%s" (error: %s)', url, str(e))

                return False

        @staticmethod
        def send_slack_notification(
            icao_reg: str,
            callsign: str,
            squawk: str,
            distance: str,
            mode: str = "emergency",
        ) -> bool:
            """Send a Slack Notification"""

            slack_token = os.getenv("SLACK_BOT_TOKEN")
            slack_channel = os.getenv("SLACK_CHANNEL")

            if slack_token is not None and slack_channel is not None:
                PiAware.Helpers.check_supported(
                    mode, ["emergency", "registration", "icao"]
                )

                try:
                    client = slack_sdk.WebClient(token=slack_token)

                    piaware_host_parsed = urlparse(PIAWARE_HOST)
                    piaware_host = piaware_host_parsed.netloc

                    if mode == "emergency":
                        summary = f"ICAO Emergency Squawk {squawk}"
                        header_block = f"ICAO Emergency Squawk on {piaware_host}"
                        description_block = EMERGENCY_SQUAWK[squawk]
                    elif mode == "registration":
                        summary = f"Registration of Special Interest {callsign}"
                        header_block = (
                            f"Registration of Special Interest on {piaware_host}"
                        )
                        description_block = REGISTRATION_OF_SPECIAL_INTEREST[callsign]
                    else:
                        summary = f"Flight of Special Interest {callsign}"
                        header_block = f"Flight of Special Interest on {piaware_host}"
                        description_block = ICAO_OF_SPECIAL_INTEREST[icao_reg]

                    if str(distance).lower() == "unknown":
                        distance_field = distance
                    else:
                        distance_field = f"{distance} km"

                    message_blocks = [
                        slack_sdk.models.blocks.blocks.HeaderBlock(text=header_block),
                        slack_sdk.models.blocks.blocks.SectionBlock(
                            text=description_block
                        ),
                        slack_sdk.models.blocks.blocks.DividerBlock(),
                        slack_sdk.models.blocks.blocks.SectionBlock(
                            fields=[
                                PiAware.Helpers.format_slack_field(
                                    "Aircraft", icao_reg
                                ),
                                PiAware.Helpers.format_slack_field(
                                    "Callsign", callsign
                                ),
                                PiAware.Helpers.format_slack_field("Squawk", squawk),
                                PiAware.Helpers.format_slack_field(
                                    "Distance", distance_field
                                ),
                                PiAware.Helpers.format_slack_field(
                                    "Tar1090",
                                    f"<{PIAWARE_HOST}/tar1090/?icao={icao_reg}|Open>",
                                ),
                                PiAware.Helpers.format_slack_field(
                                    "SkyAware", f"<{PIAWARE_HOST}/skyaware/|Open>"
                                ),
                            ]
                        ),
                    ]

                    response = client.chat_postMessage(
                        text=summary,
                        blocks=message_blocks,
                        channel=slack_channel,
                    )

                    return bool(response.get("ok", False)) is True
                except Exception as e:
                    logging.error("Unable to Send Slack Message: %s", str(e))
                    return False
            else:
                return False

        @staticmethod
        def distance(
            origin: Position, mode: str = "max", threshold: int = 120
        ) -> float:
            PiAware.Helpers.check_supported(mode, ["min", "max"])

            aircrafts = PiAware.get_aircrafts(raw=True)
            if mode == "max":
                aircraft_range = 0
            else:
                aircraft_range = sys.maxsize

            for aircraft in aircrafts["aircraft"]:
                if {"lat", "lon", "seen_pos"}.issubset(aircraft):
                    if aircraft["seen_pos"] <= threshold:
                        distance = PiAware.Helpers.haversine_distance(
                            origin, Position(aircraft["lat"], aircraft["lon"])
                        )
                        if mode == "max":
                            if distance > aircraft_range:
                                aircraft_range = distance
                        else:
                            if distance < aircraft_range:
                                aircraft_range = distance

            logging.info(
                "%s Aircraft Distance: %s km",
                PiAware.Helpers.ucfirst(mode),
                PiAware.Helpers.to_kilometers(aircraft_range, 2),
            )

            return aircraft_range

    @staticmethod
    def __process_interrupt(channel: int) -> None:
        if channel == 5:
            logging.info("Received Event on Pin %s - Clearing Display (Black).", channel)
            PiAware.__clear(clear=True, sleep=False, display_color="0x00")
        elif channel == 6:
            logging.info("Received Event on Pin %s - Clearing Display (White).", channel)
            PiAware.__clear(clear=True, sleep=False, display_color="0xFF")
        elif channel == 13:
            logging.info("Received Event on Pin %s - Executing Refresh.", channel)
            PiAware.refresh(cycle=0)
        elif channel == 19:
            logging.info("Received Event on Pin %s - Executing Shutdown.", channel)
            PiAware.__shutdown(signal_number=15)
        else:
            raise ValueError("Unexpected Pin: %s", channel)

    @staticmethod
    def __process_shutdown_signal(signal_number: int, frame) -> None:
        PiAware.__shutdown(signal_number)

    @staticmethod
    def __shutdown(signal_number: int) -> None:
        signal_name = signal.Signals(signal_number).name

        if signal_name == "SIGHUP":
            logging.info("Received %s, ignoring.", signal_name)
            return

        logging.info("Shutting down on Signal %s (%s).", signal_number, signal_name)

        PiAware.__clear(clear=True, sleep=True)
        GPIO.cleanup()
        GPIO.setmode(GPIO.BCM)

        logging.info("Shutdown on Signal %s complete.", signal_name)
        sys.exit(0)

    @staticmethod
    def __clear(
        clear: bool = False,
        sleep: bool = False,
        display_color: str = "0xFF"
    ) -> epaper.epaper:
        epd = epaper.epaper("epd2in7").EPD()
        epd.init()

        if clear:
            if display_color not in ["0x00", "0xFF"]:
                logging.warning("Got invalid Display Color: '%s' - Resetting to '0xFF'.", display_color)
                display_color = "0xFF"

            epd.Clear(display_color)

        if sleep:
            epd.sleep()

        return epd

    @staticmethod
    def __process_special_interest(aircraft: list) -> None:
        """
        Process Flights of Special Interest
        """

        if "flight" in aircraft and len(REGISTRATION_OF_SPECIAL_INTEREST) >= 1:
            flight = PiAware.Helpers.trim(aircraft["flight"])

            contains_registration = PiAware.Helpers.contains_any(
                flight, list(REGISTRATION_OF_SPECIAL_INTEREST.keys())
            )

            if contains_registration is True:
                sent = False

                if {"lat", "lon"}.issubset(aircraft):
                    distance = PiAware.Helpers.to_kilometers(
                        PiAware.Helpers.haversine_distance(
                            PiAware.get_receiver_position(),
                            Position(aircraft["lat"], aircraft["lon"]),
                        )
                    )
                else:
                    distance = "unknown"

                if "squawk" in aircraft:
                    squawk = PiAware.Helpers.trim(aircraft["squawk"])
                else:
                    squawk = "unknown"

                sent = PiAware.Helpers.send_slack_notification(
                    aircraft["hex"], flight, squawk, distance, "registration"
                )

                if sent is True:
                    logging.info(
                        "Found Registration of Special Interest (%s) in %s km distance. Remarks: %s",
                        flight,
                        distance,
                        REGISTRATION_OF_SPECIAL_INTEREST[flight],
                    )

        if "hex" in aircraft and len(ICAO_OF_SPECIAL_INTEREST) >= 1:
            hex = PiAware.Helpers.trim(aircraft["hex"]).upper()

            contains_icao = PiAware.Helpers.contains_any(
                hex, list(ICAO_OF_SPECIAL_INTEREST.keys())
            )

            if contains_icao is True:
                sent = False

                if {"lat", "lon"}.issubset(aircraft):
                    distance = PiAware.Helpers.to_kilometers(
                        PiAware.Helpers.haversine_distance(
                            PiAware.get_receiver_position(),
                            Position(aircraft["lat"], aircraft["lon"]),
                        )
                    )
                else:
                    distance = "unknown"

                if "flight" in aircraft:
                    flight = PiAware.Helpers.trim(aircraft["flight"])
                else:
                    flight = "unknown"

                if "squawk" in aircraft:
                    squawk = PiAware.Helpers.trim(aircraft["squawk"])
                else:
                    squawk = "unknown"

                sent = PiAware.Helpers.send_slack_notification(
                    hex, flight, squawk, distance, "icao"
                )

                if sent is True:
                    logging.info(
                        "Found Flight of Special Interest (hex:%s) in %s km distance. Remarks: %s",
                        hex.lower(),
                        distance,
                        ICAO_OF_SPECIAL_INTEREST[hex.upper()],
                    )

    @staticmethod
    def __has_emergency(current_status: str, mode: str = "slug") -> Union[int, str]:
        PiAware.Helpers.check_supported(mode, ["slug", "count"])

        emergency = 0
        aircrafts = PiAware.get_aircrafts(raw=True)
        if "aircraft" in aircrafts:
            origin = PiAware.get_receiver_position()

            for aircraft in aircrafts["aircraft"]:
                if "squawk" in aircraft:
                    contains = PiAware.Helpers.contains_any(
                        aircraft["squawk"], list(EMERGENCY_SQUAWK.keys())
                    )
                    if contains:
                        emergency += 1
                        if "flight" in aircraft:
                            flight = PiAware.Helpers.trim(aircraft["flight"])
                        else:
                            flight = "unknown"

                        if {"lat", "lon"}.issubset(aircraft):
                            aircraft_distance = PiAware.Helpers.to_kilometers(
                                PiAware.Helpers.haversine_distance(
                                    pos1=origin,
                                    pos2=Position(aircraft["lat"], aircraft["lon"]),
                                )
                            )
                        else:
                            aircraft_distance = "unknown"

                        notification_sent = PiAware.Helpers.send_slack_notification(
                            aircraft["hex"],
                            flight,
                            aircraft["squawk"],
                            aircraft_distance,
                            "emergency",
                        )

                        logging.warning(
                            "Aircraft %s (Callsign %s) with ICAO Emergency Squawk %s found in %s km distance (Notification sent %s)",
                            aircraft["hex"],
                            flight,
                            aircraft["squawk"],
                            aircraft_distance,
                            notification_sent,
                        )
        else:
            emergency = 666

        if mode == "slug":
            if emergency >= 1:
                new_status = f"!!! SQUAWK 7x00 (Count: {emergency}) !!!"
            else:
                new_status = current_status

            return new_status

        return emergency

    @staticmethod
    def get_status() -> list:
        """
        Get the PiAware Status json
        """
        r = PiAware.Helpers.download(f"{PIAWARE_HOST}/status.json")

        if r is not False and r:
            res = r.json()
        else:
            res = {}

        return res

    @staticmethod
    def get_receiver() -> list:
        """
        Get the PiAware Receiver json

        The json contains fields like Receiver Position
        """
        r = PiAware.Helpers.download(f"{PIAWARE_HOST}/skyaware/data/receiver.json")

        if r is not False:
            res = r.json()
        else:
            res = {}

        return res

    @staticmethod
    def get_receiver_position() -> Position:
        """
        Return the Receiver Position from receiver.json

        If Position was set during Setup the pre-received Position
        is returned as the receiver is unlikely to move during Runtime
        of the Script.
        """
        if isinstance(PiAware.receiver_position, Position):
            logging.debug("Found own %s", PiAware.receiver_position)

            return PiAware.receiver_position

        receiver = PiAware.get_receiver()
        if {"lat", "lon"}.issubset(receiver):
            origin = Position(receiver["lat"], receiver["lon"])
            logging.debug("Own %s", origin)
        else:
            origin = Position(0.000, 0.000)
            logging.error("Could not get own Location. Own %s", origin)

        return origin

    @staticmethod
    def get_aircrafts(
        pos: bool = True, mode: str = "adsb", threshold: int = 120, raw: bool = False
    ) -> Union[list, int]:
        """
        Return a List of Aircrafts currently seen by the Receiver

        The List is subject to filtering (mode, position, age) if needed
        """

        PiAware.Helpers.check_supported(mode, ["adsb", "mlat"])

        res = []
        r = PiAware.Helpers.download(f"{PIAWARE_HOST}/skyaware/data/aircraft.json")

        if r is not False:
            json = r.json()
        else:
            json = {}

        if "aircraft" in json:
            if raw is True:
                return json

            for aircraft in json["aircraft"]:
                if mode == "adsb":
                    if pos is True:
                        if "seen_pos" in aircraft and aircraft["seen_pos"] <= threshold:
                            res.append(aircraft)

                            PiAware.__process_special_interest(aircraft)
                    else:
                        if "seen" in aircraft and aircraft["seen"] <= threshold:
                            res.append(aircraft)
                elif mode == "mlat":
                    if "mlat" in aircraft and "lat" in aircraft["mlat"]:
                        res.append(aircraft)
                else:
                    logging.warning("Unknown Mode: %s", mode)

        ac_count = len(res)
        logging.info(
            "Found %s total Flights (Position: %s, Mode: %s, Threshold: %ss)",
            ac_count,
            pos,
            mode,
            threshold,
        )

        return ac_count

    @staticmethod
    def get_fr24_status() -> str:
        """
        Get Flightradar24 Status

        It's returned as a "side information", focus is on PiAware
        """
        r = PiAware.Helpers.download(f"{FLIGHTRADAR_HOST}/monitor.json", 2, 0.1)

        if r is not False:
            fr24 = r.json()
        else:
            fr24 = {}

        if "feed_status" in fr24 and fr24["feed_status"] == "connected":
            status = f'Connected via {fr24["feed_current_mode"]}'
        elif "feed_status" in fr24:
            status = PiAware.Helpers.ucfirst(fr24["feed_status"])
        else:
            status = "unknown"

        logging.info("Flightradar24 Status: %s", status)

        return status

    @staticmethod
    def refresh(cycle: int = 1) -> int:
        """
        Refresh the e-Paper Display
        """
        time_start = time.time()

        font_path = os.path.join(PATH_ROOT, "epaper.ttf")
        font_s = ImageFont.truetype(font_path, 11)
        font_m = ImageFont.truetype(font_path, 12)

        try:
            error = False
            status = PiAware.get_status()
            status_slug = "OK"

            if PiAware.Helpers.bool_from_env("ENABLE_FR24"):
                status_slug = f"{status_slug}, fr24: {PiAware.get_fr24_status()}"

            if "system_uptime" in status:
                uptime = str(timedelta(seconds=status["system_uptime"]))
            else:
                error = True
                uptime = "Failed API Call"

            if "time" in status:
                status_time = datetime.fromtimestamp(status["time"] / 1000).strftime(
                    "%d.%m.%Y, %H:%M:%S"
                )
            else:
                error = True
                status_time = "!!! Failed to get System Time"

            if "gps" in status:
                status_gps = status["gps"]["message"]
            else:
                error = True
                status_gps = "!!! Failed to get GPS Status"

            if "radio" in status:
                status_radio = status["radio"]["message"]
            else:
                error = True
                status_radio = "!!! Failed to get Radio Status"

            if "piaware" in status:
                status_piaware = status["piaware"]["message"]
            else:
                error = True
                status_piaware = "!!! Failed to get PiAware Status"

            if error is True:
                status_slug = "NEEDS ATTENTION"

            status_slug = PiAware.__has_emergency(current_status=status_slug)

            clear_display = bool(cycle == 1)

            epd = PiAware.__clear(clear=clear_display, sleep=False)

            logging.debug(
                "e-Paper Display is %sx%s (Height x Width)", epd.height, epd.width
            )

            ep_image = Image.new("1", (epd.height, epd.width), 255)
            draw = ImageDraw.Draw(ep_image)

            draw.line((0, 88, 264, 88), fill=0)
            draw.line((132, 88, 132, 176), fill=0)

            draw.text((4, 4), f"Status ({status_slug})", font=font_s, fill=0)
            draw.text((8, 20), status_time, font=font_m, fill=0)
            draw.text((8, 35), status_piaware, font=font_m, fill=0)
            draw.text((8, 50), status_gps, font=font_m, fill=0)
            draw.text((8, 65), status_radio, font=font_m, fill=0)

            origin = PiAware.get_receiver_position()

            ths = 120
            draw.text((4, 90), "Aircrafts", font=font_s, fill=0)
            draw.text(
                (8, 103),
                f"Count (all): {PiAware.get_aircrafts(pos=False, threshold=ths)}",
                font=font_m,
                fill=0,
            )
            draw.text(
                (8, 116),
                f"Count (w/ pos): {PiAware.get_aircrafts(pos=True, threshold=ths)}",
                font=font_m,
                fill=0,
            )
            draw.text(
                (8, 129),
                f'Count (MLAT): {PiAware.get_aircrafts(pos=True, mode="mlat", threshold=ths)}',
                font=font_m,
                fill=0,
            )
            draw.text(
                (8, 142),
                f'Min Range: {PiAware.Helpers.to_kilometers(PiAware.Helpers.distance(origin=origin, mode="min", threshold=ths), 1)} km',
                font=font_m,
                fill=0,
            )
            draw.text(
                (8, 155),
                f'Max Range: {PiAware.Helpers.to_kilometers(PiAware.Helpers.distance(origin=origin, mode="max", threshold=ths), 1)} km',
                font=font_m,
                fill=0,
            )

            if "cpu_temp_celcius" in status:
                status_temp = round(status["cpu_temp_celcius"], 2)
            else:
                status_temp = "-273.15"

            if "cpu_load_percent" in status:
                status_load = status["cpu_load_percent"]
            else:
                status_load = os.cpu_count() * 100

            local_ip = PiAware.Helpers.get_local_ip()

            draw.text((136, 90), "Receiver OS", font=font_s, fill=0)
            draw.text((140, 103), f"Up: {uptime}", font=font_m, fill=0)
            draw.text((140, 116), f"CPU Load: {status_load}%", font=font_m, fill=0)
            draw.text((140, 129), f"CPU Temp: {status_temp}°C", font=font_m, fill=0)

            draw.text((136, 147), f"Cycle: {cycle:,}", font=font_s, fill=0)
            draw.text((136, 160), f"Display IP: {local_ip}", font=font_s, fill=0)

            epd.display(epd.getbuffer(ep_image))
            epd.sleep()

            if not RUNNING_IN_DOCKER:
                ep_image.save(os.path.join(PATH_ROOT, "epaper.jpg"))

            new_cycle = cycle + 1

            logging.info(
                "Refresh cycle %s complete after %ss",
                cycle,
                round(time.time() - time_start, 3),
            )

            return new_cycle
        except IOError as e:
            logging.error(e)
            PiAware.__shutdown(signal_number=15)

    @staticmethod
    def process() -> None:
        """
        Start the Processing Loop
        """
        cycle = 1

        while True:
            logging.info("Starting Refresh Cycle %s", cycle)

            current_cycle = cycle
            cycle = PiAware.refresh(cycle=current_cycle)

            i = 0
            sleepy_display = 300
            sleepy_updates = round(sleepy_display / 10)
            logging.info(
                "Sleeping for %s seconds after refresh cycle %s (Ping every %ss)",
                sleepy_display,
                current_cycle,
                sleepy_updates,
            )
            while i < sleepy_display:
                time.sleep(1)
                i += 1
                if i % sleepy_updates == 0:
                    logging.info(
                        "Slept %s/%s seconds after refresh cycle %s",
                        i,
                        sleepy_display,
                        current_cycle,
                    )

    @staticmethod
    def setup() -> None:
        """
        Perform Setup Tasks
        """
        logging.basicConfig(
            level=LOGLEVEL,
            format="%(asctime)s %(levelname)-8s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        logging.info("Running on PID: %s", os.getpid())

        if SENTRY_DSN is not None and PiAware.Helpers.is_valid_url(SENTRY_DSN):
            sentry_sdk.init(dsn=SENTRY_DSN, traces_sample_rate=1.0)
            logging.info("Sentry SDK is Enabled")
        else:
            logging.info(
                "Skipping Sentry SDK Configuration. SENTRY_DSN is not set or invalid."
            )

        if PIAWARE_HOST is None:
            raise RuntimeError("Environment Variable PIAWARE_HOST is not set.")

        PiAware.receiver_position = PiAware.get_receiver_position()

        GPIO.setmode(GPIO.BCM)

        GPIO.setup(5, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(6, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(13, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(19, GPIO.IN, pull_up_down=GPIO.PUD_UP)

        GPIO.add_event_detect(
            5, GPIO.FALLING, callback=PiAware.__process_interrupt, bouncetime=200
        )
        GPIO.add_event_detect(
            6, GPIO.FALLING, callback=PiAware.__process_interrupt, bouncetime=200
        )
        GPIO.add_event_detect(
            13, GPIO.FALLING, callback=PiAware.__process_interrupt, bouncetime=200
        )
        GPIO.add_event_detect(
            19, GPIO.FALLING, callback=PiAware.__process_interrupt, bouncetime=200
        )

        signal.signal(signal.SIGINT, PiAware.__process_shutdown_signal)
        signal.signal(signal.SIGQUIT, PiAware.__process_shutdown_signal)
        signal.signal(signal.SIGTERM, PiAware.__process_shutdown_signal)


if __name__ == "__main__":
    PiAware.setup()
    PiAware.process()
