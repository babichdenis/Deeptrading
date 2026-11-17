import importlib.util
from io import StringIO
from pathlib import Path
import warnings

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory


ROOT = Path(__file__).resolve().parents[1] / 'alembic'


def test_revision_chain_has_one_unambiguous_head():
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        scripts = ScriptDirectory(str(ROOT))
        assert scripts.get_heads() == ['0006']
        assert [r.revision for r in scripts.walk_revisions()] == ['0006', '0005', '0004', '0003', '0002', '0001']
        assert [r.revision for r in scripts.iterate_revisions('0006', '0005')] == ['0006']


def test_consolidated_0002_preserves_both_schema_changes(monkeypatch):
    spec = importlib.util.spec_from_file_location('merged_0002', ROOT / 'versions/0002_broker_account_id.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = StringIO()
    context = MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': output})
    monkeypatch.setattr(module, 'op', Operations(context))
    module.upgrade()
    sql = output.getvalue()
    for statement in (
        'ALTER TABLE bot_logs ALTER COLUMN source TYPE VARCHAR(64)',
        'ALTER TABLE bot_logs ADD COLUMN IF NOT EXISTS request_id VARCHAR(32)',
        'CREATE INDEX IF NOT EXISTS ix_bot_logs_request_id ON bot_logs (request_id)',
        'ALTER TABLE sandbox_trades ADD COLUMN IF NOT EXISTS broker_account_id VARCHAR(40)',
        'CREATE INDEX IF NOT EXISTS ix_sandbox_trades_broker_account_id ON sandbox_trades (broker_account_id)',
    ):
        assert statement + ';' in sql
