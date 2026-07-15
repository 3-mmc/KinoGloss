#!/usr/bin/env python3
"""KinoGloss: merge two SRT subtitle files into bilingual gloss subtitles.

Takes a main-language SRT and a gloss-language SRT, aligns their cues even
when one file is time-shifted relative to the other, and writes a single SRT
where each cue shows the main text with the gloss beneath it in italics.
Timestamps are taken from whichever input the user chooses; the other file's
cues are shifted onto that timeline automatically (or by a manual offset).

Zero dependencies; Python 3.9+.
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, replace
from pathlib import Path

TIMESTAMP_RE = re.compile(
    r"(\d{1,2}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
)

# Deltas within this many ms of each other are considered the same offset
# when voting on the global shift between the two files.
OFFSET_CLUSTER_TOLERANCE_MS = 400

# Largest automatic offset considered between the two files. Beyond this,
# pass --offset explicitly.
MAX_AUTO_OFFSET_MS = 120_000

# Sync-report tuning: segment length for the per-segment table, the slope
# (ms per minute) above which residuals count as drift, and the deviation of
# a segment median from the overall median that counts as a sync jump.
SYNC_SEGMENT_MS = 10 * 60_000
DRIFT_SLOPE_MS_PER_MIN = 2.0
DRIFT_TOTAL_MS = 800
JUMP_MS = 700

# Below this fraction of reference cues finding a partner, residual analysis
# is unreliable (the surviving pairs are largely coincidental) and the sync
# verdict reports the poor match rate instead.
MIN_MATCH_FRACTION = 0.6

# Named framerate conversions to recognize in a fitted drift rate.
COMMON_RATE_RATIOS = [
    (25 / 23.976, "25 → 23.976 fps"),
    (23.976 / 25, "23.976 → 25 fps"),
    (25 / 24, "25 → 24 fps"),
    (24 / 25, "24 → 25 fps"),
    (24 / 23.976, "24 → 23.976 fps"),
    (23.976 / 24, "23.976 → 24 fps"),
]


@dataclass(frozen=True)
class Cue:
    start: int  # milliseconds
    end: int    # milliseconds
    text: str

    @property
    def duration(self) -> int:
        return self.end - self.start

    @property
    def midpoint(self) -> float:
        return (self.start + self.end) / 2


@dataclass(frozen=True)
class MergedCue:
    start: int
    end: int
    main: str   # may be empty when only the gloss file has a cue here
    gloss: str  # may be empty when no gloss cue matched


def parse_timestamp_ms(h: str, m: str, s: str, ms: str) -> int:
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms.ljust(3, "0"))


def format_timestamp(ms: int) -> str:
    ms = max(0, ms)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{milli:03d}"


def read_srt_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def parse_srt(text: str) -> list[Cue]:
    cues: list[Cue] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    for block in blocks:
        lines = [line for line in block.split("\n")]
        # Find the timing line; anything before it (the numeric index) is skipped.
        timing_idx = None
        match = None
        for i, line in enumerate(lines):
            match = TIMESTAMP_RE.search(line)
            if match:
                timing_idx = i
                break
        if timing_idx is None or match is None:
            continue
        start = parse_timestamp_ms(*match.groups()[:4])
        end = parse_timestamp_ms(*match.groups()[4:])
        body = "\n".join(lines[timing_idx + 1:]).strip("\n").strip()
        if body:
            cues.append(Cue(start=start, end=end, text=body))
    cues.sort(key=lambda c: c.start)
    return cues


def _count_overlapping(reference: list[Cue], other: list[Cue], offset: int) -> int:
    """How many `other` cues overlap some reference cue after adding `offset`."""
    ref_starts = [c.start for c in reference]
    count = 0
    for cue in other:
        start, end = cue.start + offset, cue.end + offset
        # Reference cues that could overlap sit near this insertion point;
        # scan a few neighbors on each side to allow for overlapping refs.
        pos = bisect_left(ref_starts, end)
        for ref in reference[max(0, pos - 4):pos + 1]:
            if min(ref.end, end) - max(ref.start, start) > 0:
                count += 1
                break
    return count


def estimate_offset(reference: list[Cue], other: list[Cue]) -> tuple[int, float]:
    """Estimate how many ms to add to `other` cues to land on `reference` time.

    Every pairing of an `other` cue with a reference cue starting within
    MAX_AUTO_OFFSET_MS proposes a delta. The true offset shows up as a dense
    cluster of near-identical deltas (one vote from nearly every cue), while
    coincidental pairings scatter. The densest clusters are then verified by
    actually shifting the cues and counting real overlaps, which breaks ties
    when dialogue is evenly spaced. Returns (offset_ms, support) where support
    is the fraction of `other` cues that overlap a reference cue at the
    chosen offset.
    """
    if not reference or not other:
        return 0, 0.0

    ref_starts = sorted(c.start for c in reference)
    deltas: list[int] = []
    for cue in other:
        lo = bisect_left(ref_starts, cue.start - MAX_AUTO_OFFSET_MS)
        hi = bisect_right(ref_starts, cue.start + MAX_AUTO_OFFSET_MS)
        deltas.extend(s - cue.start for s in ref_starts[lo:hi])
    if not deltas:
        return 0, 0.0
    deltas.sort()

    # Sliding-window cluster sizes over the sorted deltas.
    windows: list[tuple[int, int, int]] = []  # (count, lo, hi)
    hi = 0
    for lo in range(len(deltas)):
        if hi < lo:
            hi = lo
        while hi < len(deltas) and deltas[hi] - deltas[lo] <= OFFSET_CLUSTER_TOLERANCE_MS:
            hi += 1
        windows.append((hi - lo, lo, hi))

    # Top few non-overlapping clusters become candidate offsets.
    windows.sort(key=lambda w: -w[0])
    candidates: list[int] = []
    taken: list[tuple[int, int]] = []
    for count, lo, hi in windows:
        if len(candidates) >= 5 or count < windows[0][0] // 2:
            break
        if any(lo < t_hi and hi > t_lo for t_lo, t_hi in taken):
            continue
        candidates.append(int(statistics.median(deltas[lo:hi])))
        taken.append((lo, hi))

    best_offset, best_score = 0, -1
    for offset in candidates:
        score = _count_overlapping(reference, other, offset)
        if score > best_score:
            best_offset, best_score = offset, score
    return best_offset, best_score / len(other)


def shift_cues(cues: list[Cue], offset_ms: int) -> list[Cue]:
    return [replace(c, start=c.start + offset_ms, end=c.end + offset_ms) for c in cues]


def scale_cues(cues: list[Cue], rate: float, offset_ms: float) -> list[Cue]:
    """Apply the linear time map t' = rate * t + offset to every cue."""
    return [
        replace(c, start=round(c.start * rate + offset_ms), end=round(c.end * rate + offset_ms))
        for c in cues
    ]


def _theil_sen(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Robust line fit: median of pairwise slopes, then median intercept."""
    slopes = [
        (y2 - y1) / (x2 - x1)
        for i, (x1, y1) in enumerate(points)
        for x2, y2 in points[i + 1:]
        if x2 != x1
    ]
    slope = statistics.median(slopes) if slopes else 0.0
    intercept = statistics.median(y - slope * x for x, y in points)
    return slope, intercept


def estimate_time_map(
    reference: list[Cue], other: list[Cue], min_overlap: float
) -> tuple[float, float, float] | None:
    """Fit a linear time map t_ref ≈ rate * t_other + offset across the file.

    Handles files that drift apart (framerate mismatch), where no constant
    offset works. Two kinds of starting hypotheses compete:

    1. Each common framerate ratio (and 1.0), paired with its best constant
       offset — this catches severe drift like a PAL speedup, where the
       timelines diverge by minutes and only the correct rate makes the
       files line up at all.
    2. A robust line through local constant offsets estimated in windows of
       `other` — this catches small drift at non-standard rates, where the
       windowed offsets are noisy but lie on the true offset-over-time line.

    The best-matching hypothesis is then refined by least squares over
    actually matched cue pairs, which widens the matched region each round.
    Returns (rate, offset_ms, match_fraction), or None when there is too
    little signal or the fitted rate is implausible.
    """
    if len(reference) < 40 or len(other) < 40:
        return None

    candidates: list[tuple[int, float, float]] = []  # (score, rate, offset)
    for cand_rate in [1.0] + [ratio for ratio, _ in COMMON_RATE_RATIOS]:
        scaled = scale_cues(other, cand_rate, 0.0)
        cand_offset, _ = estimate_offset(reference, scaled)
        score = _count_overlapping(reference, scaled, cand_offset)
        candidates.append((score, cand_rate, float(cand_offset)))

    window = max(len(other) // min(10, max(4, len(other) // 40)), 8)
    anchors: list[tuple[float, float]] = []
    for lo in range(0, len(other), window):
        chunk = other[lo:lo + window]
        if len(chunk) < 8:
            continue
        offset, support = estimate_offset(reference, chunk)
        if support >= 0.2:
            anchors.append((float(chunk[len(chunk) // 2].start), float(offset)))
    if len(anchors) >= 3:
        slope, intercept = _theil_sen(anchors)  # local offset over time
        if 0.9 <= 1.0 + slope <= 1.1:
            scaled = scale_cues(other, 1.0 + slope, intercept)
            score = _count_overlapping(reference, scaled, 0)
            candidates.append((score, 1.0 + slope, intercept))

    _, rate, offset = max(candidates)

    matched = -1
    for _ in range(6):
        mapped = scale_cues(other, rate, offset)
        mapping = align(reference, mapped, min_overlap)
        pairs = [
            (float(other[js[0]].start), float(reference[i].start))
            for i, js in mapping.items()
            if len(js) == 1
        ]
        if len(pairs) < 20 or len(pairs) == matched:
            break
        matched = len(pairs)
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        new_rate = _ols_slope(xs, ys)
        if new_rate == 0.0:
            break
        rate = new_rate
        offset = sum(ys) / len(ys) - rate * (sum(xs) / len(xs))

    if matched < 20 or not 0.9 <= rate <= 1.1:
        return None
    return rate, offset, matched / len(other)


def overlap_ms(a: Cue, b: Cue) -> int:
    return min(a.end, b.end) - max(a.start, b.start)


def align(reference: list[Cue], other: list[Cue], min_overlap: float) -> dict[int, list[int]]:
    """Map each reference-cue index to the other-file cue indexes glossing it.

    Each `other` cue is assigned to the single reference cue it overlaps most,
    provided the overlap covers at least `min_overlap` of the shorter cue.
    This avoids duplicated gloss lines when one long cue spans two others.
    """
    mapping: dict[int, list[int]] = {i: [] for i in range(len(reference))}
    for j, cue in enumerate(other):
        best_i, best_ov = None, 0
        for i, ref in enumerate(reference):
            if ref.start >= cue.end:
                break
            ov = overlap_ms(ref, cue)
            if ov > best_ov:
                best_i, best_ov = i, ov
        if best_i is None:
            continue
        shorter = min(reference[best_i].duration, cue.duration)
        if shorter > 0 and best_ov >= min_overlap * shorter:
            mapping[best_i].append(j)
    return mapping


@dataclass(frozen=True)
class Segment:
    start: int          # ms, segment window start
    end: int            # ms, segment window end
    median_residual: int | None  # None when the segment has no matched pairs
    pairs: int


@dataclass(frozen=True)
class SyncReport:
    pairs: int
    slope_ms_per_min: float
    segments: list[Segment]
    in_sync: bool
    kind: str  # "ok" | "drift" | "jump" | "gap" | "low-match"
    match_fraction: float
    verdict: str


def _ols_slope(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom


def check_sync(reference: list[Cue], other: list[Cue], min_overlap: float) -> SyncReport | None:
    """Judge whether the two files stay in sync across the whole runtime.

    For every one-to-one matched pair, the residual is how much later the
    `other` cue starts than its reference cue (after the global offset was
    applied). A least-squares slope over those residuals exposes linear drift
    (framerate mismatch); steps between consecutive segment medians expose
    sync jumps (e.g. one release containing an extra scene). A jump also
    produces a nonzero overall slope, so the drift verdict additionally
    requires the slope to hold in both halves of the file. Returns None with
    fewer than two usable pairs.
    """
    mapping = align(reference, other, min_overlap)
    samples = sorted(
        (reference[i].start, other[js[0]].start - reference[i].start)
        for i, js in mapping.items()
        if len(js) == 1
    )
    if len(samples) < 2:
        return None

    xs = [t / 60_000 for t, _ in samples]  # minutes
    ys = [float(r) for _, r in samples]
    slope = _ols_slope(xs, ys)
    mid = len(samples) // 2
    half_slopes = (_ols_slope(xs[:mid], ys[:mid]), _ols_slope(xs[mid:], ys[mid:]))
    drift_is_consistent = slope != 0 and all(h / slope >= 0.3 for h in half_slopes)

    first_seg = reference[0].start // SYNC_SEGMENT_MS
    last_seg = reference[-1].start // SYNC_SEGMENT_MS
    segments: list[Segment] = []
    for seg in range(first_seg, last_seg + 1):
        lo, hi = seg * SYNC_SEGMENT_MS, (seg + 1) * SYNC_SEGMENT_MS
        residuals = [r for t, r in samples if lo <= t < hi]
        segments.append(Segment(
            start=lo,
            end=hi,
            median_residual=int(statistics.median(residuals)) if residuals else None,
            pairs=len(residuals),
        ))

    solid = [s for s in segments if s.median_residual is not None and s.pairs >= 3]
    jump_at, jump_size = None, 0
    for a, b in zip(solid, solid[1:]):
        step = b.median_residual - a.median_residual
        if abs(step) > JUMP_MS and abs(step) > abs(jump_size):
            jump_at, jump_size = b.start, step
    gaps = [s for s in segments[1:-1] if s.pairs == 0]

    match_fraction = sum(1 for js in mapping.values() if js) / len(reference)
    drift_total = slope * (xs[-1] - xs[0])
    if match_fraction < MIN_MATCH_FRACTION:
        kind = "low-match"
        verdict = (
            f"only {match_fraction:.0%} of cues found a partner — the files "
            f"likely drift apart (framerate mismatch) or differ structurally"
        )
    elif (
        abs(slope) >= DRIFT_SLOPE_MS_PER_MIN
        and abs(drift_total) >= DRIFT_TOTAL_MS
        and drift_is_consistent
    ):
        kind = "drift"
        verdict = (
            f"linear drift of {slope:+.1f} ms/min ({drift_total / 1000:+.1f} s "
            f"across the file) — likely framerate mismatch"
        )
    elif jump_at is not None:
        kind = "jump"
        verdict = (
            f"sync jump of about {jump_size:+d} ms near {format_timestamp(jump_at)[:5]}"
        )
    elif gaps:
        kind = "gap"
        at = format_timestamp(gaps[0].start)[:5]
        verdict = (
            f"no matched cues near {at} — the releases may differ structurally there"
        )
    else:
        kind = "ok"
        verdict = f"in sync throughout (drift {slope:+.1f} ms/min)"
    return SyncReport(
        pairs=len(samples), slope_ms_per_min=slope, segments=segments,
        in_sync=kind == "ok", kind=kind, match_fraction=match_fraction,
        verdict=verdict,
    )


def format_sync_table(report: SyncReport) -> str:
    lines = ["segment      median residual   matched pairs"]
    for seg in report.segments:
        span = f"{format_timestamp(seg.start)[:5]}–{format_timestamp(seg.end)[:5]}"
        med = "—" if seg.median_residual is None else f"{seg.median_residual:+d} ms"
        lines.append(f"{span:<12} {med:>15}   {seg.pairs:>13}")
    return "\n".join(lines)


def merge(
    main: list[Cue],
    gloss: list[Cue],
    timestamps_from_main: bool,
    min_overlap: float,
) -> tuple[list[MergedCue], dict[str, int]]:
    """Merge cues: main text plus the gloss text covering the same time.

    Timestamps come from `main` or `gloss` per `timestamps_from_main`; the
    other list must already be shifted onto that timeline.
    """
    reference, other = (main, gloss) if timestamps_from_main else (gloss, main)
    mapping = align(reference, other, min_overlap)

    merged: list[MergedCue] = []
    matched_refs = 0
    used_other = set()
    for i, ref in enumerate(reference):
        partners = mapping[i]
        used_other.update(partners)
        partner_text = " ".join(
            " ".join(other[j].text.split()) for j in sorted(partners)
        )
        main_text = ref.text if timestamps_from_main else partner_text
        gloss_text = partner_text if timestamps_from_main else " ".join(ref.text.split())
        if partners:
            matched_refs += 1
        if main_text or gloss_text:
            merged.append(MergedCue(start=ref.start, end=ref.end, main=main_text, gloss=gloss_text))

    stats = {
        "reference_cues": len(reference),
        "matched": matched_refs,
        "unmatched": len(reference) - matched_refs,
        "unused_other": len(other) - len(used_other),
    }
    return merged, stats


def write_srt(cues: list[Cue]) -> str:
    blocks = []
    for n, cue in enumerate(cues, start=1):
        blocks.append(
            f"{n}\n{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}\n{cue.text}\n"
        )
    return "\n".join(blocks)


def render_srt(merged: list[MergedCue], gloss_position: str = "below") -> str:
    """Render merged cues as SRT.

    "below": gloss in italics beneath the main text, one cue block.
    "top": gloss as a second, simultaneous cue tagged {\\an8} (top-center) —
    understood by VLC, mpv, and Kodi; other players show it as a normal cue.
    SRT has no reliable font-size control; use ASS output for sizes.
    """
    blocks: list[str] = []

    def block(start: int, end: int, text: str) -> None:
        blocks.append(
            f"{len(blocks) + 1}\n"
            f"{format_timestamp(start)} --> {format_timestamp(end)}\n{text}\n"
        )

    for cue in merged:
        gloss = f"<i>{cue.gloss}</i>" if cue.gloss else ""
        if gloss_position == "top" and gloss:
            if cue.main:
                block(cue.start, cue.end, cue.main)
            block(cue.start, cue.end, "{\\an8}" + gloss)
        else:
            text = "\n".join(part for part in (cue.main, gloss) if part)
            block(cue.start, cue.end, text)
    return "\n".join(blocks)


def format_ass_timestamp(ms: int) -> str:
    ms = max(0, ms)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, milli = divmod(rem, 1000)
    return f"{h}:{m:02d}:{s:02d}.{milli // 10:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("{", "(").replace("}", ")").replace("\n", "\\N")


ASS_HEADER = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1280
PlayResY: 720
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,Arial,{main_size},&H00FFFFFF,&H000000FF,&H00101010,&H7F000000,0,0,0,0,100,100,0,0,1,2,1,2,60,60,40,1
Style: Gloss,Arial,{gloss_size},&H00D8D8D8,&H000000FF,&H00101010,&H7F000000,0,1,0,0,100,100,0,0,1,2,1,8,60,60,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def render_ass(
    merged: list[MergedCue],
    gloss_position: str = "below",
    font_size: int = 48,
    gloss_scale: float = 0.75,
) -> str:
    """Render merged cues as ASS with real font-size control.

    "below": one event per cue, gloss on its own line in a smaller italic
    font. "top": gloss as a separate event with the Gloss style, top-center.
    The gloss is slightly gray in both cases to set it apart from the main
    text. Sizes are relative to a 1280×720 canvas; players scale them.
    """
    gloss_size = max(1, round(font_size * gloss_scale))
    lines = [ASS_HEADER.format(main_size=font_size, gloss_size=gloss_size)]

    def event(cue: MergedCue, style: str, text: str) -> None:
        lines.append(
            f"Dialogue: 0,{format_ass_timestamp(cue.start)},"
            f"{format_ass_timestamp(cue.end)},{style},,0,0,0,,{text}"
        )

    inline_gloss = f"{{\\fs{gloss_size}\\i1\\c&HD8D8D8&}}"
    for cue in merged:
        main = _ass_escape(cue.main)
        gloss = _ass_escape(cue.gloss)
        if gloss_position == "top":
            if main:
                event(cue, "Main", main)
            if gloss:
                event(cue, "Gloss", gloss)
        else:
            if gloss:
                joined = f"{main}\\N{inline_gloss}{gloss}" if main else f"{inline_gloss}{gloss}"
            else:
                joined = main
            event(cue, "Main", joined)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kinogloss",
        description=(
            "Merge two SRT files into bilingual subtitles: MAIN text on top, "
            "GLOSS beneath in italics. Handles a constant time offset between "
            "the files automatically."
        ),
    )
    parser.add_argument("main", type=Path, help="SRT whose text appears on top")
    parser.add_argument("gloss", type=Path, help="SRT whose text appears beneath, in italics")
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="output path (default: <main>.gloss.srt; use '-' for stdout)",
    )
    parser.add_argument(
        "--timestamps", type=int, choices=(1, 2), default=1,
        help="which file provides the timestamps: 1=main, 2=gloss (default 1)",
    )
    parser.add_argument(
        "--offset", type=int, default=None, metavar="MS",
        help="manual offset in ms added to the non-timestamp file "
             "(default: auto-detect)",
    )
    parser.add_argument(
        "--min-overlap", type=float, default=0.3, metavar="FRACTION",
        help="minimum overlap (fraction of the shorter cue) to pair two cues "
             "(default 0.3)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="print a per-segment sync table (median residual over time)",
    )
    parser.add_argument(
        "--format", choices=("srt", "ass"), default=None,
        help="output format (default: from the output file extension, else srt); "
             "ass supports font sizes, srt is the most compatible",
    )
    parser.add_argument(
        "--gloss-position", choices=("below", "top"), default="below",
        help="where the gloss goes: below the main text (default) or "
             "top-center of the screen",
    )
    parser.add_argument(
        "--font-size", type=int, default=48, metavar="PT",
        help="main text size for ass output, on a 720p canvas (default 48)",
    )
    parser.add_argument(
        "--gloss-scale", type=float, default=0.75, metavar="FRACTION",
        help="gloss size relative to the main text, ass output only "
             "(default 0.75)",
    )
    args = parser.parse_args(argv)

    main_cues = parse_srt(read_srt_text(args.main))
    gloss_cues = parse_srt(read_srt_text(args.gloss))
    if not main_cues or not gloss_cues:
        empty = args.main if not main_cues else args.gloss
        print(f"error: no cues parsed from {empty}", file=sys.stderr)
        return 1

    timestamps_from_main = args.timestamps == 1
    reference, other_raw = (
        (main_cues, gloss_cues) if timestamps_from_main else (gloss_cues, main_cues)
    )

    def run_merge(other_cues: list[Cue]) -> tuple[list[Cue], dict[str, int]]:
        if timestamps_from_main:
            return merge(reference, other_cues, True, args.min_overlap)
        return merge(other_cues, reference, False, args.min_overlap)

    if args.offset is not None:
        offset, support = args.offset, None
    else:
        offset, support = estimate_offset(reference, other_raw)
    other = shift_cues(other_raw, offset)
    merged, stats = run_merge(other)
    report = check_sync(reference, other, args.min_overlap)

    # A constant offset can't fix drifting files; try a linear time map when
    # the sync check indicates drift, and keep it only if it matches more cues.
    correction = None
    if args.offset is None and (report is None or report.kind in ("drift", "low-match")):
        time_map = estimate_time_map(reference, other_raw, args.min_overlap)
        if time_map is not None:
            rate, lin_offset, _ = time_map
            corrected = scale_cues(other_raw, rate, lin_offset)
            merged2, stats2 = run_merge(corrected)
            if stats2["matched"] > stats["matched"]:
                merged, stats, other = merged2, stats2, corrected
                report = check_sync(reference, corrected, args.min_overlap)
                correction = (rate, lin_offset)

    to_stdout = args.output is not None and str(args.output) == "-"
    out_format = args.format
    if out_format is None:
        if not to_stdout and args.output is not None and args.output.suffix.lower() == ".ass":
            out_format = "ass"
        else:
            out_format = "srt"
    if out_format == "ass":
        output_text = render_ass(
            merged, args.gloss_position, args.font_size, args.gloss_scale
        )
    else:
        output_text = render_srt(merged, args.gloss_position)

    if to_stdout:
        sys.stdout.write(output_text)
    else:
        out_path = args.output or args.main.with_suffix(f".gloss.{out_format}")
        out_path.write_text(output_text, encoding="utf-8")
        print(f"wrote {out_path}", file=sys.stderr)

    if correction is not None:
        rate, lin_offset = correction
        fps = next(
            (name for ratio, name in COMMON_RATE_RATIOS if abs(rate - ratio) < 4e-4),
            None,
        )
        fps_note = f", ≈ {fps}" if fps else ""
        print(
            f"drift correction applied to non-timestamp file: "
            f"rate ×{rate:.6f} ({(rate - 1) * 60_000:+.0f} ms/min{fps_note}), "
            f"offset {lin_offset / 1000:+.2f} s",
            file=sys.stderr,
        )
    else:
        detected = "manual" if support is None else f"auto, {support:.0%} of cues agree"
        print(
            f"offset applied to non-timestamp file: {offset:+d} ms ({detected})",
            file=sys.stderr,
        )
    print(
        f"cues: {stats['reference_cues']} from timestamp file, "
        f"{stats['matched']} glossed, {stats['unmatched']} without gloss, "
        f"{stats['unused_other']} from the other file unused",
        file=sys.stderr,
    )

    if report is None:
        print("sync: too few matched pairs to judge", file=sys.stderr)
    else:
        marker = "" if report.in_sync else "warning: "
        print(f"{marker}sync: {report.verdict}", file=sys.stderr)
        if args.check:
            print(format_sync_table(report), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
