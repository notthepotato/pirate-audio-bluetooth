"""The on-screen music menu: a stack of lists, moved through with X (up), Y (down), A (open) and B (back).

Plain Python (no hardware, D-Bus or PIL), so it can be tested anywhere. Loading a list's contents from
Mopidy happens elsewhere; a page just says what to load and holds the result.
"""

from dataclasses import dataclass, field
from typing import List, Optional

ROWS = 6  # rows that fit on the 240x240 screen


@dataclass
class Item:
    label: str
    kind: str  # "dir" (opens a list), "track" (plays), "queued" (a song in the queue), "action", "note"
    uri: str = ""
    action: str = ""  # for kind == "action": what selecting it does
    value: str = ""  # shown on the right, e.g. "Off" next to Repeat
    tlid: Optional[int] = None
    current: bool = False  # the song that's playing now
    load: tuple = ()  # for kind == "dir": what to load when it's opened
    icon: str = ""  # a small picture shown before the label (playlists), or ""


@dataclass
class Page:
    title: str
    load: tuple = ()  # ("browse", uri) | ("playlists",) | ("playlist", uri) | ("queue",) | () for fixed items
    items: List[Item] = field(default_factory=list)
    cursor: int = 0
    offset: int = 0
    loading: bool = False
    error: str = ""
    icon: str = ""  # shown next to the title (a playlist's icon)

    def set_items(self, items, cursor=0):
        self.items = items
        self.loading = False
        self.cursor = max(0, min(cursor, len(items) - 1)) if items else 0
        self._scroll()

    def move(self, delta):
        if not self.items:
            return False
        new = max(0, min(len(self.items) - 1, self.cursor + delta))
        if new == self.cursor:
            return False
        self.cursor = new
        self._scroll()
        return True

    def _scroll(self):
        if self.cursor < self.offset:
            self.offset = self.cursor
        elif self.cursor >= self.offset + ROWS:
            self.offset = self.cursor - ROWS + 1
        self.offset = max(0, min(self.offset, max(0, len(self.items) - ROWS)))

    def selected(self) -> Optional[Item]:
        return self.items[self.cursor] if self.items else None

    def visible(self):
        return self.items[self.offset:self.offset + ROWS]


class Menu:
    def __init__(self):
        self.stack: List[Page] = []

    @property
    def open(self) -> bool:
        return bool(self.stack)

    @property
    def page(self) -> Optional[Page]:
        return self.stack[-1] if self.stack else None

    def show(self, root: Page):
        self.stack = [root]

    def push(self, page: Page):
        self.stack.append(page)

    def back(self) -> bool:
        """Go up a level. Returns False when that closes the menu."""
        if self.stack:
            self.stack.pop()
        return bool(self.stack)

    def close(self):
        self.stack = []

    def is_showing(self, page: Page) -> bool:
        return any(p is page for p in self.stack)


def scroll_step(repeats: int) -> int:
    """Holding X/Y scrolls one row at a time at first, then a page at a time."""
    return 1 if repeats < 8 else ROWS
