"""Reject stale browser event indexes before Gradio dispatches a callback."""
import json
import logging


def event_matches(payload, functions):
    index=payload.get('fn_index')
    if not isinstance(index,int) or isinstance(index,bool):
        return True  # Named REST calls do not carry browser event indexes.
    function=functions.get(index)
    if function is None:
        return False
    if payload.get('trigger_id') is None and all(target[1]=='load' for target in function.targets):
        return True  # Gradio serializes root component id 0 as null on load.
    targets=[target[0] for target in function.targets if target[0] is not None]
    return not targets or payload.get('trigger_id') in targets


def middleware_class(blocks):
    class BrowserEventGuard:
        def __init__(self,app):
            self.app=app
            self.reported=set()

        async def __call__(self,scope,receive,send):
            path=scope.get('path','')
            protected=path.endswith('/queue/join') or '/gradio_api/run/' in path
            if scope['type']!='http' or scope.get('method')!='POST' or not protected:
                return await self.app(scope,receive,send)
            chunks=[]
            while True:
                message=await receive()
                if message['type']=='http.disconnect':return
                chunks.append(message.get('body',b''))
                if not message.get('more_body',False):break
            body=b''.join(chunks)
            try:payload=json.loads(body)
            except (ValueError,UnicodeDecodeError):payload={}
            if isinstance(payload,dict) and not event_matches(payload,blocks.fns):
                key=(payload.get('fn_index'),str(payload.get('trigger_id')))
                if key not in self.reported and len(self.reported)<1024:
                    self.reported.add(key)
                    logging.getLogger('brm.ui').warning('Rejected stale UI event fn=%s trigger=%s',*key)
                from starlette.responses import JSONResponse
                return await JSONResponse({'detail':'工作台已更新，请刷新页面后继续。'},status_code=409)(scope,receive,send)
            replayed=False
            async def replay():
                nonlocal replayed
                if not replayed:
                    replayed=True
                    return {'type':'http.request','body':body,'more_body':False}
                return await receive()
            await self.app(scope,replay,send)
    return BrowserEventGuard
