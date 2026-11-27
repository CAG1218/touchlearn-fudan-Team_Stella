"""``touchlearn verify`` 必须能独立指出产物与 manifest 的不一致。"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn.cli import main


def _make_job(root: Path, *, corrupt: bool = False) -> Path:
    job = root / "jobs" / "demo-job"
    job.mkdir(parents=True)
    payload = b"touchlearn-demo-artifact"
    artifact = job / "model.stl"
    artifact.write_bytes(payload)
    manifest = {
        "job_id": "demo-job",
        "artifacts": [
            {"name": "model.stl", "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()},
        ],
    }
    (job / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if corrupt:
        artifact.write_bytes(b"tampered")
    return job


class VerifyCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_verify(self, job: Path) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["verify", "--job", str(job)])
        return code, output.getvalue()

    def test_passes_on_matching_job(self) -> None:
        code, out = self.run_verify(_make_job(self.root))
        self.assertEqual(code, 0)
        self.assertIn("PASS", out)
        self.assertIn("model.stl", out)

    def test_fails_on_tampered_artifact(self) -> None:
        code, out = self.run_verify(_make_job(self.root, corrupt=True))
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)
        self.assertIn("SHA-256 不匹配", out)

    def test_reports_missing_artifact(self) -> None:
        job = _make_job(self.root)
        (job / "model.stl").unlink()
        code, out = self.run_verify(job)
        self.assertEqual(code, 1)
        self.assertIn("文件缺失", out)


if __name__ == "__main__":
    unittest.main()