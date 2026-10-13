-- 008_optimization.sql — 阶段4 优化（S4-03/S4-04）影子模式与预测审计表
-- 影子建议只记录「调度器将会如何选择」而不控制真实调度；预测运行历史用于审计。

-- 影子模式建议记录（advisory 只读，绝不改写 tasks / schedule_decisions）
CREATE TABLE IF NOT EXISTS shadow_recommendations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  recommendation_id TEXT NOT NULL UNIQUE,
  task_id TEXT,
  recommended_action TEXT NOT NULL,
  reason_codes TEXT,
  predicted_metric REAL,
  model TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shadow_recommendations_created_at ON shadow_recommendations(created_at);

-- 预测运行审计记录（可选，S4-01 的只读预测历史）
CREATE TABLE IF NOT EXISTS forecast_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL UNIQUE,
  model TEXT NOT NULL,
  metric TEXT NOT NULL,
  horizon_days INTEGER NOT NULL,
  train_start TEXT,
  train_end TEXT,
  prediction_count INTEGER NOT NULL,
  created_at TEXT NOT NULL
);
