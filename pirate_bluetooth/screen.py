"""Draws 240x240 frames for the Pirate Audio LCD with PIL.

Works with the Pillow in Raspberry Pi OS (9.4 on Bookworm, newer on Trixie).
Button positions on Pirate Audio (rotation 90): A top-left, B bottom-left, X top-right, Y bottom-right.
"""

import colorsys
import hashlib
import logging
import os
from typing import List, Tuple

from PIL import Image, ImageDraw, ImageFont

from .view import View

log = logging.getLogger(__name__)

SIZE = 240
BG = (11, 13, 18)
TEXT = (255, 255, 255)
SUBTLE = (190, 195, 206)
FAINT = (120, 126, 140)
KEY = (255, 210, 120)  # button legend colour
BLUE = (110, 165, 255)
GREEN = (110, 230, 170)

try:
    BILINEAR = Image.Resampling.BILINEAR
except AttributeError:  # Pillow < 9.1
    BILINEAR = Image.BILINEAR
ROW_SEL = (44, 74, 128)  # highlighted menu row
MENU_ROWS = 6

_FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu"]
_CJK_FONTS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]


def _find_font(bold: bool):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for d in _FONT_DIRS:
        path = os.path.join(d, name)
        if os.path.exists(path):
            return path
    return None


class Fonts:
    def __init__(self):
        self._cache = {}
        self._regular = _find_font(False)
        self._bold = _find_font(True)
        self._cjk = next((p for p in _CJK_FONTS if os.path.exists(p)), None)
        if not self._regular:
            log.warning("DejaVu fonts not found; using PIL's built-in font (install fonts-dejavu-core)")

    def get(self, size: int, bold: bool = False, text: str = ""):
        wants_cjk = self._cjk and any(ord(c) >= 0x2E80 for c in text)
        path = self._cjk if wants_cjk else (self._bold if bold else self._regular)
        key = (path, size)
        if key not in self._cache:
            if path:
                self._cache[key] = ImageFont.truetype(path, size)
            else:
                try:
                    self._cache[key] = ImageFont.load_default(size=size)
                except TypeError:  # Pillow < 10.1 has no sized default font
                    self._cache[key] = ImageFont.load_default()
        return self._cache[key]


def _text_width(draw, text, font) -> float:
    try:
        return draw.textlength(text, font=font)
    except AttributeError:
        return font.getsize(text)[0]


def _ellipsize(draw, text, font, max_width) -> str:
    if _text_width(draw, text, font) <= max_width:
        return text
    ell = "…"
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _text_width(draw, text[:mid].rstrip() + ell, font) <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + ell


def _wrap(draw, text, font, max_width, max_lines=2) -> List[str]:
    words, lines, line = text.split(), [], ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if _text_width(draw, candidate, font) <= max_width:
            line = candidate
        else:
            if line:
                lines.append(line)
            line = word
        if len(lines) == max_lines:
            break
    if line and len(lines) < max_lines:
        lines.append(line)
    if lines:
        lines[-1] = _ellipsize(draw, lines[-1], font, max_width)
    return lines


def _accent_from_text(text: str) -> Tuple[int, int, int]:
    h = int(hashlib.md5(text.encode("utf-8")).hexdigest()[:4], 16) / 0xFFFF
    r, g, b = colorsys.hsv_to_rgb(h, 0.6, 0.95)
    return int(r * 255), int(g * 255), int(b * 255)


def _vertical_gradient(top, bottom) -> Image.Image:
    grad = Image.new("RGB", (1, SIZE))
    for y in range(SIZE):
        t = y / (SIZE - 1)
        grad.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return grad.resize((SIZE, SIZE))


def _shade_mask(start_y: int, max_alpha: int) -> Image.Image:
    """Alpha mask that darkens the bottom of the frame so text stays readable."""
    mask = Image.new("L", (1, SIZE), 0)
    span = SIZE - start_y
    for y in range(start_y, SIZE):
        mask.putpixel((0, y), int(max_alpha * (((y - start_y) / span) ** 0.8)))
    return mask.resize((SIZE, SIZE))


class Renderer:
    def __init__(self):
        self.fonts = Fonts()
        self._art_key = None
        self._art_base = None
        self._accent = (235, 235, 240)
        self._shade = _shade_mask(96, 235)

    # -- helpers -----------------------------------------------------------------
    def _centered(self, d, text, y, font, fill):
        d.text(((SIZE - _text_width(d, text, font)) / 2, y), text, font=font, fill=fill)

    def _legend(self, d, top_left="", bottom_left="", top_right="", bottom_right=""):
        f = self.fonts.get(12, bold=True)
        if top_left:
            d.text((8, 8), f"A  {top_left}", font=f, fill=KEY)
        if bottom_left:
            d.text((8, SIZE - 22), f"B  {bottom_left}", font=f, fill=KEY)
        if top_right:
            t = f"{top_right}  X"
            d.text((SIZE - 8 - _text_width(d, t, f), 8), t, font=f, fill=KEY)
        if bottom_right:
            t = f"{bottom_right}  Y"
            d.text((SIZE - 8 - _text_width(d, t, f), SIZE - 22), t, font=f, fill=KEY)

    def _headphones_icon(self, d, cx, cy, color):
        d.arc((cx - 30, cy - 30, cx + 30, cy + 30), start=180, end=360, fill=color, width=6)
        d.rounded_rectangle((cx - 34, cy - 2, cx - 18, cy + 26), radius=5, fill=color)
        d.rounded_rectangle((cx + 18, cy - 2, cx + 34, cy + 26), radius=5, fill=color)

    def _phone_icon(self, d, cx, cy, color):
        d.rounded_rectangle((cx - 16, cy - 28, cx + 16, cy + 28), radius=6, outline=color, width=4)
        d.line((cx - 5, cy + 20, cx + 5, cy + 20), fill=color, width=3)
        for r in (26, 38):  # "looking for you" waves to the right
            d.arc((cx + 2 - r, cy - r, cx + 2 + r, cy + r), start=-35, end=35, fill=color, width=4)

    # -- album art: a real picture when there is one, else a colored card -----------------
    def _load_art(self, view: View):
        key = view.art or (view.album or view.title, view.artist)
        if key == self._art_key:
            return
        self._art_key = key
        if view.art:
            try:
                with Image.open(view.art) as pic:
                    pic.draft("RGB", (SIZE * 2, SIZE * 2))  # fast JPEG decoding on small Pis
                    pic = pic.convert("RGB")
                    side = min(pic.size)
                    left, top = (pic.width - side) // 2, (pic.height - side) // 2
                    img = pic.crop((left, top, left + side, top + side)).resize((SIZE, SIZE), BILINEAR)
                avg = img.resize((1, 1), BILINEAR).getpixel((0, 0))
                h, sat, _v = colorsys.rgb_to_hsv(*(c / 255 for c in avg))
                r, g, b = colorsys.hsv_to_rgb(h, max(sat, 0.45), 0.95)
                self._art_base, self._accent = img, (int(r * 255), int(g * 255), int(b * 255))
                return
            except Exception as err:  # a broken or half-downloaded picture: fall back to the card
                log.info("Couldn't use album art %s: %s", view.art, err)
        seed = (view.album or view.title or "music") + view.artist
        accent = _accent_from_text(seed)
        dark = tuple(int(c * 0.28) for c in accent)
        img = _vertical_gradient(dark, BG)
        d = ImageDraw.Draw(img)
        cx, cy = 120, 82
        col = tuple(int(c * 0.85) for c in accent)
        d.ellipse((cx - 30, cy + 18, cx + 2, cy + 44), fill=col)  # note head
        d.rectangle((cx - 4, cy - 36, cx + 2, cy + 32), fill=col)  # stem
        d.polygon([(cx + 2, cy - 36), (cx + 34, cy - 22), (cx + 34, cy - 8), (cx + 2, cy - 22)],
                  fill=col)  # flag
        self._art_base, self._accent = img, accent

    # -- frames ---------------------------------------------------------------------
    def render(self, view: View) -> Image.Image:
        frame = {
            "playing": self._now_playing,
            "connected": self._connected,
            "pairing": self._pairing,
            "confirm": self._confirm,
            "info": self._info,
            "handback": self._handback,
            "menu": self._menu,
            "pi_idle": self._pi_idle,
        }.get(view.kind, self._idle)(view)
        if view.overlay and view.kind != "handback":  # the hand-back screen shows its message inline
            frame = self._overlay(frame, view.overlay)
        return frame

    def _now_playing(self, view: View) -> Image.Image:
        self._load_art(view)
        base = self._art_base.copy()
        if not view.playing:
            base = Image.blend(base, Image.new("RGB", base.size, (0, 0, 0)), 0.45)
        frame = Image.composite(Image.new("RGB", base.size, (0, 0, 0)), base, self._shade)
        d = ImageDraw.Draw(frame)
        margin, width = 14, SIZE - 28
        title_font = self.fonts.get(21, bold=True, text=view.title)
        artist_font = self.fonts.get(16, text=view.artist)
        small = self.fonts.get(11)

        marks = ("⇄ " if view.shuffle else "") + {"all": "↻", "one": "↻1"}.get(view.repeat, "")
        mark_font = self.fonts.get(13, bold=True)
        marks_w = _text_width(d, marks.strip(), mark_font) if marks.strip() else 0
        if view.choice and not view.playing:  # just switched on: A plays the phone, X the Pi's music
            self._legend(d, top_left="play", top_right="Pi music")
        elif view.badge or view.phone:
            badge = view.badge or view.phone
            f = self.fonts.get(11, text=badge)
            d.text((margin, 10), _ellipsize(d, badge, f, width - marks_w - 8), font=f, fill=SUBTLE)
        if marks_w:
            d.text((SIZE - margin - marks_w, 8), marks.strip(), font=mark_font, fill=SUBTLE)
        title = _ellipsize(d, view.title or view.album or "Unknown track", title_font, width)
        d.text((margin, 158), title, font=title_font, fill=TEXT)
        if view.artist:
            d.text((margin, 186), _ellipsize(d, view.artist, artist_font, width), font=artist_font,
                   fill=SUBTLE)
        if view.progress is not None:
            y = 214
            d.rounded_rectangle((margin, y, SIZE - margin, y + 4), radius=2, fill=(70, 72, 80))
            fill_to = margin + int(width * max(0.0, min(1.0, view.progress)))
            if fill_to > margin + 2:
                d.rounded_rectangle((margin, y, fill_to, y + 4), radius=2, fill=self._accent)
            d.text((margin, y + 9), view.elapsed_text, font=small, fill=SUBTLE)
            d.text((SIZE - margin - _text_width(d, view.remaining_text, small), y + 9),
                   view.remaining_text, font=small, fill=SUBTLE)
        if not view.playing:
            cx, cy = 120, 82
            d.rounded_rectangle((cx - 22, cy - 26, cx - 8, cy + 26), radius=4, fill=TEXT)
            d.rounded_rectangle((cx + 8, cy - 26, cx + 22, cy + 26), radius=4, fill=TEXT)
        return frame

    def _idle(self, view: View) -> Image.Image:
        frame = _vertical_gradient((24, 30, 48), BG)
        d = ImageDraw.Draw(frame)
        hint = self.fonts.get(12)
        if view.connecting:
            self._phone_icon(d, 108, 62, BLUE)
            self._centered(d, "Connecting…", 112, self.fonts.get(20, bold=True), TEXT)
            phone = view.last_phone or "your phone"
            f = self.fonts.get(16, text=phone)
            self._centered(d, _ellipsize(d, phone, f, 220), 140, f, BLUE)
            self._centered(d, "Make sure your phone's", 180, hint, FAINT)
            self._centered(d, "Bluetooth is on", 197, hint, FAINT)
        else:
            self._headphones_icon(d, 120, 62, BLUE)
            self._centered(d, "Bluetooth ready", 112, self.fonts.get(20, bold=True), TEXT)
            name_font = self.fonts.get(16, text=view.name)
            self._centered(d, _ellipsize(d, view.name, name_font, 220), 140, name_font, BLUE)
            if view.last_phone:
                self._centered(d, "Pick it in your phone's Bluetooth", 180, hint, FAINT)
                self._centered(d, "or press A to reconnect", 197, hint, FAINT)
            else:
                self._centered(d, "No phone paired yet:", 180, hint, FAINT)
                self._centered(d, "hold A, pick Pair a new phone" if view.source else "hold A, then press X to pair",
                               197, hint, FAINT)
        if view.choice:
            self._legend(d, top_left="play", top_right="Pi music")
        elif view.source:
            self._legend(d, top_left="hold: menu")
        return frame

    def _connected(self, view: View) -> Image.Image:
        frame = _vertical_gradient((20, 46, 40), BG)
        d = ImageDraw.Draw(frame)
        self._headphones_icon(d, 120, 66, GREEN)
        self._centered(d, "Connected", 120, self.fonts.get(20, bold=True), TEXT)
        if view.phone:
            f = self.fonts.get(14, text=view.phone)
            self._centered(d, _ellipsize(d, view.phone, f, 220), 148, f, SUBTLE)
        self._centered(d, "Press A, or play on your phone", 190, self.fonts.get(12), FAINT)
        if view.choice:
            self._legend(d, top_left="play", top_right="Pi music")
        elif view.source:
            self._legend(d, top_left="hold: menu")
        return frame

    def _pi_idle(self, view: View) -> Image.Image:
        frame = _vertical_gradient((40, 30, 52), BG)
        d = ImageDraw.Draw(frame)
        cx, cy, col = 120, 62, (200, 160, 255)
        d.ellipse((cx - 26, cy + 10, cx + 2, cy + 34), fill=col)  # a music note
        d.rectangle((cx - 4, cy - 34, cx + 2, cy + 24), fill=col)
        d.polygon([(cx + 2, cy - 34), (cx + 30, cy - 22), (cx + 30, cy - 8), (cx + 2, cy - 20)], fill=col)
        self._centered(d, "Pi music", 106, self.fonts.get(14), SUBTLE)
        f = self.fonts.get(20, bold=True, text=view.title)
        self._centered(d, _ellipsize(d, view.title or "Nothing queued", f, 224), 128, f, TEXT)
        hint = self.fonts.get(12)
        self._centered(d, "Hold A to pick music,", 178, hint, FAINT)
        self._centered(d, "or add some in Iris", 195, hint, FAINT)
        self._legend(d, top_left="hold: menu")
        return frame

    def _menu(self, view: View) -> Image.Image:
        frame = Image.new("RGB", (SIZE, SIZE), BG)
        d = ImageDraw.Draw(frame)
        rows = view.menu or []
        sel = rows[view.menu_cursor] if 0 <= view.menu_cursor < len(rows) else None
        verb = {"dir": "open", "track": "play", "queued": "play", "action": "select"}.get(
            sel["kind"] if sel else "", "")
        legend = self.fonts.get(11, bold=True)
        if verb:
            d.text((8, 6), f"A  {verb}", font=legend, fill=KEY)
        t = "up  X"
        d.text((SIZE - 8 - _text_width(d, t, legend), 6), t, font=legend, fill=KEY)
        d.text((8, SIZE - 18), "B  back", font=legend, fill=KEY)
        t = "down  Y"
        d.text((SIZE - 8 - _text_width(d, t, legend), SIZE - 18), t, font=legend, fill=KEY)

        title_font = self.fonts.get(16, bold=True, text=view.title)
        count = f"{view.menu_offset + view.menu_cursor + 1}/{view.menu_total}" if view.menu_total > MENU_ROWS else ""
        small = self.fonts.get(11)
        count_w = _text_width(d, count, small) if count else 0
        d.text((12, 24), _ellipsize(d, view.title, title_font, 216 - count_w - 8), font=title_font, fill=TEXT)
        if count:
            d.text((228 - count_w, 28), count, font=small, fill=FAINT)

        top, row_h = 50, 28
        if not rows:
            note = view.menu_note or "Nothing here yet"
            for i, line in enumerate(_wrap(d, note, self.fonts.get(13), 200, max_lines=4)):
                self._centered(d, line, top + 40 + 18 * i, self.fonts.get(13), FAINT)
            return frame
        for i, row in enumerate(rows):
            y = top + i * row_h
            chosen = i == view.menu_cursor
            if chosen:
                d.rounded_rectangle((6, y + 1, 226, y + row_h - 2), radius=7, fill=ROW_SEL)
            x, right = 14, 220
            label = row["label"]
            font = self.fonts.get(15, bold=chosen, text=label)
            if row.get("current"):
                d.text((x, y + 5), "♪", font=self.fonts.get(15, bold=True), fill=GREEN)
                x += 16
            value = row.get("value") or ("›" if row["kind"] == "dir" else "")
            if value:
                vf = self.fonts.get(13, bold=row["kind"] != "dir")
                vw = _text_width(d, value, vf)
                d.text((right - vw, y + 6), value, font=vf, fill=KEY if row["kind"] == "action" else FAINT)
                right -= vw + 8
            d.text((x, y + 5), _ellipsize(d, label, font, right - x), font=font, fill=TEXT if chosen else SUBTLE)
        if view.menu_total > MENU_ROWS:  # scroll bar
            span = MENU_ROWS * row_h
            thumb = max(14, int(span * MENU_ROWS / view.menu_total))
            pos = int((span - thumb) * view.menu_offset / max(1, view.menu_total - MENU_ROWS))
            d.rounded_rectangle((231, top + pos, 234, top + pos + thumb), radius=2, fill=FAINT)
        return frame

    def _pairing(self, view: View) -> Image.Image:
        frame = _vertical_gradient((22, 32, 64), BG)
        d = ImageDraw.Draw(frame)
        self._phone_icon(d, 108, 76, BLUE)
        self._centered(d, "Pairing mode", 118, self.fonts.get(20, bold=True), TEXT)
        hint = self.fonts.get(12)
        self._centered(d, "On your iPhone: Settings › Bluetooth,", 148, hint, SUBTLE)
        name_font = self.fonts.get(14, bold=True, text=view.name)
        self._centered(d, "then tap " + _ellipsize(d, view.name, name_font, 150), 166, name_font, BLUE)
        if view.countdown:
            self._centered(d, f"{view.countdown} left", 194, self.fonts.get(12), FAINT)
        self._legend(d, bottom_left="cancel")
        return frame

    def _confirm(self, view: View) -> Image.Image:
        frame = Image.new("RGB", (SIZE, SIZE), BG)
        d = ImageDraw.Draw(frame)
        self._legend(d, top_left="pair", bottom_left="cancel")
        f = self.fonts.get(14, text=view.phone)
        self._centered(d, _ellipsize(d, f"Pair with {view.phone or 'this phone'}?", f, 220), 52, f, TEXT)
        code = view.code
        shown = f"{code[:3]} {code[3:]}" if len(code) == 6 else code
        self._centered(d, shown, 86, self.fonts.get(44, bold=True), KEY)
        hint = self.fonts.get(12)
        self._centered(d, "If your phone shows the same", 152, hint, SUBTLE)
        self._centered(d, "code, press A here and Pair there", 169, hint, SUBTLE)
        return frame

    def _handback(self, view: View) -> Image.Image:
        frame = _vertical_gradient((30, 30, 36), BG)
        d = ImageDraw.Draw(frame)
        y = 84
        if view.overlay and view.overlay[0] == "toast":  # e.g. "Kite's iPhone disconnected"
            f = self.fonts.get(13, text=str(view.overlay[1]))
            self._centered(d, _ellipsize(d, str(view.overlay[1]), f, 220), 60, f, SUBTLE)
            y = 96
        self._centered(d, "Back to Mopidy…", y, self.fonts.get(20, bold=True), TEXT)
        self._centered(d, "Bluetooth stays ready", y + 36, self.fonts.get(12), FAINT)
        return frame

    def _info(self, view: View) -> Image.Image:
        frame = Image.new("RGB", (SIZE, SIZE), BG)
        d = ImageDraw.Draw(frame)
        self._legend(d, top_left="close", top_right="pair new", bottom_left="vol −", bottom_right="vol +")
        label_font = self.fonts.get(11)
        y = 34
        for label, value in view.info[:5]:
            d.text((18, y), label.upper(), font=label_font, fill=FAINT)
            vf = self.fonts.get(14, bold=True, text=value)
            d.text((18, y + 12), _ellipsize(d, value, vf, 204), font=vf, fill=TEXT)
            y += 34
        return frame

    def _overlay(self, frame: Image.Image, overlay) -> Image.Image:
        frame = frame.copy()
        layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        if overlay[0] == "volume":
            pct = max(0, min(100, int(overlay[1])))
            d.rounded_rectangle((20, 86, 220, 152), radius=14, fill=(0, 0, 0, 200))
            f = self.fonts.get(13, bold=True)
            d.text((36, 98), overlay[2] if len(overlay) > 2 else "Headphones", font=f, fill=TEXT)
            t = f"{pct}%"
            d.text((204 - _text_width(d, t, f), 98), t, font=f, fill=TEXT)
            d.rounded_rectangle((36, 124, 204, 134), radius=5, fill=(80, 84, 94, 255))
            if pct > 0:
                d.rounded_rectangle((36, 124, 36 + int(168 * pct / 100), 134), radius=5,
                                    fill=(255, 255, 255, 255))
        elif overlay[0] == "toast":
            f = self.fonts.get(14, bold=True)
            lines = _wrap(d, str(overlay[1]), f, 184, max_lines=3)
            h = 20 + 20 * len(lines)
            top = 120 - h // 2
            d.rounded_rectangle((16, top, 224, top + h), radius=12, fill=(0, 0, 0, 215))
            for i, line in enumerate(lines):
                d.text(((SIZE - _text_width(d, line, f)) / 2, top + 10 + 20 * i), line, font=f,
                       fill=TEXT)
        return Image.alpha_composite(frame.convert("RGBA"), layer).convert("RGB")
