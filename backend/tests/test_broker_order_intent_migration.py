import importlib.util
from io import StringIO
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.models.broker_order_intent import BrokerOrderFill, BrokerOrderIntent
from app.models.paper import PaperTrade


def migration_module():
    path = Path(__file__).resolve().parents[1] / 'alembic/versions/0006_broker_order_intents.py'
    spec = importlib.util.spec_from_file_location('broker_intent_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_matches_model_postgresql_schema(monkeypatch):
    module = migration_module()
    output = StringIO()
    context = MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': output})
    monkeypatch.setattr(module, 'op', Operations(context))
    module.upgrade()
    expected = str(CreateTable(BrokerOrderIntent.__table__).compile(dialect=postgresql.dialect())).strip()
    rendered = output.getvalue()
    assert ' '.join((expected + ';').split()) in ' '.join(rendered.split())
    fill_ddl = str(CreateTable(BrokerOrderFill.__table__).compile(dialect=postgresql.dialect())).strip()
    assert ' '.join((fill_ddl + ';').split()) in ' '.join(rendered.split())
    assert "SET LOCAL lock_timeout = '2s';" in rendered
    assert "SET LOCAL statement_timeout = '10s';" in rendered
    assert module.revision == '0006'
    assert module.down_revision == '0005'


def test_downgrade_only_removes_intent_table(monkeypatch):
    module = migration_module()
    output = StringIO()
    context = MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': output})
    monkeypatch.setattr(module, 'op', Operations(context))
    module.downgrade()
    assert output.getvalue().split() == 'DROP TABLE broker_order_fills; DROP TABLE broker_order_intents;'.split()
