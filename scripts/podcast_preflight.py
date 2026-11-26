"""Check local dialogue dependencies before paying for a new writer response."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def check_runtime(config: dict, repo: Path) -> dict:
    runtime = Path(os.environ.get('NEWSRADAR_SOULX_ROOT',
        str((config.get('soulx') or {}).get('runtime_root', '')))).expanduser().resolve()
    python = runtime / '.venv/bin/python'
    for path in (python, runtime / 'source', runtime / 'models/SoulX-Podcast-1.7B-dialect',
                 runtime / 'cache'):
        if not path.exists():
            raise RuntimeError('preflight missing local dependency: ' + str(path))
    model = runtime / 'models/SoulX-Podcast-1.7B-dialect'
    weights = json.loads((model / 'model.safetensors.index.json').read_text())['weight_map']
    required = set(weights.values()) | {'config.json', 'tokenizer.json', 'flow.pt', 'hift.pt', 'campplus.onnx'}
    if not weights or any(not (model / name).is_file() or not (model / name).stat().st_size
                          for name in required):
        raise RuntimeError('preflight model artifacts incomplete')
    prompt = repo / 'TrendRadar/config' / config.get('dialogue_prompt_file', 'podcast_dialogue_prompt.txt')
    if not prompt.is_file() or not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise RuntimeError('preflight missing writer prompt or audio tools')
    for voice in ('male_1', 'female_1'):
        for suffix in ('.wav', '.txt'):
            if not (runtime / 'source/prompt_audios' / (voice + suffix)).is_file():
                raise RuntimeError('preflight missing voice reference')
    # Run the actual speech interpreter under the caller's launchd environment.
    # No model weights or provider credentials are loaded by this probe.
    probe = subprocess.run([str(python), '-c',
        'import json,torch,torchaudio; '
        'print(json.dumps({"torch":torch.__version__,"mps":torch.backends.mps.is_available()}))'],
        cwd=repo / 'TrendRadar', stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=60, check=True)
    result = json.loads(probe.stdout.strip().splitlines()[-1])
    if not result.get('mps'):
        raise RuntimeError('preflight MPS unavailable')
    with tempfile.TemporaryFile(dir=runtime / 'cache') as handle:
        handle.write(b'preflight')
    return {'status': 'passed', 'speech_python_started': True,
            'mps': True, 'torch': result['torch'], 'model_calls': 0}
