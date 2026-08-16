import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from walkman import cmd_album


class CmdAlbumSafetyTests(unittest.TestCase):
    """Regression tests for the 2026-08-13 accident (full-tree move)."""

    def _make_tree(self, root: Path) -> tuple[Path, Path, list[str]]:
        """source with top-level files + nested subdir files; returns (source, out, moved_expected)."""
        source = root / "source"
        output = root / "output"
        source.mkdir()
        output.mkdir()
        (source / "top.flac").write_bytes(b"top")
        (source / "top.lrc").write_text("[00:01.00]x\n", encoding="utf-8")
        nested = source / "singer"
        nested.mkdir()
        (nested / "deep.flac").write_bytes(b"deep")
        (nested / "deep.lrc").write_text("[00:01.00]y\n", encoding="utf-8")
        return source, output, ["top.flac", "top.lrc"]

    def _args(self, source: Path, output: Path, **overrides) -> Namespace:
        base = dict(
            source=source,
            output_dir=output,
            album_name="专辑",
            overwrite=False,
            dry_run=False,
            recursive=False,
            confirm=False,
            preview=5,
        )
        base.update(overrides)
        return Namespace(**base)

    def test_default_only_moves_top_level_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output, expected = self._make_tree(Path(directory))
            cmd_album(self._args(source, output))
            album_dir = output / "专辑"
            self.assertEqual(sorted(p.name for p in album_dir.iterdir()), expected)
            self.assertTrue((source / "singer/deep.flac").exists(), "nested file must not move")
            self.assertFalse((source / "top.flac").exists())

    def test_dry_run_moves_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output, _ = self._make_tree(Path(directory))
            cmd_album(self._args(source, output, dry_run=True))
            album_dir = output / "专辑"
            self.assertFalse(album_dir.exists(), "dry-run must not create album dir")
            self.assertTrue((source / "top.flac").exists())
            self.assertTrue((source / "top.lrc").exists())
            self.assertTrue((source / "singer/deep.flac").exists())

    def test_recursive_without_confirm_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output, _ = self._make_tree(Path(directory))
            code = cmd_album(self._args(source, output, recursive=True, confirm=False))
            self.assertEqual(code, 2)
            self.assertTrue((source / "top.flac").exists())
            self.assertTrue((source / "singer/deep.flac").exists())

    def test_recursive_with_confirm_moves_whole_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output, expected = self._make_tree(Path(directory))
            code = cmd_album(self._args(source, output, recursive=True, confirm=True))
            self.assertEqual(code, 0)
            album_dir = output / "专辑"
            self.assertEqual(
                sorted(p.name for p in album_dir.iterdir()),
                sorted(expected + ["deep.flac", "deep.lrc"]),
            )

    def test_album_dir_inside_source_only_absorbs_top_level_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source, _, _ = self._make_tree(Path(directory))
            # album dir lives under source itself; only top-level files are absorbed
            code = cmd_album(self._args(source, source, album_name="专辑"))
            self.assertEqual(code, 0)
            album_dir = source / "专辑"
            self.assertEqual(sorted(p.name for p in album_dir.iterdir()), ["top.flac", "top.lrc"])
            self.assertTrue((source / "singer/deep.flac").exists(), "nested file must stay")


if __name__ == "__main__":
    unittest.main()
