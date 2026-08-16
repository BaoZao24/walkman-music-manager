import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from bilibili_download import (
    VIDEO_URL_RE,
    cmd_bili_copy,
    cmd_bili_rename,
    read_bvids,
)


def make_args(**overrides) -> Namespace:
    base = dict(
        source=Path("."), dest=Path("."), dry_run=False,
        translations=None,
    )
    base.update(overrides)
    return Namespace(**base)


class BilibiliHelpersTests(unittest.TestCase):
    def test_read_bvids_from_inline_and_file(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "ids.txt"
            input_path.write_text(
                "# comment\nhttps://www.bilibili.com/video/BV1xx411c7mD\n"
                "BV1hD421p7Vy\n\nBV1hD421p7Vy\n",
                encoding="utf-8",
            )
            ids = read_bvids(input_path, ["BV1aabbccdde"])
            self.assertEqual(ids, ["BV1aabbccdde", "BV1xx411c7mD", "BV1hD421p7Vy"])

    def test_read_bvids_rejects_garbage(self):
        with self.assertRaises(ValueError):
            read_bvids(None, ["not-a-bv-id"])

    def test_video_url_regex(self):
        self.assertIsNotNone(VIDEO_URL_RE.search("https://www.bilibili.com/video/BV1hD421p7Vy?p=2"))
        self.assertIsNone(VIDEO_URL_RE.search("hello world"))


class BilibiliRenameTests(unittest.TestCase):
    def _tree(self, directory: Path) -> Path:
        root = Path(directory) / "covers"
        root.mkdir()
        (root / "墨流し [BV1hD421p7Vy].mp3").write_bytes(b"a")
        (root / "Bright [BV17tyHYgELa].flac").write_bytes(b"b")
        return root

    def test_rename_strips_bv_and_applies_translations_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._tree(Path(directory))
            args = make_args(source=root, dry_run=True)
            code = cmd_bili_rename(args)
            self.assertEqual(code, 0)
            self.assertTrue((root / "墨流し [BV1hD421p7Vy].mp3").exists(), "dry-run must not rename")
            self.assertTrue((root / "Bright [BV17tyHYgELa].flac").exists())

    def test_rename_applies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._tree(Path(directory))
            trans = Path(directory) / "trans.json"
            trans.write_text('{"BV17tyHYgELa": "明亮"}', encoding="utf-8")
            code = cmd_bili_rename(make_args(source=root, translations=trans))
            self.assertEqual(code, 0)
            self.assertTrue((root / "墨流し.mp3").exists(), "BV suffix must be stripped")
            self.assertTrue((root / "明亮.flac").exists(), "translation must be applied")
            self.assertFalse((root / "Bright [BV17tyHYgELa].flac").exists())

    def test_rename_duplicate_gets_number_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "covers"
            root.mkdir()
            (root / "A [BV1].mp3").write_bytes(b"a")
            (root / "A [BV2].mp3").write_bytes(b"b")
            code = cmd_bili_rename(make_args(source=root))
            self.assertEqual(code, 0)
            names = sorted(p.name for p in root.iterdir() if p.is_file())
            self.assertEqual(names, ["A (2).mp3", "A.mp3"])


class BilibiliCopyTests(unittest.TestCase):
    def test_copy_skips_existing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            src, dst = root / "src", root / "dst"
            src.mkdir(); dst.mkdir()
            (src / "a.mp3").write_bytes(b"a")
            (dst / "a.mp3").write_bytes(b"old")
            (src / "b.flac").write_bytes(b"b")
            code = cmd_bili_copy(make_args(source=src, dest=dst))
            self.assertEqual(code, 0)
            self.assertEqual((dst / "a.mp3").read_bytes(), b"old", "existing must not be overwritten")
            self.assertTrue((dst / "b.flac").exists())

    def test_copy_dry_run_copies_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            src, dst = root / "src", root / "dst"
            src.mkdir(); dst.mkdir()
            (src / "a.mp3").write_bytes(b"a")
            code = cmd_bili_copy(make_args(source=src, dest=dst, dry_run=True))
            self.assertEqual(code, 0)
            self.assertFalse((dst / "a.mp3").exists())


if __name__ == "__main__":
    unittest.main()
