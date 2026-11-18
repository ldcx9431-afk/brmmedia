import argparse
from pathlib import Path


def patch_workbench(source: str) -> str:
    """Remove LAN-only authentication controls and gate destructive asset actions."""
    def replace(old, new):
        nonlocal source
        if source.count(old) != 1:
            raise ValueError(f'Expected one workbench anchor: {old!r}')
        source = source.replace(old, new, 1)

    def conditional(first, end):
        nonlocal source
        start = source.index(first)
        stop = source.index(end, start)
        block = source[start:stop].rstrip('\n')
        indent = ' ' * (len(first) - len(first.lstrip()))
        guarded = '\n'.join('    ' + line if line.strip() else line for line in block.splitlines())
        source = source[:start] + indent + 'if not LAN_WORKBENCH_NO_LOGIN:\n' + guarded + '\n\n' + source[stop:]

    replace('GRADIO_HOST = ', 'LAN_WORKBENCH_NO_LOGIN = os.environ.get("BRM_LAN_WORKBENCH_NO_LOGIN") == "1"\nGRADIO_HOST = ')
    conditional('                    with gr.Column(scale=1, min_width=120):\n                        nav_keys_btn', '        # Settings must')
    conditional('        with gr.Column(elem_id="global-settings-trigger"):', '        # ---- 每个工作流')
    conditional('        nav_keys_btn.click(', '        qwen_submit_event' if '        qwen_submit_event' in source else '    return demo')
    if '                    switch_qwen.click(' in source:
        conditional('                    switch_qwen.click(', '                    refresh_runtime.click(')
        replace('interactive=initial_qwen["interactive"]', 'visible=not LAN_WORKBENCH_NO_LOGIN, interactive=initial_qwen["interactive"]')
        replace('interactive=initial_strata["interactive"]', 'visible=not LAN_WORKBENCH_NO_LOGIN, interactive=initial_strata["interactive"]')
    if '        gr.api(api_delete_asset, api_name="delete_asset")' in source:
        replace('def api_delete_asset(task_id: str, filename: str, confirmed: bool = False) -> dict:', 'def api_delete_asset(task_id: str, filename: str, confirmed: bool = False, request: gr.Request = None) -> dict:')
        replace('    if confirmed is not True:\n', '    if LAN_WORKBENCH_NO_LOGIN and request and request.headers.get("x-brm-workbench-edge") != "authenticated":\n        if request.headers.get("x-brm-workbench-edge") == "lan" or request.client is None or request.client.host != "127.0.0.1":\n            raise gr.Error("请通过已认证的接口删除素材。")\n    if confirmed is not True:\n')
        replace('        f\'<button type="button" class="brm-asset-delete"', '        + (\'\' if LAN_WORKBENCH_NO_LOGIN else f\'<button type="button" class="brm-asset-delete"')
        replace('彻底删除</button></article>\'\n', '彻底删除</button>\')\n        + \'</article>\'\n')
    compile(source, 'webui.py', 'exec')
    return source


def patch_nginx(source: str, lan_host: str, wsl_peer: str) -> str:
    """Open the LAN UI while retaining credentials for automation and admin routes."""
    anchor = '    location / {\n        proxy_pass http://127.0.0.1:9000;'
    if source.count(anchor) != 1:
        raise ValueError('Expected one workbench proxy')
    prefix = f'''# The Windows firewall limits the original client addresses before portproxy.
# WSL sees the Windows gateway, never the originating address.
map "$host:$remote_addr" $brm_workbench_realm {{
    default "BRMMedia testing";
    "{lan_host}:{wsl_peer}" off;
}}
map $brm_workbench_realm $brm_workbench_edge {{
    default authenticated;
    off lan;
}}
'''
    # Named Gradio automation routes continue to require Basic Auth. The
    # browser uses queue/join and run for interactive and unqueued UI events.
    protected = '''    location ~ ^/(?:gradio_api/(?:api|call|proxy=|vibe-|profiling)|monitoring(?:/|$)) {
        proxy_pass http://127.0.0.1:9000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-BRM-Workbench-Edge authenticated;
        proxy_read_timeout 3700s;
        proxy_buffering off;
        client_max_body_size 2G;
    }

'''
    return prefix + source.replace(anchor, protected + anchor.replace('        proxy_pass', '        auth_basic $brm_workbench_realm;\n        proxy_set_header X-BRM-Workbench-Edge $brm_workbench_edge;\n        proxy_buffering off;\n        proxy_pass'), 1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('webui', type=Path)
    parser.add_argument('nginx', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--lan-host', required=True)
    parser.add_argument('--wsl-peer', required=True)
    args = parser.parse_args()
    from ipaddress import IPv4Address
    webui = patch_workbench(args.webui.read_text(encoding='utf-8'))
    nginx = patch_nginx(args.nginx.read_text(encoding='utf-8'), str(IPv4Address(args.lan_host)), str(IPv4Address(args.wsl_peer)))
    args.output.mkdir(exist_ok=True)
    (args.output / 'webui.py').write_text(webui, encoding='utf-8')
    (args.output / 'brmmedia').write_text(nginx, encoding='utf-8')
