import base64
import os
import tempfile
import unittest

import app
from storage import AssistantDB


class TestImageImport(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db = app.db
        self.original_import_dir = app.ASSET_IMPORT_DIR
        app.db = AssistantDB(self.temp_dir.name)
        app.ASSET_IMPORT_DIR = os.path.join(self.temp_dir.name, "imports")

    def tearDown(self):
        app.db = self.original_db
        app.ASSET_IMPORT_DIR = self.original_import_dir
        self.temp_dir.cleanup()

    def test_import_image_creates_unsaved_asset(self):
        # A valid one-pixel PNG keeps the test independent of image libraries.
        pixel = base64.b64encode(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0dIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
        ).decode("ascii")
        result = app.import_asset_image("test.png", f"data:image/png;base64,{pixel}")

        self.assertTrue(os.path.isfile(result["path"]))
        asset = app.db.list_assets()[0]
        self.assertEqual(asset["model"], "导入图片")
        self.assertEqual(asset["saved"], 0)


if __name__ == "__main__":
    unittest.main()
