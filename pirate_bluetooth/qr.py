"""A small QR code maker (byte mode, error correction level M, versions 1-10), for the hotspot screen.

An iPhone's camera reads a "WIFI:" QR code and offers to join that Wi-Fi, so nobody has to type the
hotspot's password. Plain Python, so it needs no extra package on the Pi.
"""

# Level M: (EC codewords per block, [(blocks, data codewords per block), ...]) for versions 1-10
_BLOCKS = {
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]), 5: (24, [(2, 43)]),
    6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]), 9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
          9: [6, 26, 46], 10: [6, 28, 50]}
_ECL_BITS = 0  # level M in the format information


# == Reed-Solomon over GF(256) ==================================================================
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _generator(degree):
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, c in enumerate(poly):
            nxt[j] ^= c
            nxt[j + 1] ^= _mul(c, _EXP[i])
        poly = nxt
    return poly


def _ec_codewords(data, degree):
    gen = _generator(degree)
    rem = list(data) + [0] * degree
    for i in range(len(data)):
        coef = rem[i]
        if coef:
            for j in range(1, len(gen)):
                rem[i + j] ^= _mul(gen[j], coef)
    return rem[len(data):]


# == encoding ==================================================================================
def _capacity(version):
    _ec, groups = _BLOCKS[version]
    return sum(n * size for n, size in groups)


def _codewords(data: bytes):
    for version in range(1, 11):
        count_bits = 8 if version < 10 else 16
        if 4 + count_bits + 8 * len(data) <= 8 * _capacity(version):
            break
    else:
        raise ValueError("Too much text for a QR code this size")
    bits = [0, 1, 0, 0]  # byte mode
    bits += [(len(data) >> i) & 1 for i in range(count_bits - 1, -1, -1)]
    for byte in data:
        bits += [(byte >> i) & 1 for i in range(7, -1, -1)]
    total = 8 * _capacity(version)
    bits += [0] * min(4, total - len(bits))
    bits += [0] * (-len(bits) % 8)
    out = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(out) < _capacity(version):
        out.append(pad)
        pad ^= 0xEC ^ 0x11
    # split into blocks, add error correction, interleave
    ec_len, groups = _BLOCKS[version]
    blocks, pos = [], 0
    for n, size in groups:
        for _ in range(n):
            blocks.append(out[pos:pos + size])
            pos += size
    ecs = [_ec_codewords(b, ec_len) for b in blocks]
    result = []
    for i in range(max(len(b) for b in blocks)):
        result += [b[i] for b in blocks if i < len(b)]
    for i in range(ec_len):
        result += [e[i] for e in ecs]
    return version, result


# == the picture =================================================================================
class _Grid:
    def __init__(self, version):
        self.version = version
        self.size = 17 + 4 * version
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.fixed = [[False] * self.size for _ in range(self.size)]

    def put(self, x, y, dark):  # x = column, y = row
        self.dark[y][x] = dark
        self.fixed[y][x] = True

    def draw_function_patterns(self):
        n = self.size
        for i in range(n):  # timing patterns
            self.put(6, i, i % 2 == 0)
            self.put(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):  # finders with their white borders
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        d = max(abs(dx), abs(dy))
                        self.put(x, y, d not in (2, 4))
        align = _ALIGN[self.version]
        last = len(align) - 1
        for i, ay in enumerate(align):
            for j, ax in enumerate(align):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue  # those corners are finders
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.put(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(0)  # reserve the spots (real bits later)
        self.draw_version()

    def draw_format(self, mask):
        data = _ECL_BITS << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        bit = [((bits >> i) & 1) == 1 for i in range(15)]
        n = self.size
        for i in range(6):
            self.put(8, i, bit[i])
        self.put(8, 7, bit[6])
        self.put(8, 8, bit[7])
        self.put(7, 8, bit[8])
        for i in range(9, 15):
            self.put(14 - i, 8, bit[i])
        for i in range(8):
            self.put(n - 1 - i, 8, bit[i])
        for i in range(8, 15):
            self.put(8, n - 15 + i, bit[i])
        self.put(8, n - 8, True)  # always dark

    def draw_version(self):
        if self.version < 7:
            return
        rem = self.version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = self.version << 12 | rem
        for i in range(18):
            dark = ((bits >> i) & 1) == 1
            a, b = self.size - 11 + i % 3, i // 3
            self.put(a, b, dark)
            self.put(b, a, dark)

    def draw_data(self, codewords):
        n = self.size
        i, total = 0, len(codewords) * 8
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = n - 1 - vert if upward else vert
                    if not self.fixed[y][x] and i < total:
                        self.dark[y][x] = ((codewords[i >> 3] >> (7 - (i & 7))) & 1) == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask):
        test = [
            lambda y, x: (x + y) % 2 == 0, lambda y, x: y % 2 == 0, lambda y, x: x % 3 == 0,
            lambda y, x: (x + y) % 3 == 0, lambda y, x: (x // 3 + y // 2) % 2 == 0,
            lambda y, x: x * y % 2 + x * y % 3 == 0, lambda y, x: (x * y % 2 + x * y % 3) % 2 == 0,
            lambda y, x: ((x + y) % 2 + x * y % 3) % 2 == 0,
        ][mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fixed[y][x] and test(y, x):
                    self.dark[y][x] = not self.dark[y][x]

    def penalty(self):
        n, grid, score = self.size, self.dark, 0
        lines = [row for row in grid] + [[grid[y][x] for y in range(n)] for x in range(n)]
        pattern_a = [True, False, True, True, True, False, True, False, False, False, False]
        pattern_b = pattern_a[::-1]
        for line in lines:
            run, prev = 0, None
            for cell in line:
                if cell == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + run - 5
                    run, prev = 1, cell
            if run >= 5:
                score += 3 + run - 5
            for i in range(n - 10):
                window = line[i:i + 11]
                if window == pattern_a or window == pattern_b:
                    score += 40
        for y in range(n - 1):
            for x in range(n - 1):
                c = grid[y][x]
                if c == grid[y][x + 1] == grid[y + 1][x] == grid[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(row) for row in grid)
        total = n * n
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        return score + k * 10


def make(text: str):
    """The QR code for text, as rows of booleans (True = dark), without the light margin around it."""
    version, codewords = _codewords(text.encode("utf-8"))
    best = None
    for mask in range(8):
        g = _Grid(version)
        g.draw_function_patterns()
        g.draw_data(codewords)
        g.apply_mask(mask)
        g.draw_format(mask)
        p = g.penalty()
        if best is None or p < best[0]:
            best = (p, g)
    return best[1].dark


def wifi_text(ssid: str, password: str) -> str:
    """What phones read as "join this Wi-Fi" (WPA/WPA2 with a password, else an open network)."""
    def esc(s):
        return "".join("\\" + c if c in '\\;,:"' else c for c in s)
    if password:
        return f"WIFI:T:WPA;S:{esc(ssid)};P:{esc(password)};;"
    return f"WIFI:T:nopass;S:{esc(ssid)};;"
