import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_service import app as service


class ReferenceFileTests(unittest.TestCase):
    def test_upload_list_preview_download_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(service, "ROOT", Path(tmp)):
            with TestClient(service.app) as client:
                uploaded = client.post(
                    "/api/studio/projects/project-one/reference-files",
                    files={
                        "file": (
                            "中文参考资料.docx",
                            b"PK\x03\x04reference-word",
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        )
                    },
                    data={"text": "项目完成率 83%。\n下一步重点是续约与交付风险。"},
                )
                self.assertEqual(uploaded.status_code, 201, uploaded.text)
                attachment = uploaded.json()
                self.assertEqual(attachment["name"], "中文参考资料.docx")
                self.assertEqual(attachment["extension"], ".docx")
                self.assertGreater(attachment["characters"], 0)

                listed = client.get("/api/studio/projects/project-one/reference-files")
                self.assertEqual(listed.status_code, 200)
                self.assertEqual([item["id"] for item in listed.json()], [attachment["id"]])

                preview = client.get(attachment["textUrl"])
                self.assertEqual(preview.status_code, 200)
                self.assertIn("项目完成率 83%", preview.json()["text"])

                downloaded = client.get(attachment["downloadUrl"])
                self.assertEqual(downloaded.status_code, 200)
                self.assertEqual(downloaded.content, b"PK\x03\x04reference-word")
                self.assertIn(
                    "wordprocessingml.document",
                    downloaded.headers["content-type"],
                )

                deleted = client.delete(
                    f"/api/studio/projects/project-one/reference-files/{attachment['id']}"
                )
                self.assertEqual(deleted.status_code, 200)
                self.assertEqual(
                    client.get("/api/studio/projects/project-one/reference-files").json(),
                    [],
                )


if __name__ == "__main__":
    unittest.main()
