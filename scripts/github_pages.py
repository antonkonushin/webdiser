"""Configure or inspect GitHub Pages using the existing Git credential helper.

No credentials are stored, printed or passed in command arguments.
Usage: python scripts/github_pages.py --enable   (main, /docs)
       python scripts/github_pages.py            (status only)
"""
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
REPO='antonkonushin/webdiser'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--enable',action='store_true')
    args=parser.parse_args()
    token=os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if token:
        authorization='Bearer '+token
    else:
        env=dict(os.environ,GIT_TERMINAL_PROMPT='0',GCM_INTERACTIVE='never')
        found=subprocess.run(['git','credential','fill'],input='protocol=https\nhost=github.com\npath='+REPO+'.git\n\n',
                             text=True,encoding='utf-8',capture_output=True,cwd=ROOT,env=env,timeout=30)
        credential=dict(line.split('=',1) for line in found.stdout.splitlines() if '=' in line)
        if found.returncode or not credential.get('password'):
            raise SystemExit('No existing GitHub credential is available for the Pages API.')
        pair=credential.get('username','x-access-token')+':'+credential['password']
        authorization='Basic '+base64.b64encode(pair.encode()).decode()
    def api(path,method='GET',payload=None):
        request=urllib.request.Request('https://api.github.com/repos/'+REPO+path,
             data=json.dumps(payload).encode() if payload is not None else None,method=method,
             headers={'Authorization':authorization,'Accept':'application/vnd.github+json',
                      'X-GitHub-Api-Version':'2022-11-28','User-Agent':'webdiser-build','Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request,timeout=30) as response:
                return response.status,json.load(response)
        except urllib.error.HTTPError as error:
            return error.code,json.loads(error.read().decode())
    status,data=api('/pages')
    if args.enable:
        if status==404:
            status,data=api('/pages','POST',{'build_type':'legacy','source':{'branch':'main','path':'/docs'}})
        elif status==200 and (data.get('source')!={'branch':'main','path':'/docs'} or data.get('build_type')!='legacy'):
            status,data=api('/pages','PUT',{'build_type':'legacy','source':{'branch':'main','path':'/docs'}})
    result={'http_status':status,'url':data.get('html_url'),'status':data.get('status'),
            'source':data.get('source'),'build_type':data.get('build_type')}
    if status not in [200,201,204]:
        result['message']=data.get('message')
        print(json.dumps(result,ensure_ascii=False))
        raise SystemExit(1)
    if status in [200,201]:
        build_status,build=api('/pages/builds/latest')
        result['latest_build']={'http_status':build_status,'status':build.get('status'),
                                'commit':build.get('commit'),'error':build.get('error')}
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
