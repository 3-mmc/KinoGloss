"""Tests for the Bazarr hook. Run with: python3 -m unittest test_bazarr_hook -v"""

import tempfile
import unittest
from pathlib import Path

import bazarr_hook

RUSSIAN = """\
1
00:00:01,000 --> 00:00:03,000
Где вокзал?

2
00:00:04,500 --> 00:00:06,000
Я не знаю.

3
00:00:07,000 --> 00:00:09,000
Спасибо.
"""

ENGLISH = """\
1
00:00:01,200 --> 00:00:03,100
Where is the station?

2
00:00:04,700 --> 00:00:06,100
I don't know.

3
00:00:07,200 --> 00:00:09,100
Thank you.
"""


class HookTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self._log = bazarr_hook.LOG
        bazarr_hook.LOG = self.dir / "hook.log"
        self.cfg = dict(bazarr_hook.DEFAULTS)
        self.video = self.dir / "Film (2000).mkv"
        self.video.write_bytes(b"")  # no streams: ffprobe finds nothing embedded

    def tearDown(self):
        bazarr_hook.LOG = self._log
        self._tmp.cleanup()

    def write(self, name, text):
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_unquote_strips_bazarr_quotes(self):
        self.assertEqual(bazarr_hook.unquote('"D:\\Kino\\Film (2000).mkv"'), "D:\\Kino\\Film (2000).mkv")
        self.assertEqual(bazarr_hook.unquote("ru"), "ru")

    def test_study_subtitle_is_glossed_with_english_sidecar(self):
        ru = self.write("Film (2000).ru.srt", RUSSIAN)
        self.write("Film (2000).en.srt", ENGLISH)
        result = bazarr_hook.handle(self.video, ru, "ru", self.cfg)
        self.assertEqual(result, ["wrote Film (2000).ru.ass"])
        ass = (self.dir / "Film (2000).ru.ass").read_text(encoding="utf-8")
        self.assertIn(bazarr_hook.MARKER, ass)
        self.assertIn("Где вокзал?", ass)
        self.assertIn("Where is the station?", ass)

    def test_english_arrival_glosses_existing_study_sidecar(self):
        self.write("Film (2000).ru.srt", RUSSIAN)
        en = self.write("Film (2000).en.srt", ENGLISH)
        result = bazarr_hook.handle(self.video, en, "en", self.cfg)
        self.assertEqual(result, ["wrote Film (2000).ru.ass"])

    def test_forced_english_is_not_a_gloss(self):
        ru = self.write("Film (2000).ru.srt", RUSSIAN)
        self.write("Film (2000).en.forced.srt", ENGLISH)
        result = bazarr_hook.handle(self.video, ru, "ru", self.cfg)
        self.assertIn("no en subtitle", result[0])

    def test_hand_made_ass_is_never_overwritten(self):
        ru = self.write("Film (2000).ru.srt", RUSSIAN)
        self.write("Film (2000).en.srt", ENGLISH)
        self.write("Film (2000).ru.ass", "[Script Info]\nTitle: mine\n")
        result = bazarr_hook.handle(self.video, ru, "ru", self.cfg)
        self.assertIn("not written by KinoGloss", result[0])
        self.assertIn("Title: mine", (self.dir / "Film (2000).ru.ass").read_text(encoding="utf-8"))

    def test_own_output_is_regenerated(self):
        ru = self.write("Film (2000).ru.srt", RUSSIAN)
        self.write("Film (2000).en.srt", ENGLISH)
        bazarr_hook.handle(self.video, ru, "ru", self.cfg)
        result = bazarr_hook.handle(self.video, ru, "ru", self.cfg)
        self.assertEqual(result, ["wrote Film (2000).ru.ass"])

    def test_non_srt_download_is_skipped(self):
        ass = self.write("Film (2000).ru.ass", "[Script Info]\n")
        result = bazarr_hook.handle(self.video, ass, "ru", self.cfg)
        self.assertIn("SRT only", result[0])

    def test_unrelated_language_does_nothing(self):
        fr = self.write("Film (2000).fr.srt", ENGLISH)
        self.assertIn("nothing to do", bazarr_hook.handle(self.video, fr, "fr", self.cfg)[0])


if __name__ == "__main__":
    unittest.main()
