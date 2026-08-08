import tempfile
import unittest

from storage import AssistantDB


class TestAssistantDBReliability(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = AssistantDB(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_prompt_favorite_is_unique_by_positive_and_negative(self):
        first = self.db.add_favorite("cat", "bad anatomy", model="Z-Image Turbo")
        second = self.db.add_favorite("cat", "bad anatomy", model="Z-Image Turbo")
        different_negative = self.db.add_favorite("cat", "text", model="Z-Image Turbo")

        self.assertEqual(first, second)
        self.assertNotEqual(first, different_negative)
        self.assertEqual(len(self.db.list_favorites()), 2)

        self.db.set_favorite_note(first, "角色草稿")
        favorite = next(item for item in self.db.list_favorites() if item["id"] == first)
        self.assertEqual(favorite["note"], "角色草稿")

    def test_image_records_are_idempotent_by_path(self):
        asset_id = self.db.add_asset("C:/output/a.png", "C:/output/a.png", "cat", "Z-Image Turbo", {})
        self.assertEqual(
            asset_id,
            self.db.add_asset("C:/output/a.png", "C:/output/a.png", "cat", "Z-Image Turbo", {}),
        )
        history_id = self.db.add_history("cat", "bad", "Z-Image Turbo", {}, image_path="C:/output/a.png")
        self.assertEqual(
            history_id,
            self.db.add_history("cat", "bad", "Z-Image Turbo", {}, image_path="C:/output/a.png"),
        )
        self.assertEqual(len(self.db.list_assets()), 1)
        self.assertEqual(len(self.db.list_history()), 1)

    def test_generation_task_snapshot_survives_memory_loss(self):
        self.db.save_generation_task("prompt-123", {"prompt": "cat", "negative": "bad", "seed": 42}, "workflow.json")
        task = self.db.get_generation_task("prompt-123")

        self.assertEqual(task["params"]["negative"], "bad")
        self.assertEqual(task["workflow_path"], "workflow.json")


if __name__ == "__main__":
    unittest.main()
