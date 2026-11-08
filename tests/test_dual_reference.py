import asyncio
import copy
import hashlib
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import studio
import dual_reference
from sequence_settings import settings
from story_sequences import StorySequences


class DualReferenceTests(unittest.TestCase):
    def test_legacy_default_and_invalid_experimental_presets(self):
        self.assertEqual(settings({},studio.MANIFEST)["continuation_strategy"],"previous")
        for config in ({"quality":"draft"},{"quality":"standard_clipproj"}, {"quality":"standard","steps":12}):
            with self.assertRaises(ValueError):
                settings(dict(config,continuation_strategy="dual_reference"),studio.MANIFEST)

    def test_graph_has_identity_and_tail_without_resetting_pose(self):
        graph,w,h = studio.render_workflow("continue_dual","turn slowly",42,"standard","portrait",
                                           "tail.mp4",steps=20,identity_name="original.png")
        self.assertEqual(graph["1"]["inputs"]["unet_name"],dual_reference.MODEL_NAME)
        self.assertEqual(graph["5"]["class_type"],"MiniMaxH3ReferenceToVideo")
        self.assertEqual(graph["5"]["inputs"]["ref_images.ref_image_0"],["22",0])
        self.assertEqual(graph["5"]["inputs"]["ref_videos.ref_video_0"],["19",0])
        self.assertEqual(graph["5"]["inputs"]["ref_video_audios.ref_video_audio_0"],["19",1])
        self.assertEqual(graph["22"]["inputs"]["image"],"original.png")
        self.assertEqual(graph["18"]["inputs"]["file"],"tail.mp4")
        self.assertEqual(graph["20"]["inputs"]["frame_idx"],0)
        self.assertEqual(graph["9"]["inputs"]["conditioning"],["20",0])
        self.assertNotIn("first_frame",graph["5"]["inputs"])
        self.assertNotIn("17",graph); self.assertNotIn("21",graph)
        self.assertEqual((w,h),(480,864))

    def test_original_workflow_is_unchanged_and_missing_reference_fails(self):
        before = studio.render_workflow("continue","rain",42,"standard","portrait","tail.mp4")
        with self.assertRaises(ValueError):
            studio.render_workflow("continue_dual","rain",42,"standard","portrait","tail.mp4")
        studio.render_workflow("continue_dual","rain",42,"standard","portrait","tail.mp4",identity_name="ref.png")
        self.assertEqual(before,studio.render_workflow("continue","rain",42,"standard","portrait","tail.mp4"))

    def test_each_continuation_reads_same_original_not_latest_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = StorySequences({"jobs":{}},studio,root)
            identifier = str(uuid.uuid4())
            original = root/f"{identifier}.png"; original.write_bytes(b"unchanged identity fixture")
            plan = dict(id=identifier,title="QA",first_image=True,character_lock="",
                        **settings({"quality":"standard","continuation_strategy":"dual_reference"},studio.MANIFEST),
                        shots=[dict(title="one",prompt="turn"),dict(title="two",prompt="smile")])
            parent = dict(id=str(uuid.uuid4()),quality="standard",aspect="portrait",width=480,height=864,
                          steps=20,output="first.mp4",output_sha256="hash")
            first = service.make_job(plan,0,parent)
            second = service.make_job(plan,1,dict(parent,output="second.mp4"))
            self.assertEqual(first["mode"],"continue_dual")
            self.assertIsNone(first["local_image"])
            self.assertEqual(first["identity_image"],second["identity_image"])
            self.assertEqual(first["identity_image"],str(original))
            self.assertEqual(first["overlap_frames"],22)
            imported = service.make_job(plan,0,dict(parent,quality="draft_clipproj",steps=8))
            self.assertEqual((imported["quality"],imported["steps"]),("standard",20))
            self.assertEqual(imported["canvas"],[480,864])
            self.assertEqual(original.read_bytes(),b"unchanged identity fixture")
            plan["first_image"] = False
            with self.assertRaisesRegex(ValueError,"原始角色"):
                service.make_job(plan,0,parent)

    def test_optional_model_hash_check_never_changes_baseline_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Path(tmp)/"models"/"diffusion_models"/dual_reference.MODEL_NAME
            model.parent.mkdir(parents=True); model.write_bytes(b"test")
            with patch.object(dual_reference,"MODEL_SIZE",4), patch.object(dual_reference,"MODEL_SHA256",hashlib.sha256(b"test").hexdigest()):
                self.assertEqual(dual_reference.verify_model(tmp),model)
                self.assertEqual(dual_reference.verify_model(tmp),model)
            self.assertNotIn(dual_reference.MODEL_NAME,str(studio.MODEL_MANIFEST))


class DualReferenceExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_uploads_both_references_and_cleans_only_temporary_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            original=root/"original.png"; original.write_bytes(b"original identity")
            source=root/"previous.mp4"; source.write_bytes(b"previous video")
            identifier=str(uuid.uuid4())
            job=dict(id=identifier,mode="continue_dual",identity_image=str(original),
                     local_image=None,continuation_source=str(source),cancel_event=asyncio.Event(),
                     prompt="turn slowly",seed=1,quality="standard",aspect="portrait",steps=20)
            uploads=AsyncMock(side_effect=[("identity.png",{"name":"identity.png"}),
                                          ("tail.mp4",{"name":"tail.mp4"})])
            captured=[]
            async def execute(app,current,client_id,graph):
                captured.append(graph)
                current["status"]="cancelled"
            def prepare(src,dst):
                dst.write_bytes(b"tail only")
                return {"has_audio":True}
            with patch.object(studio,"UPLOAD_ROOT",root), patch.object(dual_reference,"verify_model"), \
                 patch.object(studio,"upload_to_comfy",uploads), \
                 patch.object(studio,"free_comfy_models",AsyncMock()), \
                 patch.object(studio,"prepare_continuation_clip",side_effect=prepare), \
                 patch.object(studio,"wait_for_execution",side_effect=execute), \
                 patch.object(studio,"remove_comfy_input") as cleanup:
                await studio.run_job({"gpu_lock":asyncio.Lock(),"session":object()},job)
            self.assertEqual(job["status"],"cancelled",job.get("error"))
            self.assertEqual(uploads.await_count,2)
            self.assertEqual(captured[0]["22"]["inputs"]["image"],"identity.png")
            self.assertEqual(captured[0]["18"]["inputs"]["file"],"tail.mp4")
            self.assertEqual(cleanup.call_count,2)
            self.assertFalse((root/f"h3-continuation-{identifier}.mp4").exists())
            self.assertEqual(original.read_bytes(),b"original identity")
            self.assertEqual(source.read_bytes(),b"previous video")


if __name__ == "__main__": unittest.main()
