import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
from test_batch_images import studio, photo
from story_sequences import StorySequences


class SequenceFirstFrameTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patches = [patch.object(studio, 'UPLOAD_ROOT', self.root / 'uploads'),
                        patch.object(studio, 'BATCH_CACHE_ROOT', self.root / 'cache')]
        for item in self.patches: item.start()
        self.app = studio.build_app()
        self.app.on_startup.clear()
        self.app.on_cleanup.clear()
        self.app['session'] = Mock()
        self.service = self.app['sequences']
        self.service.root = self.root / 'sequences'
        self.service.root.mkdir()
        self.service.source_root = self.service.root / 'references'
        self.service.plans.clear()
        self.plan = {'id': 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', 'status': 'draft',
                     'title': '首帧测试', 'script': '静物镜头', 'quality': 'draft', 'aspect': 'landscape',
                     'character_lock': '', 'planned_chunks': 1, 'total_chunks': 1,
                     'reference_id': None, 'shots': [{'title': '第一镜头', 'prompt': '静物',
                                                    'status': 'pending', 'job_id': None}]}
        self.service.plans[self.plan['id']] = self.plan
        self.service.save(self.plan)
        self.url = f"/api/sequences/{self.plan['id']}/first-image"
        self.client = TestClient(TestServer(self.app, host='127.0.0.1'))
        await self.client.start_server()
        self.headers = {'X-H3-CSRF': self.app['csrf_token']}

    async def asyncTearDown(self):
        await self.client.close()
        for item in reversed(self.patches): item.stop()
        self.temp.cleanup()

    async def upload(self, payload=None):
        form = FormData()
        form.add_field('image', photo() if payload is None else payload, filename='frame.jpg', content_type='image/jpeg')
        return await self.client.post(self.url, data=form, headers=self.headers)

    async def test_save_preview_restore_and_remove(self):
        self.plan['reference_id'] = 'old-video'
        response = await self.upload(photo() + bytes(26 * 1024 * 1024))
        self.assertEqual(response.status, 200, await response.text())
        info = await response.json()
        self.assertTrue(info['first_image'])
        preview = await self.client.get(info['first_image_url'])
        self.assertEqual(preview.status, 200)
        with Image.open(io.BytesIO(await preview.read())) as image:
            self.assertEqual(image.size, (64, 40))
        restored = StorySequences(self.app, studio, self.service.root)
        restored.restore()
        plan = restored.plans[self.plan['id']]
        self.assertTrue(restored.public(plan)['first_image'])
        self.assertIsNone(plan['reference_id'])
        response = await self.client.post(self.url + '/remove', headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertFalse((await response.json())['first_image'])
        self.assertEqual((await self.client.get(self.url)).status, 404)
        saved = json.loads((self.service.root / f"{self.plan['id']}.json").read_text(encoding='utf-8'))
        self.assertFalse(saved['first_image'])

    async def test_invalid_replacement_and_started_plan_preserve_saved_frame(self):
        self.assertEqual((await self.upload()).status, 200)
        target = self.service.root / f"{self.plan['id']}.png"
        original = target.read_bytes()
        self.assertEqual((await self.upload(b'broken')).status, 400)
        self.assertEqual(target.read_bytes(), original)
        self.plan['shots'][0]['job_id'] = 'submitted-job'
        self.assertEqual((await self.upload()).status, 400)
        self.assertEqual((await self.client.post(self.url + '/remove', headers=self.headers)).status, 400)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual((await self.client.post(self.url + '/remove')).status, 403)

    async def test_first_job_uses_frame_and_later_job_uses_video(self):
        self.assertEqual((await self.upload()).status, 200)
        first = self.service.make_job(self.plan, 0, None)
        self.assertEqual(first['mode'], 'image')
        self.assertTrue(Path(first['local_image']).is_file())
        parent = {'id': 'previous', 'quality': 'draft', 'aspect': 'landscape',
                  'output': 'previous.mp4', 'width': 608, 'height': 352}
        later = self.service.make_job(self.plan, 0, parent)
        self.assertEqual(later['mode'], 'continue')
        self.assertIsNone(later['local_image'])
        self.assertEqual(later['continue_from_job_id'], 'previous')
