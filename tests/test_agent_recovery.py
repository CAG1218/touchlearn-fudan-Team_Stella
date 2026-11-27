from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests import EXAMPLES  # noqa: F401  (ensures src is on sys.path)

from touchlearn.agent.agent import TouchLearnAgent


class InterruptedJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.agent = TouchLearnAgent(output_root=self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_job(self, job_id: str, **fields: object) -> Path:
        job_dir = self.root / "jobs" / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "job.json").write_text(json.dumps(fields), encoding="utf-8")
        return job_dir

    def read_job(self, job_dir: Path) -> dict:
        return json.loads((job_dir / "job.json").read_text(encoding="utf-8"))

    def test_only_in_flight_jobs_are_failed(self) -> None:
        queued = self.write_job("a-queued", status="queued", progress=0)
        building = self.write_job("b-building", status="building", progress=45)
        done = self.write_job("c-done", status="done", progress=100)
        failed = self.write_job("d-failed", status="failed", progress=100, error="old")

        recovered = self.agent.recover_interrupted_jobs()

        self.assertEqual(recovered, ["a-queued", "b-building"])
        for job_dir in (queued, building):
            job = self.read_job(job_dir)
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["progress"], 100)
            self.assertIn("服务重启", job["message"])
            self.assertTrue(job["error"])
        self.assertEqual(self.read_job(done)["status"], "done")
        self.assertEqual(self.read_job(failed)["error"], "old")

    def test_second_run_is_idempotent(self) -> None:
        self.write_job("building", status="building", progress=20)
        self.assertEqual(self.agent.recover_interrupted_jobs(), ["building"])
        self.assertEqual(self.agent.recover_interrupted_jobs(), [])

    def test_broken_or_missing_records_are_skipped(self) -> None:
        (self.root / "jobs" / "broken").mkdir(parents=True)
        (self.root / "jobs" / "broken" / "job.json").write_text("{", encoding="utf-8")
        (self.root / "jobs" / "empty").mkdir(parents=True)
        self.assertEqual(self.agent.recover_interrupted_jobs(), [])

    def test_no_jobs_directory_returns_empty(self) -> None:
        agent = TouchLearnAgent(output_root=self.root / "fresh")
        self.assertEqual(agent.recover_interrupted_jobs(), [])


if __name__ == "__main__":
    unittest.main()