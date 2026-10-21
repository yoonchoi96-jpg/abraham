"""Optional local inference, serialized and admitted by the actual AMANDO policy.

No model downloads, global installs, remote providers, process kills or daemon
changes. Imports GearShift from an explicit inspected checkout, fails closed if
missing. Does not treat startup advice as permission to ignore live telemetry.
"""
import gc
import hashlib
import json
import os
import platform
import sys
import time
import urllib.request
from pathlib import Path


class Held(RuntimeError):
    def __init__(self, code, reasons=None):
        super().__init__(code)
        self.reasons = reasons or []


class GearShift:
    def __init__(self, amando_root):
        root = Path(amando_root).resolve()/'command_bridge'
        if not all((root/p).is_file() for p in ('telemetry.py','local_resources.py','g3_gearshift.py')):
            raise Held('GEARSHIFT_NOT_CONNECTED')
        sys.path.insert(0, str(root))
        import telemetry
        import local_resources
        self.telemetry, self.resources = telemetry, local_resources

    def check(self, budget, owned=None):
        snapshot = self.telemetry.collect()
        snapshot['resource_budget'] = budget
        decision = self.resources.admission(snapshot, running=owned is not None, owned_model=owned)
        if decision.get('allowed') is not True:
            raise Held('GEARSHIFT_HELD', decision.get('reasons',[]))
        return {k:decision.get(k) for k in ('allowed','warnings','used_bytes','system_ceiling_bytes')}


def ollama(path, data=None):
    req = urllib.request.Request('http://127.0.0.1:11434/api/'+path,
        data=None if data is None else json.dumps(data).encode(),
        headers={'Content-Type':'application/json'})
    return urllib.request.urlopen(req, timeout=120)


def budget_for(backend, model, context, gear):
    if backend == 'ollama':
        with ollama('tags') as r:
            tag = next((x for x in json.load(r)['models'] if x['name']==model),None)
        if tag is None:
            raise Held('OLLAMA_MODEL_NOT_INSTALLED')
        with ollama('show', {'model':model}) as r:
            show = json.load(r)
        return gear.resources.budget_from_metadata(tag,show,context)
    if backend != 'mlx' or platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise Held('MLX_REQUIRES_APPLE_SILICON')
    model_path = Path(model).resolve()
    if not model_path.is_dir() or not (model_path/'config.json').is_file():
        raise Held('MLX_LOCAL_WEIGHTS_MISSING')
    config = json.loads((model_path/'config.json').read_text())
    config = config.get('text_config', config)
    weights = sum(p.stat().st_size for p in model_path.glob('*.safetensors'))
    if not weights:
        raise Held('MLX_LOCAL_WEIGHTS_MISSING')
    try:
        layers = config['num_hidden_layers']
        heads = config['num_key_value_heads']
        dim = config.get('head_dim') or config['hidden_size']//config['num_attention_heads']
        if min(layers, heads, dim) <= 0 or context > config['max_position_embeddings']:
            raise ValueError()
        kv = layers*heads*dim*2*2*context
    except (KeyError,TypeError,ValueError):
        raise Held('MLX_MODEL_BUDGET_UNKNOWN') from None
    return {'model':str(model_path),'context_tokens':context,'weights_file_bytes':weights,
        'kv_estimate_bytes':kv,'runtime_allowance_bytes':1024**3,
        'required_increment_bytes':weights+kv+1024**3,'resident_increment_bytes':None}


def required_context(prompt, max_tokens, requested_context):
    """Bound KV reservation by real prompt bytes + response cap, not worst-case
    fixed context. UTF-8 bytes conservatively bound tokenizer tokens; leave
    128 tokens for possible chat template/system framing.
    """
    if not 512 <= requested_context <= 4096 or not 1 <= max_tokens <= 512:
        raise Held('BOUNDED_INFERENCE_REQUIRED')
    needed = len(prompt.encode('utf-8')) + max_tokens + 128
    selected = max(512, ((needed + 255) // 256) * 256)
    if selected > requested_context:
        raise Held('PROMPT_EXCEEDS_CONTEXT')
    return selected


def infer(backend, model, prompt, *, amando_root, work, context=4096, max_tokens=256):
    if backend not in {'ollama','mlx'} or not 512 <= context <= 4096 or not 1 <= max_tokens <= 512:
        raise Held('BOUNDED_INFERENCE_REQUIRED')
    allocated_context = required_context(prompt, max_tokens, context)
    import fcntl
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Use the same work directory for all local callers; never separate per model.
    with (work/'reader-inference.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Held('LOCAL_INFERENCE_BUSY') from None
        gear = GearShift(amando_root)
        budget = budget_for(backend,model,allocated_context,gear)
        before = gear.check(budget)
        started, first, last_check = time.monotonic(), None, time.monotonic()
        pieces, tokens = [], 0
        if backend == 'ollama':
            # Admission established no resident model; request lifetime ownership.
            with ollama('generate', {'model':model,'prompt':prompt,'stream':True,
                'keep_alive':0,'think':False,'options':{'temperature':0,'seed':1,'num_ctx':allocated_context,'num_predict':max_tokens}}) as r:
                final = {}
                for line in r:
                    part = json.loads(line)
                    if part.get('error'):
                        raise Held('OLLAMA_INFERENCE_FAILED')
                    text = part.get('response','')
                    if text and first is None:
                        first = time.monotonic()-started
                    pieces.append(text)
                    final = part
                    if time.monotonic()-last_check >= 1:
                        gear.check(budget, owned=model)
                        last_check = time.monotonic()
                tokens = final.get('eval_count',0)
        else:
            # Only existing local files are accepted; HF cannot fetch missing files.
            os.environ['HF_HUB_OFFLINE'] = '1'
            os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
            try:
                import mlx.core as mx
                from mlx_lm import load, stream_generate
                from mlx_lm.sample_utils import make_sampler
            except ImportError:
                raise Held('MLX_OPTIONAL_RUNTIME_MISSING') from None
            loaded = tokenizer = None
            try:
                mx.random.seed(1)
                loaded, tokenizer = load(str(Path(model).resolve()), tokenizer_config={'trust_remote_code':False})
                formatted = tokenizer.apply_chat_template([{'role':'user','content':prompt}], tokenize=False, add_generation_prompt=True, enable_thinking=False) if tokenizer.chat_template else prompt
                if len(tokenizer.encode(formatted))+max_tokens > allocated_context:
                    raise Held('PROMPT_EXCEEDS_CONTEXT')
                gear.check(budget)
                for part in stream_generate(loaded,tokenizer,prompt=formatted,max_tokens=max_tokens,sampler=make_sampler(temp=0)):
                    if first is None:
                        first = time.monotonic()-started
                    pieces.append(part.text)
                    tokens += 1
                    if time.monotonic()-last_check >= 1:
                        gear.check(budget)
                        last_check = time.monotonic()
            finally:
                del loaded, tokenizer
                gc.collect()
                mx.clear_cache()
        elapsed = time.monotonic()-started
        after = gear.check(budget, owned=model if backend=='ollama' else None)
        return {'output':''.join(pieces),'backend':backend,'model':model,
            'prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest(),
            'context':allocated_context,'requested_context':context,'max_tokens':max_tokens,'seed':1,'temperature':0,'thinking':False,
            'elapsed_seconds':elapsed,'first_token_seconds':first,'generated_tokens':tokens,
            'tokens_per_total_second':tokens/elapsed,'gearshift_before':before,'gearshift_after':after,
            'additional_api_cost':0}


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', choices=['ollama','mlx'], required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--amando-root', required=True)
    parser.add_argument('--work', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--prompt-file', required=True)
    args = parser.parse_args()
    try:
        result = infer(args.backend,args.model,Path(args.prompt_file).read_text(),amando_root=args.amando_root,work=args.work)
    except Held as exc:
        result = {'backend':args.backend,'status':'held','reason':str(exc),'additional_api_cost':0}
    except Exception:
        result = {'backend':args.backend,'status':'held','reason':'LOCAL_RUNTIME_FAILED','additional_api_cost':0}
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k not in {'output','model','gearshift_before','gearshift_after'}}))


if __name__ == '__main__':
    main()
