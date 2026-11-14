"""What one screen frame should show. Kept free of PIL so the logic can be tested anywhere."""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class View:
    # "idle" | "connected" | "playing" | "info" | "pairing" | "confirm" | "handback" | "menu" | "pi_idle"
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
    source: str = ""  # "bluetooth" | "pi" when the add-on runs the screen for both, else ""
    badge: str = ""  # small label top-left on the now-playing screen: the phone's name, or "Pi music"
    art: str = ""  # album art picture file, or "" for a colored card
    repeat: str = ""  # Pi music: "off" | "all" | "one"
    shuffle: bool = False
    # "menu": the rows on screen ({"label", "kind", "value", "current"}), which one is highlighted,
    # where they are in the whole list, and a note ("Loading…", or why the list is empty)
    menu: List[dict] = field(default_factory=list)
    menu_cursor: int = 0
    menu_offset: int = 0
    menu_total: int = 0
    menu_note: str = ""
    menu_icon: str = ""  # picture next to the menu's title (a playlist's icon)
