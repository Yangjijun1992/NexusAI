-- 002_task_limits.sql — 为 tasks 表增加任务级限制字段（S2-01 / S2-03）
-- 有限重试 / 最大执行时长 / 最大累计成本上限（reference §3、F6、§12 规则 4）。
-- SQLite 要求带 NOT NULL DEFAULT 的 ADD COLUMN 提供常量默认值；NULL 表示未设置上限。

ALTER TABLE tasks ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3;
ALTER TABLE tasks ADD COLUMN max_execution_seconds INTEGER NOT NULL DEFAULT 3600;
ALTER TABLE tasks ADD COLUMN max_cost REAL;
