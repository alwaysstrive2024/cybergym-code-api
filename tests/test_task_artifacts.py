import io
import json
import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cybergym.agents.runtime import TaskSandbox
from cybergym.task.arvo_task import prepare_arvo_files
from cybergym.task.types import STAGED_ARCHIVES_MANIFEST, TaskDifficulty
from scripts.evaluation.run_langgraph_eval import TaskSandbox as LangGraphTaskSandbox


class FakeContainer:
    def __init__(self):
        self.archive = b""

    def put_archive(self, _workspace, data):
        self.archive = data.read()
        return True


class TaskArtifactTests(unittest.TestCase):
    def test_staged_archive_uses_manifest_without_touching_dataset(self):
        with TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            dataset = root / "dataset"
            task_dir = root / "task"
            dataset.mkdir()
            task_dir.mkdir()
            source = dataset / "repo-vul.tar.gz"
            source.write_bytes(b"archive")
            original_stat = source.stat()

            prepare_arvo_files(
                task_dir,
                dataset,
                "arvo:test",
                "http://127.0.0.1:1",
                "agent",
                "checksum",
                TaskDifficulty.level0,
                stage_archives=True,
            )

            self.assertFalse((task_dir / "repo-vul.tar.gz").exists())
            self.assertEqual(source.stat().st_nlink, original_stat.st_nlink)
            manifest = json.loads((task_dir / STAGED_ARCHIVES_MANIFEST).read_text(encoding="utf-8"))
            self.assertEqual(manifest, {"repo-vul.tar.gz": str(source)})

    def test_sandbox_dereferences_and_removes_staged_archive(self):
        sandbox_classes = (
            (TaskSandbox, "cybergym.agents.runtime.docker.from_env"),
            (LangGraphTaskSandbox, "scripts.evaluation.run_langgraph_eval.docker.from_env"),
        )
        for sandbox_class, docker_patch in sandbox_classes:
            with self.subTest(sandbox=sandbox_class.__module__):
                self._assert_sandbox_stages_archive(sandbox_class, docker_patch)

    def _assert_sandbox_stages_archive(self, sandbox_class, docker_patch):
        with TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source = root / "source.tar.gz"
            source.write_bytes(b"archive")
            task_dir = root / "task"
            task_dir.mkdir()
            manifest_path = task_dir / STAGED_ARCHIVES_MANIFEST
            manifest_path.write_text(json.dumps({"repo-vul.tar.gz": str(source)}), encoding="utf-8")
            container = FakeContainer()

            with patch(docker_patch):
                sandbox = sandbox_class(task_dir, "unused", 1)
            sandbox.container = container
            sandbox._upload_initial_workspace()

            self.assertFalse(manifest_path.exists())
            self.assertFalse((task_dir / "repo-vul.tar.gz").exists())
            self.assertTrue(source.exists())
            self.assertEqual(source.read_bytes(), b"archive")
            with tarfile.open(fileobj=io.BytesIO(container.archive)) as archive:
                member = archive.getmember("repo-vul.tar.gz")
                self.assertTrue(member.isfile())
                extracted = archive.extractfile(member)
                self.assertIsNotNone(extracted)
                self.assertEqual(extracted.read(), b"archive")
