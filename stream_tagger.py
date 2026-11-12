#!/usr/bin/env python3
"""Record PipeWire monitor audio to FLAC and log MPRIS track metadata.

Debian 13 dependencies:
    sudo apt install python3-pyaudio python3-dbus python3-soundfile

Or:
    python3 -m pip install -r requirements.txt
"""

from __future__ import annotations

import argparse
import contextlib
import curses
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    import dbus
except ImportError:
    dbus = None

try:
    import pyaudio
except ImportError:
    pyaudio = None

try:
    import soundfile as sf
except ImportError:
    sf = None


MPRIS_PREFIX = "org.mpris.MediaPlayer2"
MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_ROOT = "org.mpris.MediaPlayer2"
MPRIS_PLAYER = "org.mpris.MediaPlayer2.Player"
DBUS_PROPERTIES = "org.freedesktop.DBus.Properties"
MAX_STREAM_INDEX = 255
STREAM_PATTERN = re.compile(r"^stream\.(\d{3})\.flac$")
MAX_RUN_SECONDS = 9 * 60


@contextlib.contextmanager
def _suppress_stderr():
    try:
        sys.stderr.flush()
    except Exception:
        pass
    try:
        saved_fd = os.dup(2)
    except OSError:
        yield
        return
    devnull_fd = None
    try:
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull_fd, 2)
        yield
    finally:
        if devnull_fd is not None:
            try:
                os.close(devnull_fd)
            except OSError:
                pass
        try:
            os.dup2(saved_fd, 2)
        except OSError:
            pass
        try:
            os.close(saved_fd)
        except OSError:
            pass
        try:
            sys.stderr.flush()
        except Exception:
            pass


def format_timecode(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    total_microseconds = int(round(max(0.0, float(seconds)) * 1_000_000))
    hours, remainder = divmod(total_microseconds, 3_600_000_000)
    minutes, remainder = divmod(remainder, 60_000_000)
    whole_seconds, microseconds = divmod(remainder, 1_000_000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{microseconds:06d}"


def sample_timecode(sample_position: int, sample_rate: int) -> str:
    return format_timecode(sample_position * (1.0 / sample_rate))


def next_stream_index(directory: Path) -> int:
    highest = -1
    if directory.is_dir():
        for entry in directory.iterdir():
            match = STREAM_PATTERN.match(entry.name)
            if match:
                value = int(match.group(1))
                if 0 <= value <= MAX_STREAM_INDEX:
                    highest = max(highest, value)
    return (highest + 1) % (MAX_STREAM_INDEX + 1)


def stream_paths(directory: Path, index: int) -> tuple[Path, Path]:
    name = f"{index:03d}"
    return directory / f"stream.{name}.flac", directory / f"stream.{name}.tags.log"


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value if str(item))
    return str(value)


def _as_microseconds(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value) / 1_000_000.0
    except (TypeError, ValueError):
        return None


def parse_mpris_metadata(raw: Any) -> dict[str, Any]:
    if not raw:
        return {
            "title": "",
            "artist": "",
            "album": "",
            "art_url": "",
            "length": None,
        }
    return {
        "title": _as_text(raw.get("xesam:title")),
        "artist": _as_text(raw.get("xesam:artist")),
        "album": _as_text(raw.get("xesam:album")),
        "art_url": _as_text(raw.get("mpris:artUrl")),
        "length": _as_microseconds(raw.get("mpris:length")),
    }


def friendly_bus_name(bus_name: str) -> str:
    prefix = MPRIS_PREFIX + "."
    if bus_name.startswith(prefix):
        return bus_name[len(prefix) :]
    return bus_name


@dataclass
class MediaPlayer:
    bus_name: str
    identity: str
    playback_status: str = ""

    @property
    def label(self) -> str:
        return self.identity or friendly_bus_name(self.bus_name)


@dataclass
class TrackMetadata:
    track_time: str
    length: str
    supposed_end: str
    title: str
    artist: str
    album: str
    art_url: str


@dataclass
class Timemark:
    sample_index: int
    timecode: str


class MprisClient:
    def __init__(self) -> None:
        if dbus is None:
            raise RuntimeError("dbus-python is required to use MPRIS")
        self.bus = dbus.SessionBus()
        self._properties: dict[str, Any] = {}

    def _properties_interface(self, bus_name: str) -> Any:
        interface = self._properties.get(bus_name)
        if interface is None:
            remote = self.bus.get_object(bus_name, MPRIS_PATH)
            interface = dbus.Interface(remote, DBUS_PROPERTIES)
            self._properties[bus_name] = interface
        return interface

    def _get(self, bus_name: str, interface: str, prop: str) -> Any:
        try:
            return self._properties_interface(bus_name).Get(interface, prop)
        except Exception:
            return None

    def list_players(self) -> list[MediaPlayer]:
        try:
            names = list(self.bus.list_names())
        except Exception:
            return []
        candidates = sorted(
            {
                str(name)
                for name in names
                if str(name) == MPRIS_PREFIX or str(name).startswith(MPRIS_PREFIX + ".")
            }
        )
        players = []
        for bus_name in candidates:
            identity = _as_text(self._get(bus_name, MPRIS_ROOT, "Identity"))
            status = _as_text(self._get(bus_name, MPRIS_PLAYER, "PlaybackStatus"))
            players.append(
                MediaPlayer(
                    bus_name=bus_name,
                    identity=identity or friendly_bus_name(bus_name),
                    playback_status=status,
                )
            )
        players.sort(key=lambda player: (player.identity.lower(), player.bus_name))
        return players

    def metadata(self, bus_name: str) -> dict[str, Any]:
        return parse_mpris_metadata(self._get(bus_name, MPRIS_PLAYER, "Metadata"))

    def position(self, bus_name: str) -> float | None:
        return _as_microseconds(self._get(bus_name, MPRIS_PLAYER, "Position"))

    def playback_status(self, bus_name: str) -> str:
        return _as_text(self._get(bus_name, MPRIS_PLAYER, "PlaybackStatus"))


class TrackLog:
    def __init__(self) -> None:
        self.entries: list[TrackMetadata] = []
        self._last_title: str | None = None

    def reset(self) -> None:
        self._last_title = None

    def observe(
        self,
        metadata: dict[str, Any],
        capture_elapsed: float,
    ) -> TrackMetadata | None:
        title = (metadata.get("title") or "").strip()
        if not title:
            return None
        if title == self._last_title:
            return None
        self._last_title = title

        track_start = max(0.0, capture_elapsed)

        length = metadata.get("length")
        supposed_end = None
        if length is not None:
            supposed_end = track_start + length

        entry = TrackMetadata(
            track_time=format_timecode(track_start),
            length=format_timecode(length),
            supposed_end=format_timecode(supposed_end),
            title=title,
            artist=metadata.get("artist") or "",
            album=metadata.get("album") or "",
            art_url=metadata.get("art_url") or "",
        )
        self.entries.append(entry)
        return entry


@dataclass
class AudioConfig:
    device_index: int
    device_name: str
    rate: int
    channels: int
    format: int
    sample_width: int
    bits: int
    dtype: str
    subtype: str
    frames_per_buffer: int = 1024


@dataclass
class AudioSource:
    index: int
    name: str
    description: str
    rate: int
    channels: int
    info: dict[str, Any]
    is_monitor: bool
    is_default: bool
    capture_index: int = 0
    pulse_name: str = ""
    spec: dict[str, Any] | None = None

    @property
    def label(self) -> str:
        return self.description or self.name


def _pactl_json_sources() -> list[dict[str, Any]]:
    try:
        result = subprocess.run(
            ["pactl", "--format=json", "list", "sources"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0 or not result.stdout:
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _default_sink_name() -> str | None:
    try:
        result = subprocess.run(
            ["pactl", "get-default-sink"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    return name or None


def _input_devices(pa: Any) -> tuple[int | None, list[tuple[int, dict[str, Any]]]]:
    default_index = None
    try:
        default_index = int(pa.get_default_input_device_info()["index"])
    except Exception:
        pass

    devices = []
    for index in range(pa.get_device_count()):
        try:
            info = pa.get_device_info_by_index(index)
        except Exception:
            continue
        if int(info.get("maxInputChannels", 0)) > 0:
            devices.append((index, info))
    return default_index, devices


def _default_source_name() -> str | None:
    try:
        result = subprocess.run(
            ["pactl", "get-default-source"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    return name or None


def _parse_sample_specification(spec: Any) -> dict[str, Any]:
    if isinstance(spec, dict):
        parsed = {}
        for key in ("format", "channels", "rate"):
            if key in spec:
                parsed[key] = spec[key]
        return parsed
    if isinstance(spec, str):
        match = re.match(
            r"^\s*(\S+)\s+(\d+)\s*ch\s+(\d+)\s*Hz\s*$", spec, re.IGNORECASE
        )
        if match:
            return {
                "format": match.group(1),
                "channels": int(match.group(2)),
                "rate": int(match.group(3)),
            }
    return {}


def _source_specification(source_name: str) -> dict[str, Any]:
    for source in _pactl_json_sources():
        if source.get("name") == source_name:
            return _parse_sample_specification(source.get("sample_specification"))
    return {}


def _pulse_capture_index(
    devices: list[tuple[int, dict[str, Any]]], default_index: int | None
) -> int:
    for index, info in devices:
        if str(info.get("name", "")).lower() in {"pulse", "default"}:
            return index
    if default_index is not None:
        return default_index
    return devices[0][0]


def list_capture_sources(pa: Any) -> list[AudioSource]:
    default_index, devices = _input_devices(pa)
    if not devices:
        return []

    fallback_info = devices[0][1]
    try:
        fallback_rate = int(round(float(fallback_info.get("defaultSampleRate", 48000) or 48000)))
    except (TypeError, ValueError):
        fallback_rate = 48000
    fallback_channels = max(1, int(fallback_info.get("maxInputChannels", 2) or 2))
    capture_index = _pulse_capture_index(devices, default_index)

    sink_name = _default_sink_name()
    default_monitor = f"{sink_name}.monitor" if sink_name else None
    raw_sources = _pactl_json_sources()
    if raw_sources:
        default_source = _default_source_name()

        sources = []
        for raw in raw_sources:
            name = str(raw.get("name", ""))
            if not name:
                continue
            spec = _parse_sample_specification(raw.get("sample_specification"))
            try:
                rate = int(spec.get("rate") or 0)
            except (TypeError, ValueError):
                rate = 0
            try:
                channels = int(spec.get("channels") or 0)
            except (TypeError, ValueError):
                channels = 0
            if rate <= 0:
                rate = fallback_rate
            if channels <= 0:
                channels = fallback_channels

            properties = raw.get("properties") or {}
            is_monitor = (
                bool(raw.get("monitor_source"))
                or ".monitor" in name
                or str(properties.get("device.class", "")) == "monitor"
            )
            info = {
                "name": name,
                "description": str(raw.get("description", "") or ""),
                "defaultSampleRate": float(rate),
                "maxInputChannels": channels,
            }
            sources.append(
                AudioSource(
                    index=int(raw.get("index", 0) or 0),
                    name=name,
                    description=str(raw.get("description", "") or ""),
                    rate=rate,
                    channels=channels,
                    info=info,
                    is_monitor=is_monitor,
                    is_default=name == default_monitor
                    or (default_monitor is None and name == default_source),
                    capture_index=capture_index,
                    pulse_name=name,
                    spec=spec,
                )
            )

        monitors = [source for source in sources if source.is_monitor]
        result = monitors or sources
        if result and not any(source.is_default for source in result):
            for source in result:
                if source.name == default_source:
                    source.is_default = True
                    break
        return result

    sources = []
    for index, info in devices:
        name = str(info.get("name", ""))
        try:
            rate = int(round(float(info.get("defaultSampleRate", 0) or 0)))
        except (TypeError, ValueError):
            rate = 0
        spec = _source_specification(name)
        if not spec:
            rate = rate or fallback_rate
        sources.append(
            AudioSource(
                index=index,
                name=name,
                description=str(info.get("description", "") or ""),
                rate=rate,
                channels=max(1, int(info.get("maxInputChannels", 1) or 1)),
                info=info,
                is_monitor=".monitor" in name,
                is_default=(name == default_monitor if sink_name else False)
                or (default_index is not None and index == default_index),
                capture_index=index,
                spec=spec,
            )
        )

    monitors = [source for source in sources if source.is_monitor]
    return monitors or sources


def enumerate_sources() -> list[AudioSource]:
    if pyaudio is None:
        raise RuntimeError("PyAudio is required to enumerate audio sources")
    with _suppress_stderr():
        pa = pyaudio.PyAudio()
    try:
        return list_capture_sources(pa)
    finally:
        pa.terminate()


def _select_source(pa: Any) -> AudioSource:
    sources = list_capture_sources(pa)
    if not sources:
        raise RuntimeError("No audio input devices were found")
    return next((source for source in sources if source.is_default), sources[0])


def _format_from_name(name: str) -> tuple[int, int, int, str, str] | None:
    name = (name or "").lower()
    if name in {"s16le", "s16be"}:
        return pyaudio.paInt16, 2, 16, "int16", "PCM_16"
    if name in {"s24le", "s24be", "s24_3le", "s24_3be", "s24_32le", "s24_32be"}:
        return pyaudio.paInt32, 4, 24, "int32", "PCM_24"
    if name in {"s32le", "s32be"}:
        return pyaudio.paInt32, 4, 32, "int32", "PCM_24"
    if name in {"float32le", "float32be", "float64le", "float64be"}:
        return pyaudio.paFloat32, 4, 32, "float32", "PCM_24"
    if name in {"s8"}:
        return pyaudio.paInt8, 1, 8, "int8", "PCM_S8"
    return None


def detect_audio_config(
    pa: Any, source: AudioSource | None = None
) -> AudioConfig:
    if source is None:
        source = _select_source(pa)
    device_index = source.capture_index
    device = source.info
    device_name = source.name or str(device.get("name", "default"))
    spec = source.spec or _source_specification(source.pulse_name or device_name)

    try:
        default_rate = int(
            round(float(device.get("defaultSampleRate", source.rate or 48000) or 48000))
        )
    except (TypeError, ValueError):
        default_rate = 48000
    try:
        rate = int(spec.get("rate") or source.rate or default_rate)
    except (TypeError, ValueError):
        rate = default_rate
    if rate <= 0:
        rate = default_rate or 48000

    try:
        max_channels = max(1, int(device.get("maxInputChannels", 1) or 1))
    except (TypeError, ValueError):
        max_channels = 1
    try:
        requested_channels = int(
            spec.get("channels") or source.channels or min(2, max_channels)
        )
    except (TypeError, ValueError):
        requested_channels = min(2, max_channels)
    channels = max(1, min(requested_channels, max_channels))

    preferred = _format_from_name(str(spec.get("format", "")))
    fallbacks = [
        (pyaudio.paInt32, 4, 24, "int32", "PCM_24"),
        (pyaudio.paInt16, 2, 16, "int16", "PCM_16"),
    ]
    candidates = []
    if preferred is not None:
        candidates.append(preferred)
    for candidate in fallbacks:
        if candidate not in candidates:
            candidates.append(candidate)

    chosen = None
    for candidate in candidates:
        sample_format = candidate[0]
        try:
            supported = pa.is_format_supported(
                rate,
                input_device=device_index,
                input_channels=channels,
                input_format=sample_format,
            )
        except Exception:
            supported = False
        if supported:
            chosen = candidate
            break

    if chosen is None:
        chosen = preferred if preferred is not None else fallbacks[1]

    sample_format, sample_width, bits, dtype, subtype = chosen
    return AudioConfig(
        device_index=device_index,
        device_name=device_name,
        rate=rate,
        channels=channels,
        format=sample_format,
        sample_width=sample_width,
        bits=bits,
        dtype=dtype,
        subtype=subtype,
    )


class AudioEngine:
    def __init__(self, output_path: Path, source: AudioSource | None = None) -> None:
        self.output_path = Path(output_path)
        self.source = source
        self.config: AudioConfig | None = None
        self._pa: Any = None
        self._stream: Any = None
        self._lock = threading.Lock()
        self._memory: list[bytes] = []
        self._frames = 0
        self._buffered_bytes = 0
        self._timemarks: list[Timemark] = []
        self._error: BaseException | None = None
        self.started = False
        self._flac_written = False

    def start(self) -> None:
        if pyaudio is None:
            raise RuntimeError("PyAudio is required to capture audio")
        if sf is None:
            raise RuntimeError("soundfile is required to write FLAC files")
        if self.started:
            return

        if self.source is not None and self.source.pulse_name:
            os.environ["PULSE_SOURCE"] = self.source.pulse_name

        with _suppress_stderr():
            self._pa = pyaudio.PyAudio()
            try:
                self.config = detect_audio_config(self._pa, self.source)
                self._stream = self._pa.open(
                    format=self.config.format,
                    channels=self.config.channels,
                    rate=self.config.rate,
                    input=True,
                    input_device_index=self.config.device_index,
                    frames_per_buffer=self.config.frames_per_buffer,
                    stream_callback=self._callback,
                )
                self._stream.start_stream()
                self.started = True
            except Exception:
                self._cleanup()
                raise

    def _callback(
        self,
        in_data: bytes,
        frame_count: int,
        time_info: Any,
        status_flags: int,
    ) -> tuple[None, int]:
        if self._error is not None:
            return None, pyaudio.paAbort
        with self._lock:
            self._memory.append(bytes(in_data))
            self._buffered_bytes += len(in_data)
            self._frames += frame_count
        return None, pyaudio.paContinue

    def drop_timemark(self) -> Timemark:
        if self.config is None:
            raise RuntimeError("Audio engine is not started")
        with self._lock:
            sample_index = self._frames
            timemark = Timemark(
                sample_index=sample_index,
                timecode=sample_timecode(sample_index, self.config.rate),
            )
            self._timemarks.append(timemark)
        return timemark

    def stop(self) -> None:
        self._cleanup()
        if self._flac_written:
            return
        self._flac_written = True
        self._write_flac()

    def _write_flac(self) -> None:
        if self.config is None:
            return
        with self._lock:
            data = b"".join(self._memory)
            self._memory = []
        writer = sf.SoundFile(
            str(self.output_path),
            mode="w",
            samplerate=self.config.rate,
            channels=self.config.channels,
            format="FLAC",
            subtype=self.config.subtype,
        )
        try:
            if data:
                writer.buffer_write(data, dtype=self.config.dtype)
            else:
                writer.buffer_write(
                    b"\x00" * self.config.sample_width * self.config.channels,
                    dtype=self.config.dtype,
                )
        finally:
            writer.close()

    def _cleanup(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass

        pa, self._pa = self._pa, None
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass

    @property
    def elapsed(self) -> float:
        if self.config is None:
            return 0.0
        with self._lock:
            return self._frames / self.config.rate

    @property
    def frames(self) -> int:
        with self._lock:
            return self._frames

    @property
    def buffered_bytes(self) -> int:
        with self._lock:
            return self._buffered_bytes

    @property
    def timemarks(self) -> list[Timemark]:
        with self._lock:
            return list(self._timemarks)


def probe_audio_config(source: AudioSource | None) -> AudioConfig | None:
    if pyaudio is None:
        return None
    with _suppress_stderr():
        pa = pyaudio.PyAudio()
    try:
        with _suppress_stderr():
            return detect_audio_config(pa, source)
    except Exception:
        return None
    finally:
        try:
            pa.terminate()
        except Exception:
            pass


def write_empty_flac(path: Path, config: AudioConfig | None) -> None:
    if sf is None:
        return
    rate = config.rate if config is not None else 48000
    channels = config.channels if config is not None else 1
    subtype = config.subtype if config is not None else "PCM_16"
    sample_width = config.sample_width if config is not None else 2
    writer = sf.SoundFile(
        str(path),
        mode="w",
        samplerate=rate,
        channels=channels,
        format="FLAC",
        subtype=subtype,
    )
    try:
        dtype = config.dtype if config is not None else "int16"
        writer.buffer_write(b"\x00" * sample_width * channels, dtype=dtype)
    finally:
        writer.close()


def write_report(
    path: Path,
    stream_name: str,
    config: AudioConfig | None,
    tracks: Iterable[TrackMetadata],
    timemarks: Iterable[Timemark],
) -> None:
    lines = [f"Stream: {stream_name}"]
    if config is not None:
        lines.append(f"Sample rate: {config.rate} Hz")
        lines.append(f"Bit resolution: {config.bits} bits")
    else:
        lines.append("Sample rate: unknown")
        lines.append("Bit resolution: unknown")
    lines.append("")
    lines.append("Metadata:")

    tracks = list(tracks)
    if not tracks:
        lines.append("(none)")
    for track in tracks:
        lines.append(f"Track time: {track.track_time}")
        lines.append(f"Length: {track.length}")
        lines.append(f"Supposed end time: {track.supposed_end}")
        lines.append(f"Title: {track.title}")
        lines.append(f"Artist: {track.artist}")
        lines.append(f"Album: {track.album}")
        lines.append(f"Album art URL: {track.art_url}")
        lines.append("")

    lines.append("Timemarks:")
    timemarks = list(timemarks)
    if not timemarks:
        lines.append("(none)")
    for timemark in timemarks:
        lines.append(timemark.timecode)

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _safe_addstr(window: Any, row: int, column: int, text: str, attr: int = 0) -> None:
    height, width = window.getmaxyx()
    if row < 0 or row >= height or column < 0 or column >= width:
        return
    try:
        window.addnstr(row, column, text, max(0, width - column - 1), attr)
    except curses.error:
        pass


def player_menu(stdscr: Any, players: list[MediaPlayer]) -> MediaPlayer | None:
    curses.curs_set(0)
    stdscr.keypad(True)
    selected = 0

    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        _safe_addstr(stdscr, 0, 0, "Stream Tagger - select a media player", curses.A_BOLD)
        _safe_addstr(stdscr, 1, 0, "Up/Down or j/k to move, Enter to select, Q to quit")
        row = 3
        for index, player in enumerate(players):
            marker = ">" if index == selected else " "
            status = f" [{player.playback_status}]" if player.playback_status else ""
            line = f"{marker} {player.label}{status}  ({player.bus_name})"
            attr = curses.A_REVERSE if index == selected else curses.A_NORMAL
            _safe_addstr(stdscr, row, 0, line, attr)
            row += 1
            if row >= height - 1:
                break
        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k"), ord("K")):
            selected = (selected - 1) % len(players)
        elif key in (curses.KEY_DOWN, ord("j"), ord("J")):
            selected = (selected + 1) % len(players)
        elif key in (curses.KEY_ENTER, 10, 13):
            return players[selected]
        elif key in (ord("q"), ord("Q")):
            return None


def source_menu(stdscr: Any, sources: list[AudioSource]) -> AudioSource | None:
    curses.curs_set(0)
    stdscr.keypad(True)
    selected = next(
        (index for index, source in enumerate(sources) if source.is_default), 0
    )
    offset = 0

    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        _safe_addstr(
            stdscr, 0, 0, "Stream Tagger - select an audio monitor", curses.A_BOLD
        )
        _safe_addstr(stdscr, 1, 0, "Up/Down or j/k to move, Enter to select, Q to quit")

        visible = max(1, height - 4)
        if selected < offset:
            offset = selected
        elif selected >= offset + visible:
            offset = selected - visible + 1

        row = 3
        for index in range(offset, min(len(sources), offset + visible)):
            source = sources[index]
            marker = ">" if index == selected else " "
            tags = []
            if source.is_monitor:
                tags.append("monitor")
            if source.is_default:
                tags.append("default")
            suffix = f" [{', '.join(tags)}]" if tags else ""
            details = []
            if source.rate:
                details.append(f"{source.rate} Hz")
            if source.channels:
                details.append(f"{source.channels} ch")
            detail_text = f" ({', '.join(details)})" if details else ""
            line = f"{marker} {source.label}{suffix}{detail_text}  ({source.name})"
            attr = curses.A_REVERSE if index == selected else curses.A_NORMAL
            _safe_addstr(stdscr, row, 0, line, attr)
            row += 1
        stdscr.refresh()

        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k"), ord("K")):
            selected = (selected - 1) % len(sources)
        elif key in (curses.KEY_DOWN, ord("j"), ord("J")):
            selected = (selected + 1) % len(sources)
        elif key in (curses.KEY_ENTER, 10, 13):
            return sources[selected]
        elif key in (ord("q"), ord("Q")):
            return None


def monitor_ui(
    stdscr: Any,
    mpris: MprisClient,
    player: MediaPlayer,
    engine: AudioEngine,
    track_log: TrackLog,
    stream_name: str,
) -> None:
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.timeout(50)
    deadline = time.monotonic() + MAX_RUN_SECONDS
    last_poll = 0.0
    message = "Waiting for playback..."
    last_track: TrackMetadata | None = None
    last_status = ""
    last_position: float | None = None

    while True:
        now = time.monotonic()
        if now >= deadline:
            message = "9 minute limit reached; quitting"
            break
        if now - last_poll >= 0.25:
            last_poll = now
            try:
                metadata = mpris.metadata(player.bus_name)
                position = mpris.position(player.bus_name)
                status = mpris.playback_status(player.bus_name)
                previous_status = last_status
                last_status = status
                last_position = position
                playing = status.lower() == "playing"
                if playing and not engine.started:
                    try:
                        engine.start()
                        track_log.reset()
                        message = "Playback detected; capture started"
                    except Exception as exc:
                        message = f"Capture error: {exc}"
                elif (
                    engine.started
                    and previous_status.lower() == "playing"
                    and status.lower() == "paused"
                ):
                    return
                if engine.started:
                    entry = track_log.observe(metadata, engine.elapsed)
                    if entry is not None:
                        last_track = entry
                        message = f"Logged: {entry.title} at {entry.track_time}"
            except Exception as exc:
                message = f"Metadata error: {exc}"

        stdscr.erase()
        _safe_addstr(stdscr, 0, 0, "Stream Tagger", curses.A_BOLD)
        _safe_addstr(stdscr, 1, 0, f"Player: {player.label} ({player.bus_name})")
        _safe_addstr(stdscr, 2, 0, f"Output: {stream_name}")
        if engine.config is not None:
            config = engine.config
            _safe_addstr(
                stdscr,
                3,
                0,
                f"Capture: {config.device_name} - {config.rate} Hz, "
                f"{config.bits} bit, {config.channels} channel(s)",
            )
            _safe_addstr(
                stdscr,
                4,
                0,
                f"Elapsed: {format_timecode(engine.elapsed)}  "
                f"Samples: {engine.frames}  Buffer: {engine.buffered_bytes} bytes",
            )
        else:
            _safe_addstr(stdscr, 3, 0, "Capture: not started (waiting for Playing)")
        _safe_addstr(
            stdscr,
            5,
            0,
            f"Playback: {last_status or 'unknown'}  Position: {format_timecode(last_position)}",
        )
        if last_track is not None:
            _safe_addstr(stdscr, 6, 0, f"Track: {last_track.title} - {last_track.artist}")
            _safe_addstr(stdscr, 7, 0, f"Album: {last_track.album}")
            _safe_addstr(stdscr, 8, 0, f"Length: {last_track.length}")
        _safe_addstr(stdscr, 10, 0, f"Metadata entries: {len(track_log.entries)}")
        _safe_addstr(stdscr, 11, 0, f"Timemarks: {len(engine.timemarks)}")
        _safe_addstr(stdscr, 13, 0, message)
        _safe_addstr(
            stdscr,
            15,
            0,
            f"[Q] Quit   [T] Drop timemark   Max run: {MAX_RUN_SECONDS // 60} minutes",
        )
        stdscr.refresh()

        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            return
        if key in (ord("t"), ord("T")):
            if not engine.started:
                message = "Capture not started yet"
                continue
            try:
                timemark = engine.drop_timemark()
                message = f"Timemark T at {timemark.timecode}"
            except Exception as exc:
                message = f"Timemark error: {exc}"


def require_module_names(*names: str) -> None:
    missing = [name for name in names if globals().get(name) is None]
    if missing:
        packages = ", ".join(missing)
        raise RuntimeError(
            f"Missing Python module(s): {packages}. "
            "Install with: sudo apt install python3-pyaudio python3-dbus python3-soundfile"
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Record a selected PipeWire monitor to FLAC and log MPRIS metadata."
        )
    )
    parser.add_argument(
        "-d",
        "--directory",
        default=os.getcwd(),
        help="directory for stream.NNN.flac and stream.NNN.tags.log (default: current directory)",
    )
    parser.add_argument(
        "-p",
        "--player",
        help="MPRIS bus name to use directly instead of showing the menu",
    )
    parser.add_argument(
        "--list-players",
        action="store_true",
        help="list active MPRIS players and exit",
    )
    parser.add_argument(
        "-s",
        "--source",
        help="audio monitor name or index to use directly instead of showing the menu",
    )
    parser.add_argument(
        "--list-sources",
        action="store_true",
        help="list capturable audio monitors and exit",
    )
    return parser.parse_args(argv)


def choose_player(
    players: list[MediaPlayer], requested: str | None
) -> MediaPlayer | None:
    if requested:
        for player in players:
            if player.bus_name == requested:
                return player
        for player in players:
            if requested in player.bus_name or requested.lower() == player.identity.lower():
                return player
        raise RuntimeError(f"MPRIS player not found: {requested}")

    return curses.wrapper(player_menu, players)


def choose_source(
    sources: list[AudioSource], requested: str | None
) -> AudioSource | None:
    if requested:
        text = requested.strip()
        for source in sources:
            if text == str(source.index) or text == source.name:
                return source
        for source in sources:
            if text.lower() in source.name.lower() or text.lower() == source.label.lower():
                return source
        raise RuntimeError(f"Audio source not found: {requested}")

    return curses.wrapper(source_menu, sources)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.list_sources:
        try:
            require_module_names("pyaudio")
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        try:
            sources = enumerate_sources()
        except Exception as exc:
            print(f"Unable to enumerate audio sources: {exc}", file=sys.stderr)
            return 1
        for source in sources:
            tags = []
            if source.is_monitor:
                tags.append("monitor")
            if source.is_default:
                tags.append("default")
            suffix = f"\t[{', '.join(tags)}]" if tags else ""
            print(f"{source.index}\t{source.name}\t{source.label}{suffix}")
        return 0

    try:
        require_module_names("dbus")
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        mpris = MprisClient()
        players = mpris.list_players()
    except Exception as exc:
        print(f"Unable to connect to the DBus session bus: {exc}", file=sys.stderr)
        return 1

    if args.list_players:
        for player in players:
            status = f" [{player.playback_status}]" if player.playback_status else ""
            print(f"{player.bus_name}\t{player.identity}{status}")
        return 0

    if not players:
        print("No active MPRIS media players were found.", file=sys.stderr)
        return 1

    try:
        player = choose_player(players, args.player)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if player is None:
        return 0

    try:
        require_module_names("pyaudio", "sf")
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        sources = enumerate_sources()
    except Exception as exc:
        print(f"Unable to enumerate audio sources: {exc}", file=sys.stderr)
        return 1

    if not sources:
        print("No capturable audio monitors were found.", file=sys.stderr)
        return 1

    try:
        source = choose_source(sources, args.source)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if source is None:
        return 0

    directory = Path(args.directory).expanduser().resolve()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        index = next_stream_index(directory)
    except OSError as exc:
        print(f"Unable to use output directory {directory}: {exc}", file=sys.stderr)
        return 1
    stream_path, report_path = stream_paths(directory, index)
    stream_name = stream_path.name

    engine = AudioEngine(stream_path, source)
    track_log = TrackLog()

    report_ok = True
    try:
        curses.wrapper(monitor_ui, mpris, player, engine, track_log, stream_name)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"Terminal UI error: {exc}", file=sys.stderr)
    finally:
        if engine.started:
            engine.stop()
        else:
            probed = probe_audio_config(source)
            try:
                write_empty_flac(stream_path, probed)
            except Exception as exc:
                print(f"Unable to write {stream_path}: {exc}", file=sys.stderr)
            engine.config = probed
        try:
            write_report(
                report_path,
                stream_name,
                engine.config,
                track_log.entries,
                engine.timemarks,
            )
        except OSError as exc:
            report_ok = False
            print(f"Unable to write {report_path}: {exc}", file=sys.stderr)

    print(f"Wrote {stream_path}")
    if report_ok:
        print(f"Wrote {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
