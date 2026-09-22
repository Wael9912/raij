"""Burned-in Arabic subtitles, rendered as transparent PNGs with Pillow.

The local ffmpeg has no libass/drawtext, so subtitles are drawn in Python. Arabic is shaped by
HarfBuzz (Pillow raqm, see src/textshape.py) in the channel font (brand.font) and laid out word by
word right-to-left; runs of Latin/number words keep left-to-right order.

Words come from the voice timings (Phase 5), so text and timing always agree. Each cue is up to
two lines; the word being spoken sits on a yellow pill. The PNG sequence plays through ffmpeg's
concat demuxer as one overlay stream.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from src.assemble import brand

_ARABIC = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
_QUOTES = str.maketrans({"«": '"', "»": '"', "“": '"', "”": '"'})


@dataclass
class Style:
    width: int = 1080
    band_height: int = 380             # PNG band; placed at `top` on the 1080×1920 frame
    top: int = 1250                    # clear of Reels/Shorts bottom UI (~last 300 px)
    max_line_px: int = 940
    size: int = 92
    line_gap: int = 34
    stroke: int = 7
    fill: tuple = (255, 255, 255, 255)
    highlight: tuple = (255, 212, 0, 255)      # pill behind the spoken word
    highlight_text: tuple = (18, 22, 34, 255)
    stroke_fill: tuple = (0, 0, 0, 255)


@dataclass
class Cue:
    start: float
    end: float
    lines: list[list[int]]             # word indices per line (logical order)


class Renderer:
    def __init__(self, style: Style | None = None):
        self.style = style or Style()
        self.ar = brand.font(self.style.size)
        self.space = max(self.ar.getlength(" "), self.style.size * 0.27)   # room for the highlight pill

    # -- words ---------------------------------------------------------------
    @staticmethod
    def is_ltr(word: str) -> bool:
        return not _ARABIC.search(word)

    @staticmethod
    def glyphs(word: str) -> str:
        return word.translate(_QUOTES)

    def font(self, word: str) -> ImageFont.FreeTypeFont:
        return self.ar

    def width(self, word: str) -> float:
        w = self.glyphs(word)
        return self.ar.getlength(w, **brand.text_kw(w)) + 2 * self.style.stroke

    # -- layout --------------------------------------------------------------
    def line_px(self, words: list[str], line: list[int]) -> float:
        return sum(self.width(words[i]) for i in line) + self.space * (len(line) - 1)

    def wrap(self, words: list[str], idx: list[int]) -> list[list[int]]:
        """Line breaks by pixel width; a two-line cue is split where its lines are most even."""
        lines: list[list[int]] = [[]]
        used = 0.0
        for i in idx:
            w = self.width(words[i])
            if lines[-1] and used + self.space + w > self.style.max_line_px:
                lines.append([])
                used = 0.0
            used += (self.space if lines[-1] else 0) + w
            lines[-1].append(i)
        if len(lines) == 2:
            splits = [(max(self.line_px(words, idx[:k]), self.line_px(words, idx[k:])), k)
                      for k in range(1, len(idx))]
            best, k = min(splits)
            if best <= self.style.max_line_px:
                lines = [idx[:k], idx[k:]]
        return lines

    def visual_order(self, words: list[str], line: list[int]) -> list[int]:
        """Right-to-left word order, keeping consecutive LTR words (Latin, numbers) left-to-right."""
        runs: list[list[int]] = []
        for i in line:
            if runs and self.is_ltr(words[i]) and self.is_ltr(words[runs[-1][-1]]):
                runs[-1].append(i)
            else:
                runs.append([i])
        order = []
        for run in reversed(runs):                     # RTL between runs
            order.extend(run)                          # LTR inside a run
        return order

    def draw(self, words: list[str], cue: Cue, active: int | None) -> Image.Image:
        s = self.style
        img = Image.new("RGBA", (s.width, s.band_height), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        line_h = s.size + s.line_gap
        y = s.band_height - line_h * len(cue.lines) - s.stroke
        for line in cue.lines:
            order = self.visual_order(words, line)
            total = self.line_px(words, order)
            x = (s.width - total) / 2
            base = y + s.size * 0.8
            for i in order:
                w, wd = self.glyphs(words[i]), self.width(words[i])
                kw = brand.text_kw(w)
                if i == active:
                    pad = s.size * 0.16
                    d.rounded_rectangle((x - pad + s.stroke / 2, base - s.size * 0.98, x + wd + pad - s.stroke / 2,
                                         base + s.size * 0.36), radius=int(s.size * 0.24), fill=s.highlight)
                    d.text((x + s.stroke, base), w, font=self.ar, fill=s.highlight_text, anchor="ls", **kw)
                else:
                    d.text((x + s.stroke, base), w, font=self.ar, fill=s.fill, anchor="ls",
                           stroke_width=s.stroke, stroke_fill=s.stroke_fill, **kw)
                x += wd + self.space
            y += line_h
        return img


def make_cues(words: list[dict[str, Any]], beats: list[dict[str, Any]], renderer: Renderer,
              max_words: int = 7, max_gap: float = 0.6) -> list[Cue]:
    """Group timed words into ≤2-line cues; never span a beat boundary or a long pause."""
    beat_starts = [b["start"] for b in beats[1:]]
    texts = [w["text"] for w in words]
    groups: list[list[int]] = [[]]
    for i, w in enumerate(words):
        g = groups[-1]
        if g:
            crosses_beat = any(words[g[-1]]["start"] < bs <= w["start"] for bs in beat_starts)
            paused = w["start"] - words[g[-1]]["end"] > max_gap
            too_wide = len(renderer.wrap(texts, g + [i])) > 2
            if crosses_beat or paused or too_wide or len(g) >= max_words:
                groups.append([])
        groups[-1].append(i)
    cues = []
    for g in groups:
        if g:
            cues.append(Cue(words[g[0]]["start"], words[g[-1]]["end"], renderer.wrap(texts, g)))
    return cues


def render_sequence(words: list[dict[str, Any]], beats: list[dict[str, Any]], out_dir: Path,
                    total: float, renderer: Renderer | None = None, hide_until: float = 0.0) -> Path:
    """Write subtitle PNGs + an ffmpeg concat list covering [0, total]; returns the list path.
    Nothing shows before `hide_until` (the on-screen hook title has the screen then)."""
    renderer = renderer or Renderer()
    out_dir.mkdir(parents=True, exist_ok=True)
    texts = [w["text"] for w in words]
    blank = out_dir / "blank.png"
    Image.new("RGBA", (renderer.style.width, renderer.style.band_height), (0, 0, 0, 0)).save(blank)

    entries: list[tuple[Path, float]] = []
    t = 0.0
    for c, cue in enumerate(make_cues(words, beats, renderer)):
        if cue.end <= hide_until:
            continue
        shown_from = max(cue.start, hide_until)
        if shown_from > t:
            entries.append((blank, shown_from - t))
        idx = [i for line in cue.lines for i in line]
        for k, i in enumerate(idx):
            start = max(words[i]["start"] if k else cue.start, shown_from)
            end = words[idx[k + 1]]["start"] if k + 1 < len(idx) else cue.end
            if end <= start:
                continue
            png = out_dir / f"c{c:03d}_{k:02d}.png"
            renderer.draw(texts, cue, active=i).save(png, optimize=True)
            entries.append((png, end - start))
        t = cue.end
    if total > t:
        entries.append((blank, total - t))

    lst = out_dir / "subs.txt"
    body = "ffconcat version 1.0\n"
    for png, dur in entries:
        body += f"file '{png.name}'\nduration {dur:.3f}\n"
    body += f"file '{entries[-1][0].name}'\n"          # concat demuxer needs the last file repeated
    lst.write_text(body, encoding="utf-8")
    return lst


def srt(words: list[dict[str, Any]], beats: list[dict[str, Any]], renderer: Renderer | None = None) -> str:
    """Plain SRT of the same cues, for platform caption uploads."""
    renderer = renderer or Renderer()

    def ts(x: float) -> str:
        ms = int(round(x * 1000))
        return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"

    out = []
    for n, cue in enumerate(make_cues(words, beats, renderer), 1):
        lines = "\n".join(" ".join(words[i]["text"] for i in line) for line in cue.lines)
        out.append(f"{n}\n{ts(cue.start)} --> {ts(cue.end)}\n{lines}\n")
    return "\n".join(out)
