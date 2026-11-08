import copy
import json
import sys
import tempfile
import unittest
import asyncio
import io
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from storyboards import Storyboards
from qwen21_backend import build_qwen21
from image_backend import build_edit_request
from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
from unittest.mock import patch, AsyncMock, Mock
import studio


class StoryboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.studio = SimpleNamespace(ACTIVE_STATUSES={'starting', 'running'})
        self.store = Storyboards({'image_jobs': {}}, self.studio, self.temp.name)
        self.project = self.store.create('多人测试')
        self.pid = self.project['id']
        self.a, self.b, self.sid = [uuid.uuid4().hex for _ in range(3)]
        directory = self.store.directory(self.pid) / 'assets'
        directory.mkdir()
        (directory / 'a.png').write_bytes(b'original-identity-A')
        self.project['assets']['asset_a'] = {'filename':'a.png', 'sha256':'identity-a'}
        self.store.write(self.project)
        self.project.update(characters=[dict(id=self.a,name='A',description='',asset='asset_a'),
            dict(id=self.b,name='B',description='短发、穿蓝色外套的成年男子',asset='')],
            shots=[dict(id=self.sid,title='相遇',cast=[self.a,self.b],action='A 坐着，B 站在左侧递水。',guide='')])
        self.project = self.store.save(self.pid, self.project)

    def tearDown(self):
        self.temp.cleanup()

    def test_one_photo_adds_distinct_text_character(self):
        result = self.store.prepare(self.pid,self.sid)
        self.assertEqual(len(result['references']),1)
        self.assertIn('主要人物共 2 人',result['prompt'])
        self.assertIn('<image1>',result['prompt'])
        self.assertIn('没有照片',result['prompt'])
        self.assertEqual(result['mapping'][0]['role_id'],self.a)

    def test_style_reference_persists_separately_and_reaches_every_shot(self):
        p=self.store.read(self.pid)
        p['assets']['style']={'filename':'style.png','sha256':'style-hash'}
        p['assets']['pose']={'filename':'pose.png','sha256':'pose-hash'}
        self.store.write(p)
        p['style_asset']='style'
        p['shots'][0]['guide']='pose'
        extra=copy.deepcopy(p['shots'][0]);extra['id']=uuid.uuid4().hex;p['shots'].append(extra)
        p=self.store.save(self.pid,p)
        for shot in p['shots']:
            result=self.store.prepare(self.pid,shot['id'])
            self.assertEqual([r.name for r in result['references']],['a.png','pose.png','style.png'])
            self.assertEqual(result['mapping'][0]['role_id'],self.a)
            self.assertEqual([m.get('purpose') for m in result['mapping']],[None,'pose_composition','style'])
            self.assertIn('<image3> 是整个项目共用的画风参考',result['prompt'])
            self.assertIn('不使用其中人物的身份',result['prompt'])
            self.assertEqual(result['style_asset'],'style')
        # Older clients omit the field; omission must not erase the style.
        del p['style_asset'];p=self.store.save(self.pid,p)
        self.assertEqual(p['style_asset'],'style')
        p['style_asset']='';self.store.save(self.pid,p)
        self.assertEqual(len(self.store.prepare(self.pid,self.sid)['references']),2)
        self.assertEqual(self.store.read(self.pid)['characters'][0]['asset'],'asset_a')

    def test_style_rejects_foreign_assets_and_cannot_replace_identity(self):
        original=self.store.read(self.pid)
        for value in ('foreign',None,[],{}):
            with self.assertRaisesRegex(ValueError,'画风参考图'):
                self.store.save(self.pid,{**original,'style_asset':value})
        self.assertEqual(self.store.read(self.pid),original)
        p=copy.deepcopy(original);p['style_asset']='asset_a';p['characters'][0].update(asset='',description='成年人物')
        self.store.save(self.pid,p)
        with self.assertRaisesRegex(ValueError,'不能代替身份参考'):
            self.store.prepare(self.pid,self.sid)

    def test_style_counts_towards_both_engine_reference_limits(self):
        p=self.store.read(self.pid)
        p['characters']=[dict(id=uuid.uuid4().hex,name=str(i),asset='asset_a',description='') for i in range(8)]
        p['shots'][0].update(cast=[r['id'] for r in p['characters']],guide='asset_a')
        p['style_asset']='asset_a'
        p=self.store.save(self.pid,p)
        result=self.store.prepare(self.pid,self.sid)
        self.assertEqual(len(result['references']),10)
        graph=build_qwen21(result['prompt'],[f'{i}.png' for i in range(10)],seed=42,canvas=(896,512))
        self.assertEqual(graph['4']['inputs']['images.image_10'],['30',0])
        with self.assertRaisesRegex(ValueError,'最多 3 张参考图'):
            self.store.prepare(self.pid,self.sid,'legacy')
        p['shots'][0]['cast']=p['shots'][0]['cast'][:1];self.store.save(self.pid,p)
        self.assertEqual(len(self.store.prepare(self.pid,self.sid,'legacy')['references']),3)

    def test_new_engine_keeps_four_distinct_inputs(self):
        for i in range(3):
            role=copy.deepcopy(self.project['characters'][0]);role.update(id=uuid.uuid4().hex,name=f'新增{i}')
            self.project['characters'].append(role);self.project['shots'][0]['cast'].append(role['id'])
        self.store.save(self.pid,self.project)
        result=self.store.prepare(self.pid,self.sid)
        self.assertEqual(len(result['references']),4)
        with self.assertRaises(ValueError): self.store.prepare(self.pid,self.sid,'legacy')
        graph=build_qwen21(result['prompt'],['a.png','b.png','c.png','d.png'],seed=42,canvas=(896,512))
        self.assertEqual(graph['4']['inputs']['images.image_4'],['24',0])
        self.assertEqual(graph['6']['inputs']['cfg'],1)
        self.assertEqual(graph['9']['inputs']['width'],896)

    def test_legacy_storyboard_does_not_force_single_person(self):
        graph,metadata=build_edit_request('A 与 B 坐在一起',['a.png','b.png'],intent='storyboard',canvas=(896,512))
        self.assertNotIn('不要拼贴或生成多人',graph['8']['inputs']['prompt'])
        self.assertNotIn('只补充同一人物',graph['8']['inputs']['prompt'])
        self.assertEqual(graph['12']['class_type'],'EmptySD3LatentImage')

    def test_stale_save_rejected_and_paths_confined(self):
        with self.assertRaises(ValueError): self.store.save(self.pid,{**self.project,'revision':0})
        with self.assertRaises(ValueError): self.store.read('../outside')
        bad=copy.deepcopy(self.project);bad['characters'][0]['asset']='missing'
        with self.assertRaises(ValueError): self.store.save(self.pid,bad)

    def test_run_snapshot_frozen_and_no_auto_resubmit(self):
        snapshot=self.store.prepare(self.pid,self.sid)
        job={'id':str(uuid.uuid4()),'seed':42,'steps':25,'image_engine':'qwen21','status':'starting','storyboard':snapshot}
        request=uuid.uuid4().hex
        self.store.begin(job,snapshot,request)
        with self.assertRaises(ValueError): self.store.begin(job,snapshot,request)
        p=self.store.read(self.pid);p['shots'][0]['action']='下一镜的新动作';self.store.save(self.pid,p)
        restarted=Storyboards({'image_jobs':{}},self.studio,self.temp.name)
        restarted.restore()
        p=restarted.read(self.pid)
        self.assertEqual(p['runs'][0]['status'],'interrupted')
        self.assertEqual(p['runs'][0]['snapshot']['shot']['action'],'A 坐着，B 站在左侧递水。')
        self.assertEqual((self.store.directory(self.pid)/'assets/a.png').read_bytes(),b'original-identity-A')

    def test_multiple_results_preserve_versions(self):
        snapshot=self.store.prepare(self.pid,self.sid)
        source=Path(self.temp.name)/'test.png';source.write_bytes(b'output')
        for i in range(2):
            job=dict(id=str(uuid.uuid4()),seed=i,steps=25,image_engine='qwen21',status='completed',storyboard=snapshot,
                     output=str(source),output_sha256='output-sha')
            self.store.begin(job,snapshot,uuid.uuid4().hex);self.store.finish(job)
        runs=self.store.read(self.pid)['runs']
        self.assertEqual(len(runs),2)
        self.assertNotEqual(runs[0]['image_url'],runs[1]['image_url'])
        self.assertEqual(len(list((self.store.directory(self.pid)/'results').glob('*.png'))),2)


class StoryboardApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_optimizer_uses_saved_cast_and_leaves_original_untouched(self):
        p=self.app['storyboards'].create('optimizer test')
        aid,bid,sid=[uuid.uuid4().hex for _ in range(3)]
        p['characters']=[dict(id=aid,name='甲',description='绿衣',asset=''),dict(id=bid,name='乙',description='米色衣',asset='')]
        p['shots']=[dict(id=sid,title='递水',cast=[bid,aid],action='B 递水给 A',guide='')]
        p=self.app['storyboards'].save(p['id'],p)
        manager=Mock();manager.optimize.return_value={'image_prompt':'角色 B 将杯子递给角色 A','warnings':[]}
        self.app['prompt_llm']=manager;self.app['image_backend']=SimpleNamespace(free=AsyncMock())
        self.app['session']=None
        with patch.object(studio,'prompt_install_state',return_value={'profiles':{'stable':{'installed':True}}}), patch.object(studio,'free_comfy_models',new=AsyncMock()):
            response=await self.client.post('/api/optimize-prompt',json={'mode':'storyboard','project_id':p['id'],'shot_id':sid,'aspect':'portrait','prompt':'ignore this'},headers=self.headers)
        self.assertEqual(response.status,200,await response.text())
        context=json.loads(manager.optimize.call_args.args[1])
        self.assertEqual(context['角色表'][0]['名称'],'乙')
        self.assertEqual(context['画面方向'],'竖屏')
        self.assertEqual(context['原始动作'],'B 递水给 A')
        self.assertEqual(self.app['storyboards'].read(p['id'])['shots'][0]['action'],'B 递水给 A')
        self.assertFalse(self.app['submission_state']['reserved'])

    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.app=studio.build_app()
        self.app.on_startup.clear();self.app.on_cleanup.clear()
        self.app['storyboards']=Storyboards(self.app,studio,self.root/'boards')
        # Existing bound route handlers share the same store path for this test.
        for route in self.app.router.routes():
            owner=getattr(route.handler,'__self__',None)
            if isinstance(owner,Storyboards):owner.root=self.root/'boards'
        self.upload_patch=patch.object(studio,'UPLOAD_ROOT',self.root/'uploads');self.upload_patch.start()
        studio.UPLOAD_ROOT.mkdir()
        async def no_inference(app,job): pass
        self.job_patch=patch.object(studio,'run_image_job',no_inference);self.job_patch.start()
        self.client=TestClient(TestServer(self.app));await self.client.start_server()
        self.headers={'X-H3-CSRF':self.app['csrf_token']}

    async def asyncTearDown(self):
        if self.app['tasks']:await asyncio.gather(*self.app['tasks'])
        await self.client.close();self.job_patch.stop();self.upload_patch.stop();self.temp.cleanup()

    async def test_upload_persist_and_submit_four_identity_references(self):
        response=await self.client.post('/api/storyboards',json={'title':'API test'},headers=self.headers)
        project=(await response.json())['project'];pid=project['id']
        image=io.BytesIO();Image.new('RGB',(64,96),'navy').save(image,format='PNG')
        form=FormData();form.add_field('image',image.getvalue(),filename='reference.png',content_type='image/png')
        response=await self.client.post(f'/api/storyboards/{pid}/assets',data=form,headers=self.headers)
        self.assertEqual(response.status,200,await response.text())
        asset=(await response.json())['asset']
        project['characters']=[dict(id=uuid.uuid4().hex,name=f'角色{i}',description='',asset=asset['id']) for i in range(4)]
        sid=uuid.uuid4().hex;project['shots']=[dict(id=sid,title='四人合影',cast=[r['id'] for r in project['characters']],action='四个人坐在一起交谈',guide='')]
        response=await self.client.post(f'/api/storyboards/{pid}',json=project,headers=self.headers)
        self.assertEqual(response.status,200,await response.text())
        fields=dict(mode='storyboard',storyboard_project_id=pid,storyboard_shot_id=sid,storyboard_request_id=uuid.uuid4().hex,image_engine='qwen21',aspect='landscape')
        form=FormData(default_to_multipart=True)
        for key,value in fields.items():form.add_field(key,value)
        response=await self.client.post('/api/images/generate',data=form,headers=self.headers)
        self.assertEqual(response.status,202,await response.text())
        job=self.app['image_jobs'][(await response.json())['job']['id']]
        self.assertEqual(len(job['local_reference_slots']),4)
        self.assertEqual(job['steps'],25)
        self.assertIn('<image4>',job['prompt'])
        self.assertEqual(job['reference_intent'],'storyboard')
        response=await self.client.get(f'/api/storyboards/{pid}')
        self.assertEqual((await response.json())['project']['runs'][0]['status'],'starting')


if __name__=='__main__': unittest.main()
