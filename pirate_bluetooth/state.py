"""Plain-Python model of what the phone is playing (from Bluetooth AVRCP via BlueZ).

Nothing in here touches hardware, D-Bus or PIL, so it can be tested anywhere.
"""

import time
from dataclasses import dataclass
from typing import Optional

_STATUS = {"playing": "Playing", "forward-seek": "Playing", "reverse-seek": "Playing",
           "paused": "Paused", "stopped": "Stopped"}


@dataclass
class Progress:
    """Where we are in the track, anchored to a monotonic clock so it can tick between updates."""

    elapsed: float
    duration: float
    anchor: float  # time.monotonic() when `elapsed` was true

    def now(self, playing: bool, at: Optional[float] = None) -> float:
        at = time.monotonic() if at is None else at
        value = self.elapsed + ((at - self.anchor) if playing else 0.0)
        return max(0.0, min(value, self.duration))


@dataclass
class NowPlaying:
    title: str = ""
    artist: str = ""
    album: str = ""
    length_s: Optional[float] = None
    player_state: str = "Not Available"  # "Playing" | "Paused" | "Stopped" | "Not Available"
    progress: Optional[Progress] = None

    @property
    def playing(self) -> bool:
        return self.player_state == "Playing"

    @property
    def has_track(self) -> bool:
        return bool(self.title or self.artist or self.album)

    def apply_track(self, track: dict) -> bool:
        """BlueZ MediaPlayer1.Track: Title, Artist, Album, Duration (ms). True if the song changed."""
        track = track or {}
        title = str(track.get("Title") or "")
        artist = str(track.get("Artist") or "")
        album = str(track.get("Album") or "")
        duration = track.get("Duration")
        changed = (title, artist, album) != (self.title, self.artist, self.album)
        self.title, self.artist, self.album = title, artist, album
        # 0 or a huge value means "unknown" (radio streams, some apps)
        self.length_s = (int(duration) / 1000) if duration and int(duration) < 0xFFFFFFFF else None
        if changed or not self.length_s:
            self.progress = None
        elif self.progress is not None:
            self.progress = Progress(self.progress.elapsed, self.length_s, self.progress.anchor)
        return changed

    def apply_position(self, position_ms, at: Optional[float] = None) -> None:
        at = time.monotonic() if at is None else at
        if position_ms is None or not self.length_s:
            self.progress = None
            return
        self.progress = Progress(elapsed=min(int(position_ms) / 1000, self.length_s),
                                 duration=self.length_s, anchor=at)

    def apply_status(self, status: str, at: Optional[float] = None) -> None:
        at = time.monotonic() if at is None else at
        state = _STATUS.get((status or "").lower(), "Not Available")
        if self.progress is not None and state != self.player_state:
            # Freeze or restart the clock at the moment playback stops or starts.
            self.progress = Progress(self.progress.now(self.playing, at), self.progress.duration, at)
        self.player_state = state

    def clear(self) -> None:
        self.__init__()


def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"
