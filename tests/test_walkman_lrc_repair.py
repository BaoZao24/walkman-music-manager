import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
