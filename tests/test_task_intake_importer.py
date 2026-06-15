from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from company_kernel import task_intake_importer


class TaskIntakeImporterTest(unittest.TestCase):
    def test_imports_json_task_and_archives_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incoming = root / "incoming"
            processed = root / "processed"
            failed = root / "failed"
            incoming.mkdir()
            task_file = incoming / "task.json"
            task_file.write_text(
                json.dumps(
                    {
                        "task_id": "task-intake-001",
                        "from": "claude",
                        "to": "codex",
                        "title": "Test task",
                        "description": "Imported by intake bridge",
                        "priority": "P1",
                        "metadata": {"source": "unit-test"},
                    }
                ),
                encoding="utf-8",
            )
            conn = object()
            with mock.patch.object(task_intake_importer.companyctl, "connect", return_value=conn), mock.patch.object(
                task_intake_importer.companyctl,
                "submit_task_internal",
                return_value={"task": {"id": "task-intake-001"}, "file": "/tmp/task-intake-001.json"},
            ) as submit:
                result = task_intake_importer.import_once(incoming=incoming, processed=processed, failed=failed)

            self.assertEqual(result["imported"], 1)
            self.assertEqual(result["failed"], 0)
            submit.assert_called_once()
            kwargs = submit.call_args.kwargs
            self.assertEqual(kwargs["source"], "claude")
            self.assertEqual(kwargs["target"], "codex")
            self.assertEqual(kwargs["task_id"], "task-intake-001")
            self.assertEqual(kwargs["metadata"]["source"], "unit-test")
            self.assertFalse(task_file.exists())
            self.assertTrue((processed / "task.json").exists())
            receipts = list(processed.glob("task.json.receipt.json"))
            self.assertEqual(len(receipts), 1)
            receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
            self.assertTrue(receipt["ok"])
            self.assertEqual(receipt["result"]["task"]["id"], "task-intake-001")

    def test_invalid_json_moves_to_failed_without_submit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incoming = root / "incoming"
            processed = root / "processed"
            failed = root / "failed"
            incoming.mkdir()
            task_file = incoming / "broken.json"
            task_file.write_text("{", encoding="utf-8")
            with mock.patch.object(task_intake_importer.companyctl, "submit_task_internal") as submit:
                result = task_intake_importer.import_once(incoming=incoming, processed=processed, failed=failed)

            self.assertEqual(result["imported"], 0)
            self.assertEqual(result["failed"], 1)
            submit.assert_not_called()
            self.assertFalse(task_file.exists())
            self.assertTrue((failed / "broken.json").exists())
            receipt = json.loads((failed / "broken.json.receipt.json").read_text(encoding="utf-8"))
            self.assertFalse(receipt["ok"])
            self.assertIn("Expecting property name", receipt["error"])


if __name__ == "__main__":
    unittest.main()
