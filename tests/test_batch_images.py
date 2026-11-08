from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import batch_images
import studio


def photo(size=(64, 40), format="JPEG", orientation=None):
    image = Image.new("RGB", size, "#56789a")
    output = io.BytesIO()
    exif = Image.Exif()
    if orientation: exif[274] = orientation
    image.save(output, format=format, exif=exif)
    return output.getvalue()


class BatchPhotoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = self.root / "cache"
        self.patcher = patch.object(studio, "BATCH_CACHE_ROOT", self.cache)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.temp.cleanup()

    def test_ordinary_photo_is_byte_identical(self):
        source = photo()
        result = batch_images.prepare_photo(source, self.cache, keep_compatible=True)
        self.assertFalse(result["preparation"]["compressed"])
        self.assertEqual(result["prepared_path"].read_bytes(), source)

    def test_byte_only_overflow_recompresses_without_resizing(self):
        source = photo() + bytes(26 * 1024 * 1024)
        result = batch_images.prepare_photo(source, self.cache)
        info = result["preparation"]
        self.assertTrue(info["compressed"])
        self.assertEqual((info["width"], info["height"]), (64, 40))
        self.assertLessEqual(info["size"], batch_images.TARGET_BYTES)
        self.assertEqual(result["source_sha256"], hashlib.sha256(source).hexdigest())

    def test_real_45mp_resize_preserves_all_four_corners_and_aspect(self):
        image = Image.new("RGB", (8256, 5504), "white")
        draw = ImageDraw.Draw(image)
        colors = [(240, 20, 20), (20, 220, 20), (20, 20, 240), (240, 220, 20)]
        rectangles = [(0, 0, 800, 800), (7455, 0, 8255, 800),
                      (0, 4703, 800, 5503), (7455, 4703, 8255, 5503)]
        for rectangle, color in zip(rectangles, colors): draw.rectangle(rectangle, fill=color)
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=98, subsampling=0)
        image.close()
        source = output.getvalue()
        result = batch_images.prepare_photo(source, self.cache)
        info = result["preparation"]
        self.assertEqual(info["original_width"] * info["original_height"], 45_441_024)
        self.assertEqual(max(info["width"], info["height"]), 4096)
        self.assertLess(abs(info["width"] / info["height"] - 1.5), 0.001)
        self.assertFalse(info["crop"])
        with Image.open(result["prepared_path"]) as resized:
            points = [(20, 20), (resized.width - 21, 20), (20, resized.height - 21), (resized.width - 21, resized.height - 21)]
            for point, expected in zip(points, colors):
                self.assertLess(max(abs(a - b) for a, b in zip(resized.getpixel(point), expected)), 12)

    def test_orientation_is_applied_once(self):
        source = photo((64, 40), orientation=6) + bytes(26 * 1024 * 1024)
        result = batch_images.prepare_photo(source, self.cache)
        info = result["preparation"]
        self.assertEqual((info["original_width"], info["original_height"]), (40, 64))
        with Image.open(result["prepared_path"]) as output:
            self.assertEqual(output.size, (40, 64))
            self.assertIn(output.getexif().get(274), (None, 1))

    def test_transparency_survives_compression(self):
        image = Image.new("RGBA", (128, 80), (30, 80, 120, 0))
        ImageDraw.Draw(image).rectangle((30, 20, 90, 60), fill=(200, 40, 30, 255))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        source = buffer.getvalue() + bytes(26 * 1024 * 1024)
        result = batch_images.prepare_photo(source, self.cache)
        with Image.open(result["prepared_path"]) as output:
            self.assertEqual(output.convert("RGBA").getpixel((0, 0))[3], 0)
            self.assertEqual(output.convert("RGBA").getpixel((60, 40))[3], 255)

    def test_bad_images_and_input_limits_leave_no_cache(self):
        for payload in (b"", b"not an image", b"\xff\xd8\xffcorrupt"):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                batch_images.prepare_photo(payload, self.cache)
        with patch.object(batch_images, "MAX_SOURCE_PIXELS", 100), self.assertRaisesRegex(ValueError, "2 亿"):
            batch_images.prepare_photo(photo(), self.cache)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_folder_compresses_then_reads_copy_without_changing_original(self):
        folder = self.root / "photos"
        folder.mkdir()
        source = folder / "DSC_0001.JPG"
        original = photo() + bytes(26 * 1024 * 1024)
        source.write_bytes(original)
        root, files = studio.scan_batch_folder(str(folder), (self.root,))
        self.assertEqual(source.read_bytes(), original)
        registry = {}
        selection = studio.register_batch_selection(registry, root, files)
        self.assertTrue(selection["files"][0]["preparation"]["compressed"])
        self.assertNotIn(str(self.root), json.dumps(selection))
        payload, suffix, name = studio.read_batch_selection_file(registry, selection["selection_id"], selection["files"][0]["id"])
        self.assertEqual(name, "DSC_0001.JPG")
        self.assertEqual(suffix, ".jpg")
        self.assertLess(len(payload), 25 * 1024 * 1024)
        self.assertEqual(list(self.cache.iterdir()), [])
        self.assertEqual(source.read_bytes(), original)

    def test_source_changes_after_compression_are_rejected(self):
        folder = self.root / "photos"
        folder.mkdir()
        source = folder / "1.jpg"
        source.write_bytes(photo() + bytes(26 * 1024 * 1024))
        root, files = studio.scan_batch_folder(str(folder), (self.root,))
        registry = {}
        selection = studio.register_batch_selection(registry, root, files)
        source.write_bytes(photo())
        with self.assertRaisesRegex(ValueError, "发生了变化"):
            studio.read_batch_selection_file(registry, selection["selection_id"], selection["files"][0]["id"])
        studio.prune_batch_selections(registry, time.time() + 50000)
        self.assertFalse(registry)
        self.assertEqual(list(self.cache.iterdir()), [])
        self.assertTrue(source.exists())

    def test_failed_folder_scan_removes_prepared_copies(self):
        folder = self.root / "photos"
        folder.mkdir()
        (folder / "1.jpg").write_bytes(photo() + bytes(26 * 1024 * 1024))
        (folder / "2.jpg").write_bytes(b"invalid")
        with self.assertRaises(ValueError): studio.scan_batch_folder(str(folder), (self.root,))
        self.assertEqual(list(self.cache.iterdir()), [])
        self.assertEqual(len(list(folder.iterdir())), 2)


class BatchImportHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cache = self.root / "cache"
        self.patch_cache = patch.object(studio, "BATCH_CACHE_ROOT", self.cache)
        self.patch_cache.start()
        self.app = studio.build_app()
        self.app.on_startup.clear()
        self.app.on_cleanup.clear()
        self.app["session"] = Mock()
        self.app["model_integrity"] = {"ready": True}
        self.client = TestClient(TestServer(self.app, host="127.0.0.1"))
        await self.client.start_server()
        self.headers = {"X-H3-CSRF": self.app["csrf_token"]}

    async def asyncTearDown(self):
        await self.client.close()
        self.patch_cache.stop()
        self.temp.cleanup()

    async def upload(self, payload, selection_id=None):
        form = FormData()
        if selection_id: form.add_field("selection_id", selection_id)
        form.add_field("image", payload, filename="DSC_0001.JPG", content_type="image/jpeg")
        return await self.client.post("/api/batch-images/import", data=form, headers=self.headers)

    async def test_upload_append_and_existing_generate_contract(self):
        response = await self.upload(photo() + bytes(26 * 1024 * 1024))
        self.assertEqual(response.status, 200, await response.text())
        selection = (await response.json())["selection"]
        first = selection["files"][0]
        self.assertTrue(first["preparation"]["compressed"])
        response = await self.upload(photo(), selection["selection_id"])
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(len(self.app["batch_selections"][selection["selection_id"]]["files"]), 2)
        form = FormData()
        for key, value in {"prompt": "固定镜头，光线缓缓变化。", "batch_selection_id": selection["selection_id"], "batch_file_id": first["id"]}.items():
            form.add_field(key, value, content_type="text/plain")
        async def inspect_job(app, job):
            self.assertTrue(job["batch_image_preparation"]["compressed"])
            with Image.open(job["local_image"]) as image:
                self.assertEqual(image.size, (64, 40))
            Path(job["local_image"]).unlink()
            job["status"] = "completed"
        with patch.object(studio, "comfy_health", AsyncMock(return_value=(True, {}))), \
             patch.object(studio, "model_state", return_value={"ready": True}), \
             patch.object(studio, "run_job", AsyncMock(side_effect=inspect_job)):
            response = await self.client.post("/api/generate", data=form, headers=self.headers)
            self.assertEqual(response.status, 202, await response.text())
            await asyncio.gather(*list(self.app["tasks"]))

    async def test_upload_enforces_csrf_and_selection_credentials(self):
        response = await self.client.post("/api/batch-images/import", data={})
        self.assertEqual(response.status, 403)
        response = await self.upload(photo(), "../../outside")
        self.assertEqual(response.status, 400)
        self.assertFalse(self.app["batch_selections"])

    async def test_regular_video_upload_compresses_bytes_and_pixels(self):
        for payload, expected_size in [
            (photo() + bytes(26 * 1024 * 1024), (64, 40)),
            (photo((8256, 5504), orientation=6), (2731, 4096)),
        ]:
            with self.subTest(size=expected_size):
                original_hash = hashlib.sha256(payload).hexdigest()
                form = FormData()
                form.add_field("prompt", "固定镜头，光线缓缓变化。")
                form.add_field("image", payload, filename="large.jpg", content_type="image/jpeg")
                async def inspect_job(app, job):
                    with Image.open(job["local_image"]) as image:
                        self.assertEqual(image.size, expected_size)
                        image.load()
                    Path(job["local_image"]).unlink()
                    job["status"] = "completed"
                with patch.object(studio, "UPLOAD_ROOT", self.root / "uploads"), \
                     patch.object(studio, "comfy_health", AsyncMock(return_value=(True, {}))), \
                     patch.object(studio, "model_state", return_value={"ready": True}), \
                     patch.object(studio, "run_job", AsyncMock(side_effect=inspect_job)):
                    response = await self.client.post("/api/generate", data=form, headers=self.headers)
                    self.assertEqual(response.status, 202, await response.text())
                    await asyncio.gather(*list(self.app["tasks"]))
                self.assertEqual(hashlib.sha256(payload).hexdigest(), original_hash)
                self.assertEqual(list(self.cache.iterdir()), [])
                self.assertEqual(list((self.root / "uploads").iterdir()), [])

    async def test_all_character_reference_slots_compress_before_generation(self):
        form = FormData()
        form.add_field("prompt", "保留人物特征，调整光线。")
        expected_sizes = [(4096, 2731), (64, 40), (64, 40)]
        for index, field in enumerate(["reference_master", "reference_pose", "reference_detail"]):
            payload = photo((8256, 5504)) if index == 0 else photo() + bytes(26 * 1024 * 1024)
            form.add_field(field, payload, filename=f"reference-{index}.jpg", content_type="image/jpeg")
        async def inspect_job(app, job):
            self.assertEqual(len(job["normalized_reference_sha256"]), 3)
            for path, expected_size in zip(job["local_reference_slots"], expected_sizes):
                with Image.open(path) as image:
                    self.assertEqual(image.size, expected_size)
                    image.load()
                Path(path).unlink()
            job["status"] = "completed"
        with patch.object(studio, "UPLOAD_ROOT", self.root / "uploads"), \
             patch.object(studio, "run_image_job", AsyncMock(side_effect=inspect_job)):
            response = await self.client.post("/api/images/generate", data=form, headers=self.headers)
            self.assertEqual(response.status, 202, await response.text())
            await asyncio.gather(*list(self.app["tasks"]))
        self.assertEqual(list(self.cache.iterdir()), [])
        self.assertEqual(list((self.root / "uploads").iterdir()), [])

    async def test_batch_selection_can_feed_image_reference_master(self):
        response = await self.upload(photo())
        self.assertEqual(response.status, 200, await response.text())
        selection = (await response.json())["selection"]
        first = selection["files"][0]
        form = FormData()
        for key, value in {
            "mode": "edit",
            "prompt": "保留人物身份，生成正面半身照。",
            "quality": "draft",
            "reference_preprocess": "none",
            "batch_selection_id": selection["selection_id"],
            "batch_file_id": first["id"],
        }.items():
            form.add_field(key, value, content_type="text/plain")

        captured = {}

        async def inspect_job(app, job):
            captured.update(job)
            self.assertEqual(job["batch_item_id"], first["id"])
            self.assertEqual(job["batch_relative_path"], "DSC_0001.JPG")
            self.assertEqual(job["reference_policy"]["supplied_slots"], ["master"])
            self.assertIsNotNone(job["batch_image_preparation"])
            master = Path(job["local_reference_slots"][0])
            with Image.open(master) as image:
                self.assertEqual(image.size, (64, 40))
                image.load()
            master.unlink()
            job.update(status="completed", image_url="/results/batch.png")

        with patch.object(studio, "UPLOAD_ROOT", self.root / "uploads"), \
             patch.object(studio, "run_image_job", AsyncMock(side_effect=inspect_job)):
            response = await self.client.post("/api/images/generate", data=form, headers=self.headers)
            self.assertEqual(response.status, 202, await response.text())
            public = (await response.json())["job"]
            self.assertEqual(public["batch_item_id"], first["id"])
            self.assertEqual(public["batch_relative_path"], "DSC_0001.JPG")
            await asyncio.gather(*list(self.app["tasks"]))

        self.assertTrue(captured)
        self.assertTrue(
            self.app["batch_selections"][selection["selection_id"]]["files"][first["id"]]["claimed"]
        )
        self.assertEqual(list(self.cache.iterdir()), [])
        self.assertEqual(list((self.root / "uploads").iterdir()), [])

    async def test_sequence_first_frame_accepts_oversized_photo(self):
        service = self.app["sequences"]
        sequence_root = self.root / "sequences"
        sequence_root.mkdir()
        sequence_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        plan = {"id": sequence_id, "status": "ready", "shots": []}
        service.plans[sequence_id] = plan
        form = FormData()
        form.add_field("image", photo() + bytes(26 * 1024 * 1024), filename="large.jpg", content_type="image/jpeg")
        with patch.object(service, "root", sequence_root), \
             patch.object(studio, "UPLOAD_ROOT", self.root / "uploads"):
            response = await self.client.post(f"/api/sequences/{sequence_id}/first-image", data=form, headers=self.headers)
            self.assertEqual(response.status, 200, await response.text())
        self.assertTrue(plan["first_image"])
        with Image.open(sequence_root / f"{sequence_id}.png") as image:
            self.assertEqual(image.size, (64, 40))
        self.assertEqual(list(self.cache.iterdir()), [])
        self.assertEqual(list((self.root / "uploads").iterdir()), [])

    async def test_cancel_waits_for_worker_and_cleans_its_output(self):
        started, release = threading.Event(), threading.Event()
        self.cache.mkdir()
        def worker():
            started.set()
            release.wait(3)
            output = self.cache / ("batch-" + "a" * 32 + ".jpg")
            output.write_bytes(photo())
            return {"prepared_path": output}
        task = asyncio.create_task(studio.batch_import_thread(worker))
        await asyncio.to_thread(started.wait, 2)
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(list(self.cache.iterdir()), [])


if __name__ == "__main__": unittest.main()
