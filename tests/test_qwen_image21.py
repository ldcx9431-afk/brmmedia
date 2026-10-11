import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
import tempfile
import threading
import sys
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location('qwen21', Path(__file__).resolve().parents[1] / 'ubuntu-backend-deploy/qwen_image21_support.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

class QwenImage21Test(unittest.TestCase):
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
        self.assertEqual(events[-1]['current_step'],40)
        self.assertEqual(events[-1]['progress'],1.0)
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
