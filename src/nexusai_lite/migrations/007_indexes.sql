CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_task_runs_task_id ON task_runs(task_id);
CREATE INDEX IF NOT EXISTS idx_task_attempts_task_id ON task_attempts(task_id);
CREATE INDEX IF NOT EXISTS idx_cost_records_provider_model_status ON cost_records(provider, model, cost_status);
CREATE INDEX IF NOT EXISTS idx_config_versions_status ON config_versions(status);
