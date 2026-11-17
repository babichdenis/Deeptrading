from __future__ import annotations

import ast
import hashlib
import inspect
import json
from pathlib import Path
import textwrap

import pytest


def main():
    from app.bot import runtime as runtime_module

    root = Path(__file__).resolve().parents[2]
    folder = root / 'reports' / 'architect_20261116'
    manifest = json.loads((folder / 'step_exit_candidate_manifest.json').read_text(encoding='utf-8'))
    original = runtime_module.PaperBotRuntime._step_exit
    original_source = textwrap.dedent(inspect.getsource(original))
    if hashlib.sha256(original_source.encode()).hexdigest() != manifest['original_method_sha256']:
        raise RuntimeError('Runtime method changed; review candidate against current source first')
    candidate = (folder / 'step_exit_candidate.py').read_text(encoding='utf-8')
    if hashlib.sha256(candidate.encode()).hexdigest() != manifest['candidate_sha256']:
        raise RuntimeError('Candidate hash changed')
    parsed = ast.parse(candidate)
    if len(parsed.body) != 1 or not isinstance(parsed.body[0], ast.AsyncFunctionDef):
        raise RuntimeError('Candidate must contain exactly one async method')
    namespace = dict(vars(runtime_module))
    exec(compile(parsed, str(folder / 'step_exit_candidate.py'), 'exec'), namespace)
    runtime_module.PaperBotRuntime._step_exit = namespace['_step_exit']
    try:
        return pytest.main(['tests/test_runtime_protection_acceptance.py', '-q'])
    finally:
        runtime_module.PaperBotRuntime._step_exit = original


if __name__ == '__main__':
    raise SystemExit(main())
