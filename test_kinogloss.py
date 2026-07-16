"""Tests for kinogloss. Run with: python3 -m unittest test_kinogloss -v"""

import re
import tempfile
import unittest
from pathlib import Path

import kinogloss
from kinogloss import (
    Cue,
    MergedCue,
    check_sync,
    estimate_offset,
    format_sync_table,
    format_timestamp,
    merge,
    parse_srt,
    render_ass,
    render_srt,
    shift_cues,
)

GERMAN = """\
1
00:00:01,000 --> 00:00:03,000
Wo ist der Bahnhof?

2
00:00:04,500 --> 00:00:06,000
Ich weiß es nicht.

3
00:00:08,000 --> 00:00:11,000
Fragen wir jemanden.
"""

# Same dialogue, but every cue is 2.5 s later (an offset release).
ENGLISH_OFFSET = """\
1
00:00:03,500 --> 00:00:05,500
Where is the train station?

2
00:00:07,000 --> 00:00:08,500
I don't know.

3
00:00:10,500 --> 00:00:13,500
Let's ask someone.
"""


class ParseTests(unittest.TestCase):
    def test_parses_basic_file(self):
        cues = parse_srt(GERMAN)
        self.assertEqual(len(cues), 3)
        self.assertEqual(cues[0].start, 1000)
        self.assertEqual(cues[0].end, 3000)
        self.assertEqual(cues[0].text, "Wo ist der Bahnhof?")

    def test_tolerates_crlf_missing_index_and_dot_millis(self):
        messy = "00:00:01.000 --> 00:00:02.000\r\nHallo\r\n\r\n5\r\n00:00:03,000 --> 00:00:04,000\r\nWelt\r\n"
        cues = parse_srt(messy)
        self.assertEqual([c.text for c in cues], ["Hallo", "Welt"])

    def test_multiline_cue_text_preserved(self):
        text = "1\n00:00:01,000 --> 00:00:02,000\nZeile eins\nZeile zwei\n"
        self.assertEqual(parse_srt(text)[0].text, "Zeile eins\nZeile zwei")

    def test_timestamp_roundtrip(self):
        self.assertEqual(format_timestamp(3_723_456), "01:02:03,456")
        self.assertEqual(format_timestamp(0), "00:00:00,000")


class EncodingTests(unittest.TestCase):
    def _decode(self, content: str, encoding: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub.srt"
            path.write_bytes(content.encode(encoding))
            return kinogloss.read_srt_text(path)

    SRT = "1\n00:00:01,000 --> 00:00:02,000\n{}\n"

    def test_cp1251_russian(self):
        line = "Что вы никогда его не найдёте? Хорошо, я скажу вам."
        self.assertIn(line, self._decode(self.SRT.format(line), "cp1251"))

    def test_cp1250_polish(self):
        line = "Właśnie żółtą łąkę zdążyłem ćwiczyć, część pierwsza."
        self.assertIn(line, self._decode(self.SRT.format(line), "cp1250"))

    def test_cp1252_western(self):
        line = "Fällt der Zug aus? Voilà, ça marche déjà süß."
        self.assertIn(line, self._decode(self.SRT.format(line), "cp1252"))

    def test_utf8_and_utf16_bom(self):
        line = "Смешанный текст, mixed text, ładny."
        for encoding in ("utf-8", "utf-8-sig", "utf-16"):
            self.assertIn(line, self._decode(self.SRT.format(line), encoding))


class OffsetTests(unittest.TestCase):
    def test_detects_constant_offset(self):
        ref = parse_srt(GERMAN)
        other = parse_srt(ENGLISH_OFFSET)
        offset, support = estimate_offset(ref, other)
        self.assertEqual(offset, -2500)
        self.assertEqual(support, 1.0)

    def test_detects_offset_despite_extra_cues(self):
        ref = parse_srt(GERMAN)
        other = parse_srt(ENGLISH_OFFSET) + [Cue(60_000, 62_000, "[music]")]
        offset, support = estimate_offset(ref, other)
        self.assertEqual(offset, -2500)
        self.assertLess(support, 1.0)

    def test_shift_cues(self):
        shifted = shift_cues([Cue(1000, 2000, "x")], -500)
        self.assertEqual((shifted[0].start, shifted[0].end), (500, 1500))


class MergeTests(unittest.TestCase):
    def _merged_offset_pair(self):
        ref = parse_srt(GERMAN)
        other = parse_srt(ENGLISH_OFFSET)
        offset, _ = estimate_offset(ref, other)
        return merge(ref, shift_cues(other, offset), True, 0.3)

    def test_merges_with_reference_timestamps(self):
        merged, stats = self._merged_offset_pair()
        self.assertEqual(stats["matched"], 3)
        self.assertEqual(stats["unmatched"], 0)
        first = merged[0]
        self.assertEqual((first.start, first.end), (1000, 3000))
        self.assertEqual(first.main, "Wo ist der Bahnhof?")
        self.assertEqual(first.gloss, "Where is the train station?")

    def test_many_to_one_joins_gloss_cues(self):
        ref = [Cue(0, 4000, "Ein langer Satz, der weitergeht.")]
        other = [Cue(0, 2000, "A long sentence,"), Cue(2000, 4000, "that goes on.")]
        merged, stats = merge(ref, other, True, 0.3)
        self.assertEqual(stats["matched"], 1)
        self.assertEqual(merged[0].gloss, "A long sentence, that goes on.")

    def test_unmatched_reference_cue_kept_without_gloss(self):
        ref = [Cue(0, 2000, "Hallo"), Cue(50_000, 52_000, "[Musik]")]
        other = [Cue(0, 2000, "Hello")]
        merged, stats = merge(ref, other, True, 0.3)
        self.assertEqual(stats["unmatched"], 1)
        self.assertEqual(merged[1].main, "[Musik]")
        self.assertEqual(merged[1].gloss, "")

    def test_timestamps_from_gloss_file(self):
        # Timeline comes from the gloss file; main text still ends up on top.
        main = [Cue(1000, 3000, "Wo ist der Bahnhof?")]
        gloss = [Cue(1100, 3100, "Where is the train station?")]
        merged, _ = merge(main, gloss, False, 0.3)
        self.assertEqual((merged[0].start, merged[0].end), (1100, 3100))
        self.assertEqual(merged[0].main, "Wo ist der Bahnhof?")
        self.assertEqual(merged[0].gloss, "Where is the train station?")


class FragmentFillTests(unittest.TestCase):
    def test_ends_sentence_heuristics(self):
        ends = kinogloss._ends_sentence
        self.assertTrue(ends("Wo ist der Bahnhof?"))
        self.assertTrue(ends("[Musik]"))
        self.assertTrue(ends('"Genau."'))
        self.assertTrue(ends("Ende.</i>"))
        self.assertFalse(ends("Ich habe gestern"))
        self.assertFalse(ends("Warte mal..."))
        self.assertFalse(ends("Warte mal…"))

    def test_starts_mid_sentence_heuristics(self):
        starts = kinogloss._starts_mid_sentence
        self.assertTrue(starts("den ganzen Tag"))
        self.assertTrue(starts("...und dann?"))
        self.assertFalse(starts("Hallo!"))
        self.assertFalse(starts("- Nein."))
        self.assertFalse(starts("<i>Ja.</i>"))

    def test_middle_fragment_borrows_overlapping_gloss(self):
        # One sentence: three cues in the reference, two in the gloss file.
        # The middle reference cue overlaps no gloss cue best, but borrows
        # both that cover it.
        ref = [
            Cue(0, 2000, "Ich habe gestern"),
            Cue(2000, 4000, "den ganzen Tag"),
            Cue(4000, 6000, "gearbeitet."),
        ]
        other = [
            Cue(0, 3000, "Yesterday I worked"),
            Cue(3000, 6000, "the whole day."),
        ]
        merged, stats = merge(ref, other, True, 0.3)
        self.assertEqual(stats["matched"], 2)
        self.assertEqual(stats["filled"], 1)
        self.assertEqual(stats["unmatched"], 0)
        self.assertEqual(merged[1].gloss, "Yesterday I worked the whole day.")

    def test_standalone_cue_is_not_filled(self):
        # The interjection is a complete utterance the gloss file skipped;
        # it overlaps the long gloss cue but must not borrow its text.
        ref = [Cue(0, 2000, "Hallo."), Cue(2050, 2500, "Hm.")]
        other = [Cue(0, 2500, "Hello.")]
        merged, stats = merge(ref, other, True, 0.3)
        self.assertEqual(stats["filled"], 0)
        self.assertEqual(stats["unmatched"], 1)
        self.assertEqual(merged[1].gloss, "")

    def test_fill_requires_time_overlap(self):
        # A fragment with no gloss cue anywhere near it stays bare.
        ref = [Cue(0, 2000, "Ich habe gestern"), Cue(50_000, 52_000, "gearbeitet.")]
        other = [Cue(0, 2000, "Yesterday I worked")]
        merged, stats = merge(ref, other, True, 0.3)
        self.assertEqual(stats["filled"], 0)
        self.assertEqual(merged[1].gloss, "")


class RenderTests(unittest.TestCase):
    MERGED = [
        MergedCue(1000, 3000, "Wo ist der Bahnhof?", "Where is the train station?"),
        MergedCue(4000, 6000, "[Musik]", ""),
    ]

    def test_srt_below_stacks_italic_gloss(self):
        out = render_srt(self.MERGED)
        self.assertIn(
            "1\n00:00:01,000 --> 00:00:03,000\n"
            "Wo ist der Bahnhof?\n<i>Where is the train station?</i>\n",
            out,
        )
        self.assertIn("2\n00:00:04,000 --> 00:00:06,000\n[Musik]\n", out)

    def test_srt_top_emits_simultaneous_an8_cue(self):
        out = render_srt(self.MERGED, gloss_position="top")
        blocks = out.strip().split("\n\n")
        self.assertEqual(len(blocks), 3)  # main, gloss, [Musik]
        self.assertIn("Wo ist der Bahnhof?", blocks[0])
        self.assertNotIn("<i>", blocks[0])
        self.assertIn("{\\an8}<i>Where is the train station?</i>", blocks[1])
        # Both cues cover the same time span, numbering stays sequential.
        self.assertTrue(blocks[1].startswith("2\n00:00:01,000 --> 00:00:03,000"))

    def test_ass_below_uses_inline_smaller_italic_gloss(self):
        out = render_ass(self.MERGED, font_size=48, gloss_scale=0.75)
        self.assertIn("Style: Main,Arial,48,", out)
        self.assertIn("Style: Gloss,Arial,36,", out)
        self.assertIn(
            "Dialogue: 0,0:00:01.00,0:00:03.00,Main,,0,0,0,,"
            "Wo ist der Bahnhof?\\N{\\fs36\\i1\\c&HD8D8D8&}Where is the train station?",
            out,
        )

    def test_ass_top_uses_separate_gloss_style_event(self):
        out = render_ass(self.MERGED, gloss_position="top")
        self.assertIn("Dialogue: 0,0:00:01.00,0:00:03.00,Main,,0,0,0,,Wo ist der Bahnhof?", out)
        self.assertIn(
            "Dialogue: 0,0:00:01.00,0:00:03.00,Gloss,,0,0,0,,Where is the train station?",
            out,
        )

    FILLED = [
        MergedCue(0, 2000, "Ich habe gestern", "Yesterday I worked the whole day."),
        MergedCue(2100, 4000, "den ganzen Tag", "Yesterday I worked the whole day."),
        MergedCue(4100, 6000, "gearbeitet.", "Yesterday I worked the whole day."),
    ]

    def test_ass_top_merges_duplicate_glosses_into_spanning_event(self):
        out = render_ass(self.FILLED, gloss_position="top")
        self.assertEqual(out.count("Dialogue:"), 4)  # 3 main + 1 gloss span
        self.assertIn("Dialogue: 0,0:00:00.00,0:00:06.00,Gloss,", out)

    def test_srt_top_merges_duplicate_glosses_into_spanning_cue(self):
        out = render_srt(self.FILLED, gloss_position="top")
        self.assertEqual(out.count("{\\an8}"), 1)
        self.assertIn(
            "00:00:00,000 --> 00:00:06,000\n"
            "{\\an8}<i>Yesterday I worked the whole day.</i>",
            out,
        )

    def test_below_mode_keeps_duplicate_glosses_per_cue(self):
        out = render_ass(self.FILLED)
        self.assertEqual(out.count("Yesterday I worked"), 3)

    def test_distant_repeats_are_not_merged(self):
        merged = [
            MergedCue(0, 2000, "Вау.", "Wow."),
            MergedCue(30_000, 32_000, "Вау.", "Wow."),
        ]
        out = render_ass(merged, gloss_position="top")
        self.assertEqual(out.count(",Gloss,"), 2)

    def test_ass_escapes_braces_and_newlines(self):
        merged = [MergedCue(0, 1000, "Zeile eins\nZeile {zwei}", "")]
        out = render_ass(merged)
        self.assertIn("Zeile eins\\NZeile (zwei)", out)


class SyncCheckTests(unittest.TestCase):
    @staticmethod
    def _pair(residual_fn, n=200, gap=30_000, dur=2500):
        """Reference cues every `gap` ms; other cues shifted by residual_fn(minutes)."""
        ref = [Cue(i * gap, i * gap + dur, f"r{i}") for i in range(n)]
        other = [
            Cue(
                i * gap + int(residual_fn(i * gap / 60_000)),
                i * gap + dur + int(residual_fn(i * gap / 60_000)),
                f"o{i}",
            )
            for i in range(n)
        ]
        return ref, other

    def test_in_sync_files_get_clean_verdict(self):
        ref, other = self._pair(lambda m: 0)
        report = check_sync(ref, other, 0.3)
        self.assertTrue(report.in_sync)
        self.assertIn("in sync", report.verdict)
        self.assertAlmostEqual(report.slope_ms_per_min, 0.0, delta=0.5)

    def test_jitter_does_not_trigger_warnings(self):
        ref, other = self._pair(lambda m: (int(m * 2) % 3 - 1) * 60)
        report = check_sync(ref, other, 0.3)
        self.assertTrue(report.in_sync)

    def test_linear_drift_detected_with_slope(self):
        ref, other = self._pair(lambda m: 10 * m)  # 10 ms per minute
        report = check_sync(ref, other, 0.3)
        self.assertFalse(report.in_sync)
        self.assertIn("drift", report.verdict)
        self.assertAlmostEqual(report.slope_ms_per_min, 10.0, delta=1.0)

    def test_sync_jump_not_misreported_as_drift(self):
        ref, other = self._pair(lambda m: 0 if m < 50 else 900)
        report = check_sync(ref, other, 0.3)
        self.assertFalse(report.in_sync)
        self.assertIn("jump", report.verdict)
        self.assertIn("+900", report.verdict)
        self.assertNotIn("drift of", report.verdict)

    def test_unmatched_stretch_reported(self):
        ref = [Cue(i * 30_000, i * 30_000 + 2500, f"r{i}") for i in range(200)]
        other = [
            Cue(i * 30_000, i * 30_000 + 2500, f"o{i}")
            for i in range(200)
            if not 80 <= i < 120  # minutes 40–60 exist only in the reference
        ]
        report = check_sync(ref, other, 0.3)
        self.assertFalse(report.in_sync)
        self.assertIn("no matched cues", report.verdict)
        self.assertIn("00:40", report.verdict)

    def test_segment_table_has_row_per_segment(self):
        ref, other = self._pair(lambda m: 0)
        report = check_sync(ref, other, 0.3)
        table = format_sync_table(report)
        self.assertEqual(len(table.splitlines()), len(report.segments) + 1)
        self.assertIn("00:00–00:10", table)

    def test_low_match_rate_overrides_residual_analysis(self):
        # Only half the reference cues have a partner; the pairs that do
        # exist look clean, but the verdict must report the poor match rate.
        ref = [Cue(i * 30_000, i * 30_000 + 2500, f"r{i}") for i in range(200)]
        other = [Cue(i * 30_000, i * 30_000 + 2500, f"o{i}") for i in range(100)]
        report = check_sync(ref, other, 0.3)
        self.assertFalse(report.in_sync)
        self.assertIn("50% of cues found a partner", report.verdict)

    def test_too_few_pairs_returns_none(self):
        self.assertIsNone(check_sync([Cue(0, 1000, "a")], [], 0.3))


class DriftCorrectionTests(unittest.TestCase):
    @staticmethod
    def _drifting_pair(rate=1.0043, offset=3000, n=600):
        """Reference runs `rate` times faster than `other`: t_ref = rate*t + offset."""
        ref, other = [], []
        t = 5000
        for i in range(n):
            dur = 1500 + (i * 911) % 2000
            other.append(Cue(t, t + dur, f"Sentence {i}"))
            ref.append(
                Cue(round(t * rate + offset), round((t + dur) * rate + offset), f"Satz {i}")
            )
            t += dur + 800 + (i * 2617) % 6000
        return ref, other

    def test_estimate_time_map_recovers_rate_and_offset(self):
        ref, other = self._drifting_pair()
        result = kinogloss.estimate_time_map(ref, other, 0.3)
        self.assertIsNotNone(result)
        rate, offset, match_fraction = result
        self.assertAlmostEqual(rate, 1.0043, delta=3e-4)
        self.assertAlmostEqual(offset, 3000, delta=400)
        self.assertGreater(match_fraction, 0.9)

    def test_recovers_pal_speedup(self):
        # 25 → 23.976 fps: timelines diverge by minutes by the end of a film,
        # far beyond the constant-offset search window.
        true_rate = 23.976 / 25
        ref, other = self._drifting_pair(rate=1 / true_rate, offset=-8000)
        result = kinogloss.estimate_time_map(ref, other, 0.3)
        self.assertIsNotNone(result)
        rate, offset, match_fraction = result
        self.assertAlmostEqual(rate, 1 / true_rate, delta=3e-4)
        self.assertGreater(match_fraction, 0.9)

    def test_returns_none_for_tiny_input(self):
        ref, other = self._drifting_pair(n=20)
        self.assertIsNone(kinogloss.estimate_time_map(ref, other, 0.3))

    def test_cli_corrects_drift_and_pairs_correctly(self):
        import contextlib
        import io

        ref, other = self._drifting_pair()
        with tempfile.TemporaryDirectory() as tmp:
            de = Path(tmp) / "de.srt"
            en = Path(tmp) / "en.srt"
            de.write_text(kinogloss.write_srt(ref), encoding="utf-8")
            en.write_text(kinogloss.write_srt(other), encoding="utf-8")
            out = Path(tmp) / "out.srt"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = kinogloss.main([str(de), str(en), "-o", str(out)])
            self.assertEqual(code, 0)
            self.assertIn("drift correction applied", stderr.getvalue())
            self.assertIn("sync: in sync throughout", stderr.getvalue())

            merged = parse_srt(out.read_text(encoding="utf-8"))
            glossed = mispaired = 0
            for cue in merged:
                m = re.match(r"Satz (\d+)\n<i>Sentence (\d+)</i>", cue.text)
                if m:
                    glossed += 1
                    if m.group(1) != m.group(2):
                        mispaired += 1
            self.assertEqual(mispaired, 0)
            self.assertGreater(glossed / len(merged), 0.97)

    def test_in_sync_files_do_not_trigger_correction(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            de = Path(tmp) / "de.srt"
            en = Path(tmp) / "en.srt"
            de.write_text(GERMAN, encoding="utf-8")
            en.write_text(ENGLISH_OFFSET, encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                kinogloss.main([str(de), str(en), "-o", str(Path(tmp) / "out.srt")])
            self.assertNotIn("drift correction", stderr.getvalue())
            self.assertIn("offset applied", stderr.getvalue())


class JumpCorrectionTests(unittest.TestCase):
    JUMPS = ((0, -1800), (60, 1500), (200, 3800))  # (first cue index, offset)

    @classmethod
    def _jumpy_pair(cls, n=300):
        """Reference shifted by a different constant in each stretch, as with
        releases whose commercial breaks sit at different points."""
        ref, other = [], []
        t = 5000
        for i in range(n):
            offset = next(off for lo, off in reversed(cls.JUMPS) if i >= lo)
            dur = 1500 + (i * 911) % 2000
            other.append(Cue(t, t + dur, f"Sentence {i}"))
            ref.append(Cue(t + offset, t + dur + offset, f"Satz {i}"))
            t += dur + 800 + (i * 2617) % 6000
        return ref, other

    def test_estimate_piecewise_recovers_segments(self):
        ref, other = self._jumpy_pair()
        segments = kinogloss.estimate_piecewise_offsets(ref, other)
        self.assertIsNotNone(segments)
        self.assertEqual(len(segments), len(self.JUMPS))
        for (seg_lo, seg_hi, seg_offset), (true_lo, true_offset) in zip(
            segments, self.JUMPS
        ):
            self.assertLessEqual(abs(seg_lo - true_lo), 2)
            self.assertLessEqual(abs(seg_offset - true_offset), 150)
        self.assertEqual(segments[-1][1], len(other))

    def test_tiny_rogue_segment_is_dissolved(self):
        # Five cues only one translator subtitled: their reference partners
        # sit at a +9000 offset no real segment uses. The rogue offset is
        # injected as a candidate; the assignment must not keep a 5-cue
        # segment for it.
        ref, other = self._jumpy_pair()
        for i in range(100, 105):
            ref[i] = Cue(other[i].start + 9000, other[i].end + 9000, ref[i].text)
        ref.sort(key=lambda c: c.start)
        original = kinogloss._offset_candidates
        kinogloss._offset_candidates = lambda r, o: original(r, o) + [9000]
        try:
            segments = kinogloss.estimate_piecewise_offsets(ref, other)
        finally:
            kinogloss._offset_candidates = original
        self.assertIsNotNone(segments)
        self.assertTrue(
            all(
                hi - lo >= kinogloss.PIECEWISE_MIN_SEGMENT_CUES
                for lo, hi, _ in segments
            ),
            segments,
        )
        self.assertNotIn(9000, {offset for _, _, offset in segments})

    def test_constant_offset_yields_no_segments(self):
        ref, other = self._jumpy_pair()
        shifted = shift_cues(other, 2500)
        self.assertIsNone(kinogloss.estimate_piecewise_offsets(other, shifted))

    def test_cli_corrects_jumps_and_pairs_correctly(self):
        import contextlib
        import io

        ref, other = self._jumpy_pair()
        with tempfile.TemporaryDirectory() as tmp:
            de = Path(tmp) / "de.srt"
            en = Path(tmp) / "en.srt"
            de.write_text(kinogloss.write_srt(ref), encoding="utf-8")
            en.write_text(kinogloss.write_srt(other), encoding="utf-8")
            out = Path(tmp) / "out.srt"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = kinogloss.main([str(de), str(en), "-o", str(out)])
            self.assertEqual(code, 0)
            self.assertIn("sync-jump correction applied", stderr.getvalue())

            merged = parse_srt(out.read_text(encoding="utf-8"))
            glossed = mispaired = 0
            for cue in merged:
                m = re.match(r"Satz (\d+)\n<i>Sentence (\d+)</i>", cue.text)
                if m:
                    glossed += 1
                    if m.group(1) != m.group(2):
                        mispaired += 1
            self.assertEqual(mispaired, 0)
            self.assertGreater(glossed / len(merged), 0.95)


class CliTests(unittest.TestCase):
    def _write(self, directory, name, content, encoding="utf-8"):
        path = Path(directory) / name
        path.write_bytes(content.encode(encoding))
        return path

    def test_end_to_end_with_offset_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            de = self._write(tmp, "de.srt", GERMAN)
            en = self._write(tmp, "en.srt", ENGLISH_OFFSET, encoding="utf-8-sig")
            out = Path(tmp) / "out.srt"
            code = kinogloss.main([str(de), str(en), "-o", str(out)])
            self.assertEqual(code, 0)
            result = parse_srt(out.read_text(encoding="utf-8"))
            self.assertEqual(len(result), 3)
            # Timestamps from file 1 (German), gloss aligned despite 2.5 s offset.
            self.assertEqual(result[0].start, 1000)
            self.assertIn("<i>Where is the train station?</i>", result[0].text)
            self.assertIn("<i>Let's ask someone.</i>", result[2].text)

    def test_timestamps_flag_uses_gloss_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            de = self._write(tmp, "de.srt", GERMAN)
            en = self._write(tmp, "en.srt", ENGLISH_OFFSET)
            out = Path(tmp) / "out.srt"
            code = kinogloss.main([str(de), str(en), "-o", str(out), "--timestamps", "2"])
            self.assertEqual(code, 0)
            result = parse_srt(out.read_text(encoding="utf-8"))
            self.assertEqual(result[0].start, 3500)  # English timeline kept
            self.assertEqual(
                result[0].text,
                "Wo ist der Bahnhof?\n<i>Where is the train station?</i>",
            )

    def test_manual_offset_and_default_output_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            de = self._write(tmp, "film.srt", GERMAN)
            en = self._write(tmp, "en.srt", ENGLISH_OFFSET)
            code = kinogloss.main([str(de), str(en), "--offset", "-2500"])
            self.assertEqual(code, 0)
            out = Path(tmp) / "film.gloss.srt"
            self.assertTrue(out.exists())
            result = parse_srt(out.read_text(encoding="utf-8"))
            self.assertIn("<i>I don't know.</i>", result[1].text)

    def test_check_flag_prints_sync_table(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            de = self._write(tmp, "de.srt", GERMAN)
            en = self._write(tmp, "en.srt", ENGLISH_OFFSET)
            out = Path(tmp) / "out.srt"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = kinogloss.main([str(de), str(en), "-o", str(out), "--check"])
            self.assertEqual(code, 0)
            self.assertIn("sync: in sync throughout", stderr.getvalue())
            self.assertIn("median residual", stderr.getvalue())

    def test_ass_output_inferred_from_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            de = self._write(tmp, "de.srt", GERMAN)
            en = self._write(tmp, "en.srt", ENGLISH_OFFSET)
            out = Path(tmp) / "out.ass"
            code = kinogloss.main([str(de), str(en), "-o", str(out), "--gloss-scale", "0.6"])
            self.assertEqual(code, 0)
            content = out.read_text(encoding="utf-8")
            self.assertTrue(content.startswith("[Script Info]"))
            self.assertIn("Style: Gloss,Arial,29,", content)  # 48 * 0.6 rounded
            self.assertIn("Where is the train station?", content)

    def test_gloss_position_top_in_srt(self):
        with tempfile.TemporaryDirectory() as tmp:
            de = self._write(tmp, "de.srt", GERMAN)
            en = self._write(tmp, "en.srt", ENGLISH_OFFSET)
            out = Path(tmp) / "out.srt"
            code = kinogloss.main([str(de), str(en), "-o", str(out), "--gloss-position", "top"])
            self.assertEqual(code, 0)
            cues = parse_srt(out.read_text(encoding="utf-8"))
            self.assertEqual(len(cues), 6)  # 3 dialogue + 3 top gloss cues
            self.assertIn("{\\an8}<i>Where is the train station?</i>", cues[1].text)

    def test_default_output_name_follows_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            de = self._write(tmp, "film.srt", GERMAN)
            en = self._write(tmp, "en.srt", ENGLISH_OFFSET)
            code = kinogloss.main([str(de), str(en), "--format", "ass"])
            self.assertEqual(code, 0)
            self.assertTrue((Path(tmp) / "film.gloss.ass").exists())

    def test_empty_input_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            de = self._write(tmp, "de.srt", GERMAN)
            empty = self._write(tmp, "empty.srt", "not a subtitle\n")
            self.assertEqual(kinogloss.main([str(de), str(empty)]), 1)


if __name__ == "__main__":
    unittest.main()
