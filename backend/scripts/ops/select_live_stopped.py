import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import urllib.request


def main():
    output = Path('data/live_selection_20261115.json')
    if output.exists():
        raise FileExistsError(output)
    wrapper = Path('C:/Users/nadts/start_backend.ps1').read_text()
    if not re.search(r'\$env:BOT_AUTOSTART\s*=\s*"0"', wrapper):
        raise RuntimeError('Backend autostart must be disabled')
    with urllib.request.urlopen('http://127.0.0.1:8000/api/v1/bot/status', timeout=20) as response:
        status = json.load(response)
    if status.get('running') is not False or status.get('starting'):
        raise RuntimeError('Bot must be stopped')
    probe = json.loads(Path('data/live_connection_20261115.json').read_text())
    if not probe.get('configured_account_visible'):
        raise RuntimeError('Live account not verified')
    env_path = Path('.env')
    original = env_path.read_text(encoding='utf-8')
    state_path = Path('data/run_state.json')
    state = json.loads(state_path.read_text(encoding='utf-8'))
    receipt = {'as_of': datetime.now(timezone.utc).isoformat(), 'old_run_state': state,
               'old_env_mode': re.findall(r'^BOT_MODE=(.*)$', original, re.MULTILINE),
               'requested_mode': 'live', 'bot_autostart': False, 'stage': 'prepared'}
    output.write_text(json.dumps(receipt, indent=2) + '\n')
    updated, count = re.subn(r'^BOT_MODE=.*$', 'BOT_MODE=live', original, flags=re.MULTILINE)
    if count != 1:
        raise RuntimeError('Expected one mode setting')
    if env_path.read_text(encoding='utf-8') != original:
        raise RuntimeError('Concurrent environment change')
    temporary = env_path.with_name('.env.live-selection.tmp')
    with temporary.open('x', encoding='utf-8') as stream:
        stream.write(updated)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, env_path)
    state['mode'] = 'live'
    temporary = state_path.with_name('run_state.live-selection.tmp')
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(state, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, state_path)
    receipt['stage'] = 'selected_restart_pending'
    output.write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps({'mode': 'live', 'bot_autostart': False, 'restart_pending': True}))


if __name__ == '__main__':
    main()
