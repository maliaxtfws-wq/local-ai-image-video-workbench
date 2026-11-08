import asyncio
import copy
import hashlib
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import studio
from storyboards import Storyboards
from aiohttp.test_utils import TestClient, TestServer


class BatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = studio.build_app()
        self.app.on_startup.clear()
        self.app.on_cleanup.clear()
        self.store = Storyboards(self.app, studio, self.root / "boards")
        self.app["storyboards"] = self.store
        self.queue = self.app["storyboard_batches"]
        self.patch_upload = patch.object(studio, "UPLOAD_ROOT", self.root / "uploads")
        self.patch_upload.start()
        studio.UPLOAD_ROOT.mkdir()
        self.project = self.store.create("Batch test")
        self.pid = self.project["id"]
        directory = self.store.directory(self.pid) / "assets"
        directory.mkdir()
        for name in ("a", "b", "c"):
            (directory / (name + ".png")).write_bytes(name.encode())
            self.project["assets"][name] = dict(filename=name + ".png", original_name=name + ".png", sha256=hashlib.sha256(name.encode()).hexdigest())
        self.rid, self.rid2, self.sid, self.sid2 = [uuid.uuid4().hex for _ in range(4)]
        self.project.update(characters=[dict(id=self.rid, name="A", description="", asset="a"),
                                       dict(id=self.rid2, name="B", description="", asset="b")],
            shots=[dict(id=self.sid, title="一", cast=[self.rid, self.rid2], action="交谈", guide=""),
                   dict(id=self.sid2, title="二", cast=[self.rid], action="挥手", guide="")])
        self.store.write(self.project)
        self.payload = dict(request_id=uuid.uuid4().hex, settings=dict(seed="17"), items=[
            dict(asset_id="c", shot_id=self.sid, target=self.rid),
            dict(asset_id="a", shot_id=self.sid2, target=self.rid)])
        self.calls = []
        self.block = None
        self.fail = False

        async def inference(app, job):
            self.calls.append(job)
            if self.block:
                await self.block.wait()
            if self.fail:
                job.update(status="failed", error="test failure")
            else:
                output = self.root / (job["id"] + ".png")
                output.write_bytes(b"result")
                job.update(status="completed", output=str(output), output_sha256="result")
            self.store.finish(job)
            for path in job["local_reference_slots"]:
                if path:
                    Path(path).unlink(missing_ok=True)
        self.patch_run = patch.object(studio, "run_image_job", inference)
        self.patch_run.start()
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.headers = {"X-H3-CSRF": self.app["csrf_token"]}

    async def asyncTearDown(self):
        if self.block:
            self.block.set()
        await self.drain()
        await self.client.close()
        self.patch_run.stop()
        self.patch_upload.stop()
        self.temp.cleanup()

    async def drain(self):
        if self.app["tasks"]:
            await asyncio.gather(*list(self.app["tasks"]))

    async def test_custom_output_copies_and_resumes_without_generation(self):
        destination = self.root / "chosen output"
        self.payload["export_dir"] = str(destination)
        batch = self.queue.create(self.pid, self.payload)
        self.assertEqual(batch["export_dir"], str(destination.resolve()))
        self.payload["export_dir"] = str(self.root / "different")
        self.assertEqual(self.queue.create(self.pid, self.payload)["export_dir"], str(destination.resolve()))
        for item in batch["items"]:
            job = self.queue.make_job(batch, item)
            self.store.begin(job, item["snapshot"], uuid.uuid4().hex)
            source = self.root / (job["id"] + ".png")
            source.write_bytes(b"verified image")
            job.update(status="completed", output=str(source), output_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
            self.store.finish(job)
            item.update(status="completed", job_id=job["id"])
        await self.queue.launch(batch)
        await self.drain()
        self.assertEqual(batch["status"], "completed")
        self.assertEqual(len(list(destination.glob("*.png"))), 2)
        self.assertEqual(self.calls, [])
        first = Path(batch["items"][0]["export_path"])
        first.write_bytes(b"existing user file")
        batch["items"][0]["export_error"] = "retry"
        await self.queue.launch(batch)
        await self.drain()
        self.assertEqual(batch["status"], "failed")
        self.assertEqual(first.read_bytes(), b"existing user file")
        self.assertEqual(self.calls, [])

    async def test_invalid_output_rejected_before_batch_saved(self):
        self.payload["export_dir"] = "relative-folder"
        with self.assertRaises(ValueError):
            self.queue.create(self.pid, self.payload)
        self.assertFalse(self.queue.path(self.pid, self.payload["request_id"]).exists())

    async def test_single_image_optional_output(self):
        import image_output
        destination = image_output.validate_folder(str(self.root / "single output"))
        batch = self.queue.create(self.pid, self.payload)
        item = batch["items"][0]
        job = self.queue.make_job(batch, item)
        job["storyboard"]["export_dir"] = destination
        self.store.begin(job, job["storyboard"], uuid.uuid4().hex)
        source = self.root / "single.png"
        source.write_bytes(b"single result")
        job.update(status="completed", output=str(source), output_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
        self.store.finish(job)
        run = self.store.read(self.pid)["runs"][-1]
        self.assertEqual(Path(run["export_path"]).read_bytes(), b"single result")
        self.assertTrue((self.store.directory(self.pid) / "results" / run["filename"]).is_file())

    async def test_picker_requires_csrf_and_handles_cancel(self):
        with patch("image_output.choose_folder", return_value="") as picker:
            response = await self.client.post("/api/image-output-folder", json={})
            self.assertEqual(response.status, 403)
            picker.assert_not_called()
            response = await self.client.post("/api/image-output-folder", json={}, headers=self.headers)
            self.assertEqual(await response.json(), {"folder": ""})
            picker.assert_called_once()

    async def test_overrides_are_frozen_without_changing_library(self):
        original = self.store.read(self.pid)
        batch = self.queue.create(self.pid, self.payload)
        item = batch["items"][0]
        self.assertEqual([m["sha256"] for m in item["snapshot"]["mapping"]], [original["assets"][k]["sha256"] for k in ("c", "b")])
        self.assertEqual(self.store.read(self.pid), original)
        original["shots"][0]["action"] = "changed"
        self.store.write(original)
        await self.queue.launch(batch)
        await self.drain()
        self.assertIn("交谈", self.calls[0]["prompt"])
        self.assertNotIn("changed", self.calls[0]["prompt"])
        self.assertEqual([j["seed"] for j in self.calls], [17,17])
        self.assertEqual(self.queue.read(self.pid,batch["id"])["status"],"completed")

    async def test_pause_resume_serial_and_no_duplicate_launch(self):
        batch=self.queue.create(self.pid,self.payload)
        self.block=asyncio.Event()
        await self.queue.launch(batch)
        await asyncio.sleep(0)
        await self.queue.launch(self.queue.read(self.pid,batch["id"]))
        self.assertEqual(len(self.calls),1)
        self.assertIsNotNone(studio.active_gpu_job(self.app))
        response=await self.client.post(f"/api/storyboards/{self.pid}/batches/{batch['id']}/pause",json={},headers=self.headers)
        self.assertEqual(response.status,200)
        self.block.set()
        await self.drain()
        self.assertEqual(batch["status"],"paused")
        self.assertEqual([i["status"] for i in batch["items"]],["completed","queued"])
        await self.queue.launch(self.queue.read(self.pid,batch["id"]))
        await self.drain()
        self.assertEqual(len(self.calls),2)
        self.assertEqual(self.calls[1]["storyboard"]["shot_id"],self.sid2)

    async def test_failure_stops_and_retry_keeps_completed_results(self):
        batch=self.queue.create(self.pid,self.payload)
        self.fail=True
        await self.queue.launch(batch);await self.drain()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(batch["status"],"failed")
        self.fail=False
        await self.queue.launch(batch);await self.drain()
        self.assertEqual(len(self.calls),2)  # resume does not implicitly retry failure
        await self.queue.launch(batch,retry=True);await self.drain()
        self.assertEqual(len(self.calls),3)
        self.assertEqual(batch["status"],"completed")
        self.assertEqual(len(self.store.read(self.pid)["runs"]),3)

    async def test_restart_reconciles_finished_and_does_not_resubmit(self):
        batch=self.queue.create(self.pid,self.payload)
        await self.queue.launch(batch);await self.drain()
        batch["status"]="running"
        batch["items"][0]["status"]="running"
        batch["items"][1].update(status="running",job_id="unknown")
        self.queue.write(batch)
        self.queue.restore()
        restored=self.queue.read(self.pid,batch["id"])
        self.assertEqual(restored["status"],"interrupted")
        self.assertEqual([i["status"] for i in restored["items"]],["completed","interrupted"])
        self.assertEqual(len(self.calls),2)

    async def test_invalid_later_row_rejects_entire_batch(self):
        for change in ({"asset_id":"../outside"},{"target":uuid.uuid4().hex},{"shot_id":uuid.uuid4().hex}):
            payload=copy.deepcopy(self.payload);payload["items"][1].update(change)
            with self.assertRaises(ValueError):self.queue.create(self.pid,payload)
            self.assertFalse(self.queue.path(self.pid,payload["request_id"]).exists())
        self.assertFalse(self.calls)

    async def test_busy_gpu_rejects_start_but_preserves_draft(self):
        batch=self.queue.create(self.pid,self.payload)
        self.app["image_jobs"]["other"]={"id":"other","status":"running"}
        with self.assertRaises(ValueError):await self.queue.launch(batch)
        self.assertEqual(self.queue.read(self.pid,batch["id"])["status"],"paused")

    async def test_creation_idempotency_and_public_api_no_private_paths(self):
        url=f"/api/storyboards/{self.pid}/batches"
        for _ in range(2):
            response=await self.client.post(url,json=self.payload,headers=self.headers)
            self.assertEqual(response.status,201,await response.text())
            data=await response.json()
            self.assertNotIn("references",data["batch"]["items"][0])
        self.assertEqual(len(list(self.queue.path(self.pid,self.payload["request_id"]).parent.glob("*.json"))),1)
        self.assertFalse(self.calls)
        self.assertEqual((await self.client.post(url,json=self.payload)).status,403)

    async def test_guide_override_keeps_identities(self):
        payload=copy.deepcopy(self.payload);payload["items"]=payload["items"][:1];payload["items"][0]["target"]="guide"
        batch=self.queue.create(self.pid,payload)
        mapping=batch["items"][0]["snapshot"]["mapping"]
        self.assertEqual([m.get("role_id") for m in mapping],[self.rid,self.rid2,None])
        self.assertEqual(mapping[-1]["purpose"],"pose_composition")
        self.assertEqual(self.store.read(self.pid)["shots"][0]["guide"],"")

    async def test_legacy_uses_padded_slots_and_legacy_steps(self):
        payload=copy.deepcopy(self.payload);payload["settings"]["image_engine"]="legacy"
        batch=self.queue.create(self.pid,payload)
        await self.queue.launch(batch);await self.drain()
        self.assertTrue(all(len(j["local_reference_slots"])==3 and j["steps"]==12 for j in self.calls))
        self.assertFalse(list(studio.UPLOAD_ROOT.iterdir()))

    async def test_invalid_settings_and_limit(self):
        for settings in ({"seed":"-1"},{"seed":str(2**63)},{"aspect":"invalid"},{"quality":"invalid"},{"image_engine":"invalid"}):
            with self.assertRaises(ValueError):self.queue.create(self.pid,{**self.payload,"settings":settings})
        with self.assertRaises(ValueError):self.queue.create(self.pid,{**self.payload,"items":[]})
        batch=self.queue.create(self.pid,{**self.payload,"items":self.payload["items"]*126})
        self.assertEqual(len(batch['items']),252)
        self.assertFalse(self.calls)


if __name__ == "__main__":
    unittest.main()
