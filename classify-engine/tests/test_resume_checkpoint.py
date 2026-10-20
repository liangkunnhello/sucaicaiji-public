import importlib.util
import json
import sys
import tempfile

import _tempdir  # noqa: F401  # 把临时目录挪出 %TEMP%（见 _tempdir.py）
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("classify_pure_api", SCRIPTS / "classify_pure_api.py")
CLASSIFIER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(CLASSIFIER)


class ResumeCheckpointTest(unittest.TestCase):
    def test_regular_api_call_reuses_cached_recognition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "cached.jpg"
            image.write_bytes(b"image")
            cached = {"visible_text": ["cached result"]}
            config = {"api_endpoint": "https://example.test", "model_id": "model", "api_key": "key"}

            with patch.object(CLASSIFIER, "read_cache", return_value=cached) as read_cache, \
                    patch.object(CLASSIFIER, "get_session") as get_session:
                result = CLASSIFIER.call_vision_api(image, config, root / "cache")

            read_cache.assert_called_once()
            get_session.assert_not_called()
            self.assertTrue(result["_cached"])

    def test_refresh_api_call_skips_cache_and_replaces_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "refresh.jpg"
            image.write_bytes(b"image")
            config = {"api_endpoint": "https://example.test", "model_id": "model", "api_key": "key"}
            response = Mock(status_code=200, headers={})
            response.json.return_value = {"model": "model", "id": "response", "usage": {}}
            session = Mock()
            session.post.return_value = response

            with patch.object(CLASSIFIER, "read_cache") as read_cache, \
                    patch.object(CLASSIFIER, "get_session", return_value=session), \
                    patch.object(CLASSIFIER, "extract_response_text", return_value="{}"), \
                    patch.object(CLASSIFIER, "parse_json_object", return_value={"visible_text": ["fresh result"]}), \
                    patch.object(CLASSIFIER, "write_cache") as write_cache:
                result = CLASSIFIER.call_vision_api(image, config, root / "cache", refresh_api=True)

            read_cache.assert_not_called()
            write_cache.assert_called_once()
            self.assertFalse(result["_cached"])
            self.assertEqual(["fresh result"], result["visible_text"])

    def test_move_mode_writes_audit_with_key_captured_before_move(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "raw" / "move.jpg"
            output = root / "classified"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"image")
            source_key = CLASSIFIER.image_checkpoint_key(source)

            destination = CLASSIFIER.place_file(source, output, "move")
            audit = root / "audit.jsonl"
            with audit.open("w", encoding="utf-8") as handle:
                CLASSIFIER.write_audit(
                    handle,
                    source,
                    {"product": "产品", "direction": "方向"},
                    "AUTO",
                    destination,
                    source_key,
                )

            entry = json.loads(audit.read_text(encoding="utf-8"))
            self.assertFalse(source.exists())
            self.assertTrue(destination.is_file())
            self.assertEqual(source_key, entry["source_key"])

    def test_completed_image_is_resumed_from_stable_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "raw" / "done.jpg"
            output = root / ".重新分类工作区"
            destination = output / "产品" / "方向" / source.name
            source.parent.mkdir(parents=True)
            destination.parent.mkdir(parents=True)
            source.write_bytes(b"source")
            destination.write_bytes(b"classified")
            audit = output / "_API分类审计.jsonl"
            audit.write_text(json.dumps({
                "source": str(source),
                "source_key": CLASSIFIER.image_checkpoint_key(source),
                "destination": str(destination),
                "status": "AUTO",
            }, ensure_ascii=False) + "\n", encoding="utf-8")

            completed = CLASSIFIER.load_completed_checkpoints(audit, output)

            self.assertIn(CLASSIFIER.image_checkpoint_key(source), completed)

    def test_api_failure_is_removed_and_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "raw" / "retry.jpg"
            output = root / ".重新分类工作区"
            destination = output / "_待确认" / "API失败" / source.name
            source.parent.mkdir(parents=True)
            destination.parent.mkdir(parents=True)
            source.write_bytes(b"source")
            destination.write_bytes(b"failed")
            audit = output / "_API分类审计.jsonl"
            audit.write_text(json.dumps({
                "source": str(source),
                "source_key": CLASSIFIER.image_checkpoint_key(source),
                "destination": str(destination),
                "status": "REVIEW",
                "api_failed": True,
            }, ensure_ascii=False) + "\n", encoding="utf-8")

            completed = CLASSIFIER.load_completed_checkpoints(audit, output)

            self.assertNotIn(CLASSIFIER.image_checkpoint_key(source), completed)
            self.assertFalse(destination.exists())

    def test_legacy_workspace_destination_is_remapped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "raw" / "legacy.jpg"
            output = root / ".重新分类工作区"
            destination = output / "产品" / source.name
            source.parent.mkdir(parents=True)
            destination.parent.mkdir(parents=True)
            source.write_bytes(b"source")
            destination.write_bytes(b"classified")
            old_destination = root / ".重新分类临时-20260727-180000-000" / "产品" / source.name
            audit = output / "_API分类审计.jsonl"
            audit.write_text(json.dumps({
                "source": str(source),
                "destination": str(old_destination),
                "status": "AUTO",
            }, ensure_ascii=False) + "\n", encoding="utf-8")

            completed = CLASSIFIER.load_completed_checkpoints(audit, output)

            entry = completed[CLASSIFIER.image_checkpoint_key(source)]
            self.assertEqual(destination, Path(entry["destination"]))


if __name__ == "__main__":
    unittest.main()
