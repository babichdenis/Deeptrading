
ALTER TABLE experiment_trades ADD COLUMN IF NOT EXISTS initial_stop NUMERIC(20,6);
ALTER TABLE experiment_trades ADD COLUMN IF NOT EXISTS take_profit NUMERIC(20,6);
