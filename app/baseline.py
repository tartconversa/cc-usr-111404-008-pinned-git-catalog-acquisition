"""当前单响应概览。"""
import json
def analyze(response):
    value=json.loads(response.body)
    return {'tree':value['sha'],'truncated':value['truncated'],
            'entries':[{k:r[k] for k in ('path','mode','type','sha')} for r in value['tree']]}
