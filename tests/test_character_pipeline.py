"""No model inference: test dependency routing and export with stubbed images."""
import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
import test_storyboard_batches as base
import character_pipeline
import studio


class CharacterPipelineTests(base.BatchTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.export_patch=patch.object(character_pipeline,'EXPORT_ROOT',self.root/'exports')
        self.export_patch.start()
        p=self.store.read(self.pid)
        import uuid
        for number in (3,4):
            p['shots'].append(dict(id=uuid.uuid4().hex,title=str(number),cast=[self.rid2,self.rid],action='公园散步',guide=''))
        p['character_pipeline']=dict(primary_role_id=self.rid,shot_ids=[s['id'] for s in p['shots']])
        self.store.write(p)
        self.pipeline_payload={**self.payload,'workflow':'character_sequence'}
        self.seen=[]
        self.patch_run.stop()

        async def stub(app,job):
            self.calls.append(job)
            self.seen.append([Path(v).read_bytes() if v else None for v in job['local_reference_slots']])
            if self.block:
                await self.block.wait()
            if self.fail:
                job.update(status='failed',error='test failure')
            else:
                content=job['id'].encode()
                output=self.root/(job['id']+'.png');output.write_bytes(content)
                job.update(status='completed',output=str(output),output_sha256=hashlib.sha256(content).hexdigest())
            self.store.finish(job)
            for value in job['local_reference_slots']:
                if value:Path(value).unlink(missing_ok=True)
        self.patch_run=patch.object(studio,'run_image_job',stub)
        self.patch_run.start()

    async def asyncTearDown(self):
        await super().asyncTearDown()
        self.export_patch.stop()

    async def test_four_acts_per_source_use_own_first_result_and_export_flat(self):
        original=self.store.read(self.pid)
        style_path=self.store.directory(self.pid)/'assets'/'style.png'
        style_path.write_bytes(b'style-only')
        original['assets']['style']={'filename':'style.png','sha256':hashlib.sha256(b'style-only').hexdigest()}
        original['style_asset']='style'
        self.store.write(original)
        batch=self.queue.create(self.pid,self.pipeline_payload)
        self.assertEqual(len(batch['items']),8)
        # Changing the project after submission must not change this batch.
        changed=self.store.read(self.pid);changed['style_asset']='';self.store.save(self.pid,changed)
        await self.queue.launch(batch);await self.drain()
        self.assertEqual(batch['status'],'completed')
        self.assertEqual(len(self.calls),8)
        for job,slots in zip(self.calls,self.seen):
            mapping=job['storyboard']['mapping'][-1]
            self.assertEqual(mapping['purpose'],'style')
            self.assertEqual(slots[mapping['slot']-1],b'style-only')
            self.assertEqual(job['storyboard']['style_asset'],'style')
        for offset,source in ((0,b'c'),(4,b'a')):
            hero=self.calls[offset]['id'].encode()
            self.assertEqual(self.seen[offset][0],source)
            self.assertEqual(self.seen[offset+1][0],hero)
            for idx in (offset+2,offset+3):
                self.assertEqual(self.seen[idx][:2],[b'b',hero])
            for idx in (offset+1,offset+2,offset+3):
                self.assertEqual(self.calls[idx]['storyboard']['processed_character']['source_job_id'],self.calls[offset]['id'])
                self.assertIn('保留处理后的身份、发型和服装',self.calls[idx]['prompt'])
        current=self.store.read(self.pid)
        self.assertEqual(current['characters'],original['characters'])
        self.assertEqual(current['shots'],original['shots'])
        exports=list(character_pipeline.EXPORT_ROOT.glob('*.png'))
        self.assertEqual(len(exports),8)
        self.assertEqual(len(list(character_pipeline.EXPORT_ROOT.glob('*.json'))),8)
        self.assertFalse(any(p.is_dir() for p in character_pipeline.EXPORT_ROOT.iterdir()))
        sidecar=json.loads(exports[-1].with_suffix('.json').read_text(encoding='utf-8'))
        self.assertIn('snapshot',sidecar)

    async def test_first_act_failure_blocks_all_dependent_generation(self):
        batch=self.queue.create(self.pid,self.pipeline_payload)
        self.fail=True
        await self.queue.launch(batch);await self.drain()
        self.assertEqual(len(self.calls),1)
        self.fail=False
        await self.queue.launch(batch);await self.drain()
        self.assertEqual(len(self.calls),1)
        self.assertIn('第一幕未成功',batch['items'][1]['error'])
        await self.queue.launch(batch,retry=True);await self.drain()
        self.assertEqual(batch['status'],'completed')
        self.assertEqual(len(self.calls),9)

    async def test_export_failure_resumes_without_regenerating_first_act(self):
        batch=self.queue.create(self.pid,self.pipeline_payload)
        original=character_pipeline.export_result
        with patch.object(character_pipeline,'export_result',side_effect=OSError('disk full')):
            await self.queue.launch(batch);await self.drain()
        self.assertEqual(batch['status'],'failed')
        self.assertEqual(batch['items'][0]['status'],'completed')
        self.assertEqual(batch['items'][0]['export_error'],'disk full')
        self.assertEqual(len(self.calls),1)
        first_job=batch['items'][0]['job_id']
        await self.queue.launch(batch);await self.drain()
        self.assertEqual(batch['status'],'completed')
        self.assertEqual(len(self.calls),8)
        self.assertEqual(batch['items'][0]['job_id'],first_job)

    async def test_restore_last_completed_image_exports_without_gpu(self):
        batch=self.queue.create(self.pid,self.pipeline_payload)
        await self.queue.launch(batch);await self.drain()
        last=batch['items'][-1]
        Path(last.pop('export_path')).unlink()
        last['status']='running';batch['status']='running'
        self.queue.write(batch);self.queue.restore()
        restored=self.queue.read(self.pid,batch['id'])
        await self.queue.launch(restored);await self.drain()
        self.assertEqual(len(self.calls),8)
        self.assertTrue(Path(restored['items'][-1]['export_path']).is_file())

    async def test_config_requires_four_distinct_shots_and_main_role_in_each(self):
        p=self.store.read(self.pid)
        p['character_pipeline']['shot_ids'][3]=p['character_pipeline']['shot_ids'][0]
        self.store.write(p)
        with self.assertRaises(ValueError):self.queue.create(self.pid,self.pipeline_payload)
        p['character_pipeline']['shot_ids']=[s['id'] for s in p['shots']]
        p['shots'][3]['cast']=[self.rid2]
        self.store.write(p)
        with self.assertRaises(ValueError):self.queue.create(self.pid,self.pipeline_payload)

    async def test_corrupted_hero_is_not_used_by_later_shots(self):
        batch=self.queue.create(self.pid,self.pipeline_payload)
        self.block=asyncio.Event()
        await self.queue.launch(batch);await asyncio.sleep(0)
        batch['pause_requested']=True;self.block.set();await self.drain()
        first=batch['items'][0]
        path,_=character_pipeline.archived_result(self.store,batch,first)
        path.write_bytes(b'modified')
        await self.queue.launch(batch);await self.drain()
        self.assertEqual(len(self.calls),1)
        self.assertEqual(batch['status'],'failed')

    async def test_export_never_overwrites_an_existing_different_file(self):
        batch=self.queue.create(self.pid,self.pipeline_payload)
        await self.queue.launch(batch);await self.drain()
        first=batch['items'][0];target=Path(first['export_path'])
        target.write_bytes(b'user replacement')
        with self.assertRaises(ValueError):character_pipeline.export_result(self.store,batch,first)
        self.assertEqual(target.read_bytes(),b'user replacement')
