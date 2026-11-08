import asyncio
import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import studio
from script_planner import planning_batches, build_shot_payload, validate_shots
from sequence_settings import settings, fit_first_frame
from story_sequences import StorySequences


class SettingsTests(unittest.TestCase):
    def test_turbo_and_standard_steps(self):
        self.assertEqual(settings({}, studio.MANIFEST)["image_fit"], "contain")
        for bad in ({"steps": 30}, {"target_shots": True}, {"target_shots": 1001}, {"aspect": "bad"}):
            with self.assertRaises(ValueError): settings(bad, studio.MANIFEST)
        config = settings({"quality":"standard_clipproj", "steps":30}, studio.MANIFEST)
        graph, _, _ = studio.render_workflow("text", "rain", 1, config["quality"], "portrait", None, steps=config["steps"])
        self.assertEqual(graph["8"]["inputs"]["steps"], 30)
        self.assertIn("21", graph)

    def test_many_shots_small_requests_and_full_text_coverage(self):
        script = "她走进花园，看向花朵。\n她停下来观察蝴蝶。"
        for target in (12, 24, 48, 96, 1000):
            batches = planning_batches(script, target)
            self.assertEqual(sum(count for _, count in batches), target)
            self.assertTrue(all(1 <= count <= 6 for _, count in batches))
            by_id = {unit["id"]:unit["text"] for units, _ in batches for unit in units}
            self.assertEqual("".join(by_id[i] for i in sorted(by_id)), script.replace("\n", ""))
        units, count = planning_batches(script, 12)[0]
        payload = build_shot_payload(units, "", [], count)
        self.assertEqual(payload["format"]["properties"]["shots"]["maxItems"], count)
        with self.assertRaises(ValueError): validate_shots({"shots":[]}, units, count)

    def test_contain_keeps_top_bottom_markers_without_modifying_original(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "portrait.png"
            im = Image.new("RGB", (100, 200), "white")
            im.paste("red", (0,0,100,40)); im.paste("blue", (0,160,100,200)); im.save(source)
            before = source.read_bytes()
            output = fit_first_frame(source, (200,100), "contain")
            self.assertEqual(output.size, (200,100))
            self.assertEqual(output.getpixel((100,5)), (255,0,0))
            self.assertEqual(output.getpixel((100,95)), (0,0,255))
            self.assertEqual(output.getpixel((0,50)), (0,0,0))
            cropped = fit_first_frame(source, (200,100), "crop")
            self.assertEqual(cropped.getpixel((100,5)), (255,255,255))
            self.assertEqual(source.read_bytes(), before)


class SequenceSettingsHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(studio, "RESULT_ROOT", self.root / "results"),
                        patch.object(studio, "UPLOAD_ROOT", self.root / "uploads")]
        for p in self.patches: p.start()
        studio.RESULT_ROOT.mkdir(); studio.UPLOAD_ROOT.mkdir()
        self.app = studio.build_app()
        self.app.on_startup.clear(); self.app.on_cleanup.clear()
        self.app["session"] = Mock()
        self.s = self.app["sequences"]
        self.s.root = self.root / "sequences"; self.s.root.mkdir()
        self.s.source_root = self.s.root / "references"
        self.plan = dict(id=str(uuid.uuid4()), title="无裁切测试", script="雨后花园", character_lock="",
                         model_profile="stable", **settings({}, studio.MANIFEST), status="draft", error=None,
                         planned_chunks=1, total_chunks=1, current_job_id=None, reference_id=None, created_at=1,
                         first_image=True, shots=[dict(title="看花",prompt="看花",source_ids=[1],source_text="雨后花园",status="pending",job_id=None)])
        self.s.plans[self.plan["id"]] = self.plan
        self.s.save(self.plan)
        Image.new("RGB", (108,135), "red").save(self.s.root / f"{self.plan['id']}.png")
        self.client = TestClient(TestServer(self.app, host="127.0.0.1")); await self.client.start_server()
        self.headers = {"X-H3-CSRF":self.app["csrf_token"]}
        self.url = f"/api/sequences/{self.plan['id']}"

    async def asyncTearDown(self):
        await self.client.close()
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()

    async def post(self, endpoint, data=None):
        return await self.client.post(self.url+endpoint, json=data or {}, headers=self.headers)

    async def test_config_preview_actual_first_frame_and_continuation(self):
        config = settings({"quality":"standard_clipproj","steps":30}, studio.MANIFEST)
        result = await self.post("/settings",config)
        self.assertEqual(result.status,200,await result.text())
        first = self.s.make_job(self.plan,0,None)
        self.assertEqual(first["aspect"],"portrait"); self.assertEqual(first["steps"],30)
        with Image.open(first["local_image"]) as im: self.assertEqual(im.size,(480,864))
        preview = await self.client.get(self.url+"/framing-preview")
        self.assertEqual(preview.status,200)
        self.assertEqual(await preview.read(),Path(first["local_image"]).read_bytes())
        parent = dict(first,output="previous.mp4",width=480,height=864)
        second = self.s.make_job(self.plan,0,parent)
        self.assertEqual(second["steps"],30); self.assertEqual(second["canvas"],[480,864])
        self.assertEqual(second["mode"],"continue")

    async def test_started_plan_requires_copy_and_keeps_original(self):
        self.plan["shots"][0].update(job_id=str(uuid.uuid4()),status="completed",result={"filename":"old.mp4"})
        self.plan["merge"] = {"status":"completed", "filename":"previous-full.mp4"}
        original = copy.deepcopy(self.plan)
        config = settings({"quality":"standard","steps":30},studio.MANIFEST)
        self.assertEqual((await self.post("/settings",config)).status,400)
        response = await self.post("/copy",config)
        self.assertEqual(response.status,200,await response.text())
        new = (await response.json())["sequence"]
        self.assertNotEqual(new["id"],self.plan["id"])
        self.assertEqual(new["steps"],30); self.assertIsNone(new["shots"][0]["job_id"])
        self.assertIsNone(new["merge"]["status"])
        self.assertEqual(self.plan,original)
        self.assertTrue((self.s.root / f"{new['id']}.png").is_file())

    async def test_dual_strategy_requires_new_copy_after_generation(self):
        self.plan["shots"][0].update(job_id=str(uuid.uuid4()),status="completed")
        config=settings({"quality":"standard","continuation_strategy":"dual_reference"},studio.MANIFEST)
        self.assertEqual((await self.post("/settings",config)).status,400)
        response=await self.post("/copy",config)
        self.assertEqual(response.status,200)
        new=(await response.json())["sequence"]
        self.assertEqual(new["continuation_strategy"],"dual_reference")
        self.assertEqual(self.plan["continuation_strategy"],"previous")
        self.assertTrue(new["first_image"])

    async def test_copy_long_title_with_separator_at_truncation_boundary(self):
        self.plan["title"]="功能验证：连续视频去重拼接（仅复用测试片）"
        self.s.save(self.plan)
        config=settings({"quality":"standard","continuation_strategy":"dual_reference"},studio.MANIFEST)
        response=await self.post("/copy",config)
        self.assertEqual(response.status,200,await response.text())
        new=(await response.json())["sequence"]
        self.assertEqual((await self.client.get(f"/api/sequences/{new['id']}" )).status,200)
        self.assertTrue(Path(new["output_folder"]).name.endswith(uuid.UUID(new["id"]).hex))

    async def test_replan_creates_another_plan_and_never_generates_video(self):
        config = settings({"target_shots":24},studio.MANIFEST)
        def skip_spawn(coro, name): coro.close()
        with patch.object(self.s,"spawn",side_effect=skip_spawn):
            response = await self.post("/copy",dict(config,replan=True))
        self.assertEqual(response.status,202,await response.text())
        new = (await response.json())["sequence"]
        self.assertEqual(new["target_shots"],24); self.assertGreater(new["total_chunks"],1)
        self.assertFalse(new["shots"]); self.assertEqual(len(self.plan["shots"]),1)
        self.assertFalse(self.app["jobs"])

    async def test_group_collection_keeps_original_and_restores_new_grouped_job(self):
        job_id = str(uuid.uuid4())
        source = studio.RESULT_ROOT / "old.mp4"; source.write_bytes(b"original-video")
        job = dict(id=job_id,status="completed",output=str(source),filename=source.name,width=608,height=352,
                   quality="draft",aspect="landscape",seed=1,media={"verified":True},output_sha256="sha",completed_at=2,
                   video_url="/results/old.mp4")
        self.app["jobs"][job_id] = job
        self.plan["shots"][0].update(status="completed",job_id=job_id,result=studio.public_job(job))
        result = await self.post("/collect")
        self.assertEqual(result.status,200,await result.text())
        folder = Path((await result.json())["sequence"]["output_folder"])
        self.assertEqual((folder/source.name).read_bytes(),source.read_bytes())
        self.assertTrue((folder/"首图.png").is_file()); self.assertTrue((folder/"镜头表.json").is_file())
        self.assertEqual((await self.client.get(self.url+"/videos/old.mp4")).status,200)
        self.assertEqual((await self.client.get(self.url+"/videos/首图.png")).status,404)
        self.assertEqual((await self.post("/collect")).status,200)
        # A newly generated grouped sidecar restores its custom step count and link.
        output = folder/"new.mp4"; output.write_bytes(b"new-video")
        new_id = str(uuid.uuid4())
        meta = dict(job,prompt_id=new_id,output=str(output),sequence_id=self.plan["id"],
                    output_subdir=self.plan["output_subdir"],steps=30)
        studio.write_result_metadata(output,meta)
        restored = StorySequences(self.app,studio,self.s.root); restored.restore()
        self.assertEqual(self.app["jobs"][new_id]["steps"],30)
        self.assertEqual(self.app["jobs"][new_id]["video_url"],self.url+"/videos/new.mp4")

    async def test_boundary_and_csrf(self):
        self.assertEqual((await self.client.post(self.url+"/settings",json={})).status,403)
        self.assertEqual((await self.client.post(self.url+"/merge",json={})).status,403)
        self.assertEqual((await self.post("/merge")).status,400)
        self.assertEqual((await self.client.get(self.url+"/merged-video")).status,404)
        for value in ("剧本连续/../escape", "剧本连续/wrong", "剧本连续/x/extra"):
            with self.assertRaises(ValueError): studio.video_result_directory(dict(self.plan,output_subdir=value))
        self.assertEqual((await self.post("/settings",{"output_subdir":"bad"})).status,400)

    async def test_merged_download_is_bound_to_this_plan_folder(self):
        self.s.collect_outputs(self.plan)
        folder = studio.video_result_directory(self.plan)
        (folder/"full.mp4").write_bytes(b"master")
        self.plan["merge"] = {"status":"completed", "filename":"full.mp4"}
        response = await self.client.get(self.url+"/merged-video")
        self.assertEqual(response.status,200)
        self.assertEqual(await response.read(),b"master")
        self.plan["merge"]["filename"] = "../../full.mp4"
        self.assertEqual((await self.client.get(self.url+"/merged-video")).status,404)


if __name__ == "__main__": unittest.main()
