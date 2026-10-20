#!/usr/bin/env python3
"""Pixel runner for the experiment track: a small white wolf in red shorts, seen from the side, facing right.

Run `python3 sprite.py` to rebuild assets/runner.png (and `--preview out.png` for an 8x enlarged check).
Shapes are filled with flat colours; the dark outline is added automatically around every filled pixel,
so all poses keep one consistent side-view silhouette.
"""
import math
import struct
import sys
import zlib
from pathlib import Path

W, H = 22, 24          # one frame; the figure stands about 20 px tall on the ground row y = 23
GROUND = 23
COLORS = {
    "w": (246, 246, 242), "g": (196, 202, 212), "r": (214, 58, 47), "d": (160, 38, 30),
    "e": (38, 38, 38), "p": (238, 160, 160), "f": (178, 184, 194), "k": (38, 38, 38), "q": (204, 125, 94),
}
# Animation rows, in sheet order: (name, frames)
ROWS = [("run", 6), ("look", 2), ("stumble", 4), ("jump", 4), ("rest", 2)]


class Part:
    """One body part: flat fill; its own outline is added when it is laid onto the frame."""
    def __init__(self):
        self.px = {}

    def put(self, x, y, c):
        self.px[(round(x), round(y))] = c

    def ellipse(self, cx, cy, rx, ry, c):
        for y in range(math.floor(cy - ry), math.ceil(cy + ry) + 1):
            for x in range(math.floor(cx - rx), math.ceil(cx + rx) + 1):
                if ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1.0:
                    self.put(x, y, c)

    def line(self, a, b, c, width=1):
        steps = max(1, int(max(abs(b[0] - a[0]), abs(b[1] - a[1])) * 2))
        for i in range(steps + 1):
            x = a[0] + (b[0] - a[0]) * i / steps
            y = a[1] + (b[1] - a[1]) * i / steps
            for d in range(width):
                self.put(x + d - (width - 1) / 2, y, c)

    def tri(self, pts, c):
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        for y in range(math.floor(min(ys)), math.ceil(max(ys)) + 1):
            for x in range(math.floor(min(xs)), math.ceil(max(xs)) + 1):
                (x1, y1), (x2, y2), (x3, y3) = pts
                d = (y2 - y3) * (x1 - x3) + (x3 - x2) * (y1 - y3)
                a = ((y2 - y3) * (x - x3) + (x3 - x2) * (y - y3)) / d
                b = ((y3 - y1) * (x - x3) + (x1 - x3) * (y - y3)) / d
                if a >= -0.05 and b >= -0.05 and 1 - a - b >= -0.05:
                    self.put(x, y, c)


class Frame:
    def __init__(self):
        self.px = {}

    def lay(self, part, outline=True):
        """Paint a part over what is already there, outline first, so overlaps show a dividing line."""
        if outline:
            for (x, y) in part.px:
                for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if (nx, ny) not in part.px and 0 <= nx < W and 0 <= ny < H:
                        self.px[(nx, ny)] = "k"
        for (x, y), c in part.px.items():
            if 0 <= x < W and 0 <= y < H:
                self.px[(x, y)] = c


def leg(hip, foot, c):
    """Two-segment leg with the knee pushed forward (to the right) and a small foot pointing forward."""
    p = Part()
    kx, ky = (hip[0] + foot[0]) / 2 + 1, (hip[1] + foot[1]) / 2
    p.line(hip, (kx, ky), c, 1)
    p.line((kx, ky), foot, c, 1)
    p.put(foot[0] + 1, foot[1], "f")
    p.put(foot[0], foot[1], "f")
    return p


def arm(shoulder, hand, c):
    p = Part()
    p.line(shoulder, hand, c, 1)
    p.put(hand[0], hand[1], c)
    return p


def wolf(dx=0, dy=0, near_foot=(12, GROUND), far_foot=(10, GROUND), near_hand=(13, 16), far_hand=(11, 16),
         eye="open", tilt=0, sitting=False, mark=None):
    """One pose. dx/dy move the upper body; feet and hands are absolute; tilt leans the head forward/down."""
    f = Frame()
    bx, by = 10 + dx, 15 + dy                            # torso centre
    hx, hy = 12 + dx + tilt, 8 + dy + abs(tilt)          # head centre
    hip = by + 4
    shoulder = (bx + 1, by - 1)
    if not sitting:
        f.lay(leg((bx - 1, hip), far_foot, "g"))
    tail = Part()
    tail.line((bx - 2, by + 1), (bx - 5, by - 1), "w", 2)
    tail.line((bx - 5, by - 1), (bx - 6, by - 3), "w", 2)
    tail.put(bx - 6, by - 4, "g")
    f.lay(tail)
    body = Part()
    for y in range(round(by - 2), round(by + 2)):
        for x in range(round(bx - 2), round(bx + 3)):
            body.put(x, y, "w")
    for x in range(round(bx - 3), round(bx + 3)):            # shorts, a bit wider than the waist
        body.put(x, by + 2, "r")
        body.put(x, by + 3, "r" if bx - 3 < x < bx + 2 else "d")
    f.lay(body)
    if sitting:
        thigh = Part()
        thigh.line((bx + 1, hip + 1), near_foot, "w", 1)
        thigh.put(near_foot[0] + 1, near_foot[1], "f")
        f.lay(thigh)
    else:
        f.lay(leg((bx + 1, hip), near_foot, "w"))
    head = Part()
    head.ellipse(hx, hy, 4.2, 3.6, "w")
    head.ellipse(hx + 4.2, hy + 1.3, 2.3, 1.4, "w")      # snout
    head.tri(((hx - 3.2, hy - 1.5), (hx - 2.2, hy - 6.2), (hx - 0.6, hy - 2.6)), "w")   # back ear
    head.tri(((hx - 0.8, hy - 2.6), (hx + 0.6, hy - 6.4), (hx + 1.9, hy - 2.2)), "w")   # front ear
    head.put(hx - 2.0, hy - 3.6, "p")
    head.put(hx + 0.6, hy - 3.8, "p")
    head.put(hx - 2, hy + 2.6, "g")
    head.put(hx - 1, hy + 3.0, "g")
    head.put(hx + 6.4, hy + 0.9, "e")                    # nose
    if eye == "open":
        head.put(hx + 2.0, hy - 0.4, "e")
        head.put(hx + 2.0, hy + 0.6, "e")
    elif eye == "back":
        head.put(hx + 1.0, hy - 0.4, "e")
        head.put(hx + 1.0, hy + 0.6, "e")
    else:
        head.put(hx + 1.5, hy + 0.6, "e")
        head.put(hx + 2.5, hy + 0.6, "e")
    f.lay(head)
    f.lay(arm(shoulder, near_hand, "w"))
    if mark == "?":
        q = Part()
        for x, y in ((18, 0), (19, 0), (20, 1), (19, 2), (19, 4)):
            q.put(x, y, "q")
        f.lay(q, outline=False)
    return f


def frames():
    G = GROUND
    run = [  # contact, down, pass, contact (other leg), down, pass
        wolf(0, 0, (16, G), (6, G - 1), (9, 15), (16, 13)),
        wolf(0, 1, (14, G), (7, G - 2), (10, 16), (15, 14)),
        wolf(0, 0, (11, G), (12, G - 3), (12, 16), (13, 16)),
        wolf(0, 0, (7, G - 1), (16, G), (16, 13), (9, 15)),
        wolf(0, 1, (8, G - 2), (14, G), (15, 14), (10, 16)),
        wolf(0, -1, (12, G - 3), (11, G), (13, 16), (12, 16)),
    ]
    look = [
        wolf(0, 0, (13, G), (9, G), (13, 17), (11, 17)),
        wolf(0, 0, (13, G), (9, G), (13, 17), (11, 17), eye="back", mark="?"),
    ]
    stumble = [
        wolf(1, 0, (16, G - 1), (8, G), (17, 14), (16, 15), tilt=1),
        wolf(2, 2, (17, G), (7, G - 2), (19, 19), (18, 19), tilt=1),
        wolf(2, 3, (16, G), (8, G), (19, 22), (18, 22), tilt=2, eye="closed"),
        wolf(1, 1, (14, G), (9, G), (15, 17), (13, 17)),
    ]
    jump = [
        wolf(0, 2, (13, G), (9, G), (13, 18), (11, 18)),
        wolf(0, -2, (14, G - 3), (10, G - 3), (16, 9), (14, 10)),
        wolf(0, -3, (13, G - 4), (10, G - 5), (16, 6), (14, 7), eye="closed"),
        wolf(0, 1, (14, G), (9, G), (15, 15), (12, 16)),
    ]
    rest = [
        wolf(0, 3, (17, G), (15, G), (14, 19), (12, 19), sitting=True),
        wolf(0, 4, (17, G), (15, G), (14, 20), (12, 20), eye="closed", sitting=True),
    ]
    return [run, look, stumble, jump, rest]


def png(width, height, pixels):
    raw = b"".join(b"\x00" + b"".join(bytes(pixels[y][x]) for x in range(width)) for y in range(height))
    chunk = lambda tag, data: struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def sheet(scale=1):
    rows = frames()
    cols = max(len(r) for r in rows)
    width, height = cols * W * scale, len(rows) * H * scale
    pixels = [[(0, 0, 0, 0)] * width for _ in range(height)]
    for ri, row in enumerate(rows):
        for ci, fr in enumerate(row):
            for (x, y), c in fr.px.items():
                for sy in range(scale):
                    for sx in range(scale):
                        pixels[(ri * H + y) * scale + sy][(ci * W + x) * scale + sx] = COLORS[c] + (255,)
    return png(width, height, pixels)


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    (here / "assets/runner.png").write_bytes(sheet())
    if len(sys.argv) > 2 and sys.argv[1] == "--preview":
        Path(sys.argv[2]).write_bytes(sheet(8))
    print("assets/runner.png", [(n, k) for n, k in ROWS], f"{W}x{H} per frame")
