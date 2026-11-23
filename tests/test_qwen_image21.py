import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch, Mock
import tempfile
import threading
import sys
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location('qwen21', Path(__file__).resolve().parents[1] / 'ubuntu-backend-deploy/qwen_image21_support.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class QwenImage21Test(unittest.TestCase):
    def test_fullhd_generates_at_aligned_resolution_and_crops_exact_output(self):
        for width,height in ((1920,1080),(1080,1920)):
            for editing in (False,True):
                with self.subTest(width=width,editing=editing):
                    args=m.validate_edit('编辑',width,height,40,42,'ref.png') if editing else m.validate('生成',width,height,40,42)
                    graph=m.build_workflow(args)
                    size=graph['10' if editing else '5']['inputs']
                    self.assertEqual(size['width'],(width+31)//32*32)
                    self.assertEqual(size['height'],(height+31)//32*32)
                    crop=graph['11']['inputs']
                    self.assertEqual((crop['width'],crop['height']),(width,height))
                    self.assertEqual((crop['x'],crop['y']),(4,0) if width==1080 else (0,4))
                    self.assertEqual(graph['8']['inputs']['images'],['11',0])
        for width,height in ((1920,1088),(1088,1920),(1536,1536)):
            with self.assertRaises(ValueError):m.validate('超出范围',width,height,40,42)

    def test_edit_uses_reference_in_both_vision_and_vae_and_matching_latent(self):
        graph = m.build_workflow(m.validate_edit('把茶壶改成蓝色',1344,768,40,42,'reference.png'))
        self.assertEqual(graph['4']['inputs']['images.image_1'],['10',0])
        self.assertEqual(graph['4']['inputs']['vae'],['3',0])
        self.assertEqual(graph['4']['inputs']['resolution'],0)
        self.assertEqual(graph['6']['inputs']['latent_image'],['4',2])
        self.assertEqual((graph['10']['inputs']['width'],graph['10']['inputs']['height']),(1344,768))
        self.assertNotIn('5',graph)

    def test_edit_rejects_missing_or_external_reference_paths(self):
        for path in ('',None,'../private.png','/etc/private.png','C:/private.png','a\\b.png'):
            with self.subTest(path=path),self.assertRaises(ValueError):
                m.validate_edit('编辑',1024,1024,40,0,path)

    def test_cancel_is_targeted_and_deletes_pending_first(self):
        session=Mock()
        m.cancel_prompt(session,'owned')
        self.assertEqual(session.post.call_args_list[0].kwargs['json'],{'delete':['owned']})
        self.assertEqual(session.post.call_args_list[1].kwargs['json'],{'prompt_id':'owned'})
        self.assertEqual(session.post.return_value.raise_for_status.call_count,2)

    def test_cleanup_does_not_hide_original_inference_error(self):
        response=Mock()
        response.json.return_value={'old':{'status':{'status_str':'error','messages':['original OOM']}}}
        session=Mock()
        session.__enter__=Mock(return_value=session)
        session.__exit__=Mock(return_value=False)
        session.get.return_value=response
        socket=SimpleNamespace(create_connection=Mock(side_effect=RuntimeError('no websocket')))
        task=SimpleNamespace(prompt_id='old',args={},name='error',cancel_event=threading.Event())
        with tempfile.TemporaryDirectory() as root, patch.object(m,'BASE','http://127.0.0.1:8190'), patch.object(m,'release_idle',side_effect=[None,RuntimeError('cleanup failed')]), patch.object(m,'_session',return_value=session), patch.dict(sys.modules,{'websocket':socket}), patch.object(m,'cancel_prompt') as cancel:
            with self.assertRaisesRegex(RuntimeError,'original OOM'):
                m.run_task(task,root,'http://127.0.0.1:8188',lambda e:None)
            cancel.assert_called_once_with(session,'old')

    def test_successful_task_cannot_hide_failed_gpu_handover(self):
        response=Mock(content=b'png')
        response.json.return_value={'old':{'status':{'status_str':'success'},'outputs':{'8':{'images':[{'filename':'old.png'}]}}}}
        session=Mock()
        session.__enter__=Mock(return_value=session)
        session.__exit__=Mock(return_value=False)
        session.get.return_value=response
        socket=SimpleNamespace(create_connection=Mock(side_effect=RuntimeError('no websocket')))
        task=SimpleNamespace(prompt_id='old',args={},name='ok',cancel_event=threading.Event())
        with tempfile.TemporaryDirectory() as root, patch.object(m,'BASE','http://127.0.0.1:8190'), patch.object(m,'release_idle',side_effect=[None,RuntimeError('GPU handover failed')]), patch.object(m,'_session',return_value=session), patch.dict(sys.modules,{'websocket':socket}):
            with self.assertRaisesRegex(RuntimeError,'GPU handover failed'):
                m.run_task(task,root,'http://127.0.0.1:8188',lambda e:None)

    def test_single_instance_skips_redundant_release_idle(self):
        response=Mock(content=b'png')
        response.json.return_value={'old':{'status':{'status_str':'success'},'outputs':{'8':{'images':[{'filename':'old.png'}]}}}}
        session=Mock()
        session.__enter__=Mock(return_value=session)
        session.__exit__=Mock(return_value=False)
        session.get.return_value=response
        socket=SimpleNamespace(create_connection=Mock(side_effect=RuntimeError('no websocket')))
        task=SimpleNamespace(prompt_id='old',args={},name='ok',cancel_event=threading.Event())
        with tempfile.TemporaryDirectory() as root, patch.object(m,'BASE','http://127.0.0.1:8188'), patch.object(m,'release_idle') as rel, patch.object(m,'_session',return_value=session), patch.dict(sys.modules,{'websocket':socket}):
            paths = m.run_task(task,root,'http://127.0.0.1:8188',lambda e:None)
            self.assertEqual(len(paths), 1)
            rel.assert_not_called()

    def test_recovery_uses_existing_prompt_without_resubmission(self):
        class Response:
            content=b'test-artifact'
            def raise_for_status(self): pass
            def json(self): return {'old':{'status':{'status_str':'success'},'outputs':{'8':{'images':[{'filename':'old.png','subfolder':'','type':'output'}]}}}}
        class Session:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def get(self,*args,**kwargs): return Response()
            def post(self,*args,**kwargs): raise AssertionError('Recovery must not resubmit')
        socket=SimpleNamespace(create_connection=lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('no socket')))
        task=SimpleNamespace(prompt_id='old',args={},name='recovered',cancel_event=threading.Event())
        events=[]
        with tempfile.TemporaryDirectory() as root, patch.object(m,'release_idle'), patch.object(m,'_session',return_value=Session()), patch.dict(sys.modules,{'websocket':socket}):
            paths=m.run_task(task,root,'http://127.0.0.1:8188',events.append)
            self.assertEqual(len(paths),1)
            self.assertEqual(Path(paths[0]).read_bytes(),b'test-artifact')
        self.assertEqual(events[0]['prompt_id'],'old')

    def test_progress_drains_step_backlog(self):
        import json
        class Socket:
            events=iter([json.dumps({'type':'progress','data':{'prompt_id':'p','value':i,'max':40}}) for i in range(1,41)])
            def recv(self): return next(self.events)
        events=[]
        m.read_progress(Socket(),'p',events.append)
        self.assertEqual(len(events),1)
        self.assertEqual(events[-1]['current_step'],40)
        self.assertEqual(events[-1]['progress'],1.0)

    def test_progress_ignores_other_prompts_and_malformed_events(self):
        import json
        class Socket:
            events=iter(['not json', b'preview', json.dumps({'type':'progress','data':{'prompt_id':'other','value':40,'max':40}}), json.dumps({'type':'progress','data':None})])
            def recv(self): return next(self.events)
        events=[]
        m.read_progress(Socket(),'p',events.append)
        self.assertEqual(events,[])

    def test_progress_callback_error_is_not_silently_swallowed(self):
        import json
        class Socket:
            events=iter([json.dumps({'type':'progress','data':{'prompt_id':'p','value':1,'max':40}})])
            def recv(self): return next(self.events)
        def failed(event): raise ValueError('history write failed')
        with self.assertRaisesRegex(ValueError,'history write failed'):
            m.read_progress(Socket(),'p',failed)
    def test_no_gpu_release_for_busy_runtime(self):
        class Response:
            def raise_for_status(self): pass
            def json(self): return {'queue_running': [[1]], 'queue_pending': []}
        class Session:
            called=False
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def get(self,*args,**kwargs): return Response()
            def post(self,*args,**kwargs): self.called=True
        session=Session()
        with patch.object(m,'_session',return_value=session), self.assertRaises(RuntimeError):
            m.release_idle('http://127.0.0.1:8190')
        self.assertFalse(session.called)

    def test_graph(self):
        a=m.validate('研究图片',1024,1024,40,0)
        graph=m.build_workflow(a)
        self.assertEqual(graph['6']['inputs']['cfg'],1.0)
        self.assertEqual(graph['2']['inputs']['type'],'qwen_image')
        self.assertIn('int8',graph['1']['inputs']['unet_name'])
        self.assertNotIn('turbo',graph['1']['inputs']['unet_name'])
        self.assertEqual(graph['6']['inputs']['steps'],40)

    def test_limits(self):
        for values in [('',1024,1024,40,0),('x',2048,2048,40,0),('x',513,512,40,0),('x',1024,1024,41,0),('x',512,512,20,-1),('x',512,512,True,0)]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                m.validate(*values)

if __name__ == '__main__':
    unittest.main()
