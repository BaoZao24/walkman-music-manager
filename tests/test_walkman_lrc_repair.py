import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from download_music import merge_bilingual_lyrics, safe_filename, select_lyrics
from lrc_translate import japanese_lines, translate_lrc
from process_music import convert_ncm, iter_ncm_files, matching_lrc
from search_music import format_duration, format_track
from walkman_lrc_repair import iter_lrc_files, transform


class WalkmanLrcRepairTests(unittest.TestCase):
    def test_transform_removes_metadata_and_normalizes_timestamps(self):
        source = (
            "[ar:demo]\n"
            "{\"t\":123,\"c\":\"metadata\"}\n"
            "[0:01.2]第一行  \n"
            "[00:02:123]第二行\n"
        ).encode("utf-8")

        repaired, result = transform(source, Path("demo.lrc"))

        self.assertEqual(
            repaired.decode("utf-8"), "[00:01.20]第一行\n[00:02.12]第二行\n"
        )
        self.assertEqual(result.action, "would-repair")
        self.assertEqual(result.json_lines_removed, 1)
        self.assertEqual(result.metadata_lines_removed, 1)
        self.assertEqual(result.timed_lines_kept, 2)

    def test_transform_decodes_gb18030(self):
        source = "[00:01.00]测试\n".encode("gb18030")
        repaired, result = transform(source, Path("gb18030.lrc"))

        self.assertEqual(repaired, "[00:01.00]测试\n".encode("utf-8"))
        self.assertEqual(result.encoding, "GB18030")

    def test_iter_lrc_files_ignores_appledouble_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "keep.lrc").write_text("[00:01.00]ok\n", encoding="utf-8")
            (root / "._keep.lrc").write_bytes(b"sidecar")
            errors = []

            files = list(iter_lrc_files(root, errors))

            self.assertEqual(files, [root / "keep.lrc"])
            self.assertEqual(errors, [])

    def test_download_helpers_keep_safe_names_and_bilingual_timestamps(self):
        self.assertEqual(safe_filename("歌名:现场/特别版"), "歌名_现场_特别版")
        original = "[00:01.00]こんにちは\n[00:02.00]さようなら\n"
        translated = "[00:01.00]你好\n[00:02.00]再见\n"
        self.assertEqual(
            merge_bilingual_lyrics(original, translated),
            "[00:01.00]こんにちは\n[00:01.00]你好\n"
            "[00:02.00]さようなら\n[00:02.00]再见\n",
        )
        self.assertEqual(
            select_lyrics({"lrc": original, "tlyric": translated}, "translated"),
            translated,
        )

    def test_search_helpers_format_track_results(self):
        result = format_track(
            1,
            {
                "id": 123,
                "name": "测试歌曲",
                "artists": [{"name": "测试歌手"}],
                "album": {"name": "测试专辑"},
                "duration": 185000,
            },
        )
        self.assertEqual(result["id"], "123")
        self.assertEqual(result["artists"], ["测试歌手"])
        self.assertEqual(result["durationFormatted"], "3:05")
        self.assertEqual(format_duration(None), "0:00")

    def test_translation_only_replaces_japanese_timed_lines(self):
        source = (
            "[ti:示例]\n"
            "[00:01.00]こんにちは world\n"
            "[00:02.00]作詞：山田太郎\n"
            "[00:03.00]Hello\n"
        )
        self.assertEqual(japanese_lines(source), ["こんにちは world"])
        with patch("lrc_translate.translate_lines", return_value=["你好 world"]):
            translated = translate_lrc(source, api_key="test-key")
        self.assertEqual(
            translated,
            "[ti:示例]\n"
            "[00:01.00]你好 world\n"
            "[00:02.00]作詞：山田太郎\n"
            "[00:03.00]Hello\n",
        )

    def test_ncm_helpers_ignore_sidecars_and_find_matching_lyrics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            artist = root / "歌手"
            artist.mkdir()
            ncm = artist / "歌曲.ncm"
            ncm.write_bytes(b"not a real ncm")
            lyric = artist / "歌曲.lrc"
            lyric.write_text("[00:01.00]歌词\n", encoding="utf-8")
            (artist / "._歌曲.ncm").write_bytes(b"sidecar")
            self.assertEqual(iter_ncm_files(root), [ncm])
            self.assertEqual(matching_lrc(ncm), lyric)

    def test_ncmdump_conversion_keeps_original_output_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ncm = root / "source.ncm"
            ncm.write_bytes(b"not a real ncm")
            fake_ncmdump = root / "fake-ncmdump"
            fake_ncmdump.write_text(
                "#!/usr/bin/env python3\n"
                "from pathlib import Path\n"
                "import sys\n"
                "out = Path(sys.argv[sys.argv.index('--output') + 1])\n"
                "(out / '真实歌曲.flac').write_bytes(b'audio')\n",
                encoding="utf-8",
            )
            fake_ncmdump.chmod(0o755)
            converted, name = convert_ncm(ncm, str(fake_ncmdump), 10)
            converted.unlink(missing_ok=True)
            self.assertEqual(name, "真实歌曲.flac")


if __name__ == "__main__":
    unittest.main()
