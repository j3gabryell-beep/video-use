import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HELPERS = Path(__file__).parents[1] / "helpers"
SPEC = importlib.util.spec_from_file_location("video_use_auto_cut", HELPERS / "auto_cut.py")
assert SPEC and SPEC.loader
auto_cut = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(auto_cut)


def w(i, text, start, end):
    return {"i": i, "text": text, "start": start, "end": end, "n": auto_cut.norm(text)}


def words_of(spec):
    return [w(i, t, a, b) for i, (t, a, b) in enumerate(spec)]


# Local-ASR timings of a Portuguese take with a false start and a long pause.
TAKE = words_of([
    ("No", 5.68, 6.00), ("primeiro", 6.00, 6.48), ("mês,", 6.48, 6.80), ("a", 6.96, 7.12),
    ("gente", 7.12, 7.44), ("teve.", 7.44, 7.80),
    ("No", 8.88, 9.18), ("primeiro", 9.20, 9.68), ("mês,", 9.68, 10.00), ("a", 10.16, 10.32),
    ("gente", 10.32, 10.64), ("teve", 10.64, 10.96), ("um", 10.96, 11.12),
    ("crescimento", 11.12, 11.68), ("de", 11.68, 11.90), ("40%.", 11.90, 12.48),
    ("E", 14.72, 14.96), ("o", 14.96, 15.20), ("segredo", 15.20, 15.76),
])


class RetakeTest(unittest.TestCase):
    def test_false_start_is_dropped_and_last_take_kept(self):
        phrases = auto_cut.split_phrases(TAKE, 0.5)
        dropped = auto_cut.find_retakes(phrases, lookahead=3, window_s=20, min_ratio=0.75)
        self.assertEqual(len(dropped), 1)
        take, kept, _ = dropped[0]
        self.assertEqual(auto_cut.text_of(take), "No primeiro mês, a gente teve.")
        self.assertTrue(auto_cut.text_of(kept).startswith("No primeiro mês, a gente teve um"))

    def test_distinct_list_items_are_not_retakes(self):
        items = words_of([("Primeiro,", 0.0, 0.5), ("conteúdo", 0.6, 1.0), ("todo", 1.0, 1.2), ("dia.", 1.2, 1.5),
                          ("Segundo,", 2.3, 2.8), ("responder", 2.9, 3.3), ("todo", 3.3, 3.5), ("mundo.", 3.5, 3.8)])
        self.assertEqual(auto_cut.find_retakes(auto_cut.split_phrases(items, 0.5), 3, 20, 0.75), [])


class StutterTest(unittest.TestCase):
    def test_repeated_bigram_drops_first_copy(self):
        p = words_of([("a", 0.0, 0.1), ("gente", 0.1, 0.4), ("a", 0.5, 0.6), ("gente", 0.6, 0.9), ("teve", 0.9, 1.2)])
        drop, singles = auto_cut.find_stutters(p)
        self.assertEqual([x["i"] for x in drop], [0, 1])
        self.assertEqual(singles, [])

    def test_repeated_single_word_is_only_reported(self):
        p = words_of([("muito", 0.0, 0.3), ("muito", 0.35, 0.7), ("bom", 0.7, 1.0)])
        drop, singles = auto_cut.find_stutters(p)
        self.assertEqual(drop, [])
        self.assertEqual(len(singles), 1)


class RangeTest(unittest.TestCase):
    def test_long_pause_splits_and_pads_stay_inside_the_gap(self):
        keep = {x["i"] for x in TAKE[6:]}
        ranges = auto_cut.build_ranges(TAKE, keep, max_gap=0.45, pad_before=0.08, pad_after=0.12, total=None)
        self.assertEqual(len(ranges), 2)
        first, second = ranges
        # Starts after the dropped false start: pad limited to half the 1.08s gap.
        self.assertAlmostEqual(first["start"], 8.80)
        self.assertAlmostEqual(first["end"], 12.60)
        self.assertAlmostEqual(second["start"], 14.64)

    def test_pad_never_reaches_into_a_dropped_neighbour(self):
        spec = words_of([("ahn", 0.0, 0.30), ("Oi", 0.34, 0.60)])
        ranges = auto_cut.build_ranges(spec, {1}, max_gap=0.45, pad_before=0.08, pad_after=0.12, total=None)
        self.assertGreaterEqual(ranges[0]["start"], 0.30)


class CliTest(unittest.TestCase):
    def test_portuguese_um_is_an_article_not_a_filler(self):
        with tempfile.TemporaryDirectory() as tmp:
            edit = Path(tmp) / "edit"
            (edit / "transcripts").mkdir(parents=True)
            scribe = [{"type": "word", "text": x["text"], "start": x["start"], "end": x["end"]} for x in TAKE]
            (edit / "transcripts" / "take.json").write_text(json.dumps({"words": scribe}))
            subprocess.run([sys.executable, str(HELPERS / "auto_cut.py"), "--edit-dir", str(edit),
                            str(Path(tmp) / "take.mp4")], check=True, capture_output=True)
            edl = json.loads((edit / "edl.json").read_text())
            quotes = " ".join(r["quote"] for r in edl["ranges"])
            self.assertIn("teve um crescimento", quotes)
            self.assertEqual(quotes.count("No primeiro mês"), 1)


if __name__ == "__main__":
    unittest.main()
