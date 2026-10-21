"""Promptfoo local-only Python provider. Never use an external AI provider."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from reader_colors import _processing_gate
from scripts.reader_local import infer, Held


def call_api(prompt, options, context):
    backend = options.get('config',{}).get('backend')
    if backend == 'reader-tags':
        tags = context.get('vars',{}).get('tags',[])
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except ValueError:
                tags = [tags]
        color, _, _ = _processing_gate({'readwise_tags':tags})
        return {'output':json.dumps({'color':color or 'held'})}
    if backend not in {'ollama','mlx'}:
        return {'output':json.dumps({'color':'held','reason':'UNKNOWN_LOCAL_BACKEND'})}
    try:
        result = infer(backend,os.environ['READER_'+backend.upper()+'_MODEL'],prompt,
            amando_root=os.environ['READER_AMANDO_ROOT'],work=os.environ['READER_LOCAL_WORK'])
        # Malformed or unknown colors are held by the reporter.
        return {'output':result['output'], 'metadata':{k:v for k,v in result.items() if k not in {'output','model'}}}
    except Held as exc:
        return {'output':json.dumps({'color':'held','reason':str(exc)})}
    except Exception:
        return {'output':json.dumps({'color':'held','reason':'LOCAL_PROVIDER_UNAVAILABLE'})}
