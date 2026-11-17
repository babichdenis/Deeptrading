from sqlalchemy import create_mock_engine
from sqlalchemy.schema import CreateTable

from app.database import Base, create_bootstrap_schema
from app.models.broker_order_intent import BrokerOrderFill, BrokerOrderIntent
from app.lab.persist import ensure_schema


def created_tables(create):
    names = []
    def execute(statement, *args, **kwargs):
        if isinstance(statement, CreateTable):
            names.append(statement.element.name)
    create(create_mock_engine('postgresql://', execute))
    return set(names)


def test_bootstrap_excludes_imported_execution_ledgers_but_keeps_existing_tables():
    assert BrokerOrderIntent.__table__ in Base.metadata.tables.values()
    assert BrokerOrderFill.__table__ in Base.metadata.tables.values()
    names = created_tables(create_bootstrap_schema)
    assert 'broker_order_intents' not in names
    assert 'broker_order_fills' not in names
    assert {'paper_trades', 'paper_accounts', 'bot_logs', 'sandbox_trades'} <= names
    assert names == {table.name for table in Base.metadata.tables.values() if not table.info.get('migration_managed')}


def test_lab_bootstrap_also_cannot_create_execution_ledgers():
    names = created_tables(ensure_schema)
    assert 'broker_order_intents' not in names
    assert 'broker_order_fills' not in names
    assert {'lab_experiment_runs', 'lab_signal_events'} <= names
