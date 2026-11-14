"""What one screen frame should show. Kept free of PIL so the logic can be tested anywhere."""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class View:
    # "idle" | "connected" | "playing" | "info" | "pairing" | "confirm" | "handback"
    kind: str = "idle"
    title: str = ""
    artist: str = ""
    album: str = ""
    playing: bool = True
    progress: Optional[float] = None  # 0..1, or None when unknown
    elapsed_text: str = ""
    remaining_text: str = ""
    name: str = "Pirate Audio"  # what phones see
    phone: str = ""  # connected (or pairing) phone's name
    last_phone: str = ""  # most recently paired phone, for the idle screen
    code: str = ""  # 6-digit pairing code to compare with the phone
    countdown: str = ""  # time left in pairing mode, e.g. "2:41"
    overlay: Optional[Tuple] = None  # ("volume", pct) | ("toast", text)
    info: List[Tuple[str, str]] = field(default_factory=list)
    connecting: bool = False  # looking for the paired phone (after power-on, or after pressing A)
    choice: bool = False  # just switched on: A plays the phone, X switches to the Pi's own music
