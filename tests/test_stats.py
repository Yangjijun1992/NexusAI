"""自身请求记录与统计测试（S1-04）。

使用临时数据库与 :class:`DbRequestLogger` 写入构造的请求记录，验证聚合指标、
延迟分位数（最近秩法）、样本不足质量标记、去重与成本/分组统计。无网络。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from nexusai_lite import db
from nexusai_lite.records import ErrorCategory, RequestRecord
from nexusai_lite.stats import DbRequestLogger, nearest_rank_percentile, summary

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

T0 = "2026-10-12T01:00:00+00:00"


def _db_file(tmp_path: Path) -> Path:
    path = tmp_path / "nexusai.db"
    db.init_db(path)
    return path


def _conn(tmp_path: Path) -> sqlite3.Connection:
    return db.connect(_db_file(tmp_path))


def _record(  # noqa: PLR0913
    request_id: str,
    *,
    model: str = "deepseek-chat",
    provider: str = "deepseek",
    started_at: str = T0,
    latency_ms: int = 100,
    status_code: int | None = 200,
    retry_count: int = 0,
    input_tokens: int = 1,
    output_tokens: int = 1,
    estimated_cost: float | None = None,
    cost_status: str = "unknown",
    error_category: ErrorCategory | None = None,
) -> RequestRecord:
    """构造一条请求记录（默认参数便于多数断言）。"""
    return RequestRecord(
        request_id=request_id,
        provider=provider,
        model=model,
        started_at=started_at,
        ended_at=started_at,
        latency_ms=latency_ms,
        status_code=status_code,
        retry_count=retry_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        estimated_cost=estimated_cost,
        cost_status=cost_status,
        error_category=error_category,
        safe_error_message=None,
        raw_error_reference=None,
        streaming=False,
        task_id="task-1",
        attempts=(),
    )


def _log_all(tmp_path: Path, records: list[RequestRecord]) -> None:
    logger = DbRequestLogger(_db_file(tmp_path))
    for record in records:
        logger.log_request(record)


# ------------------------------------------------------------------ 分位数
class TestPercentiles:
    def test_nearest_rank_percentile(self) -> None:
        """最近秩分位数：手算 5 样本 50/95/99 分位。"""
        values = [100, 200, 300, 400, 500]
        assert nearest_rank_percentile(values, 50) == 300
        assert nearest_rank_percentile(values, 95) == 500
        assert nearest_rank_percentile(values, 99) == 500

    def test_summary_percentiles_ok_with_enough_sample(self, tmp_path: Path) -> None:
        """样本充足（>=min_sample）：quality=ok 且分位数正确。"""
        latencies = [100, 200, 300, 400, 500]
        records = [
            _record(f"r{i}", latency_ms=lat, status_code=200) for i, lat in enumerate(latencies)
        ]
        _log_all(tmp_path, records)
        conn = _conn(tmp_path)
        try:
            report = summary(conn, min_sample=5)
        finally:
            conn.close()
        assert report.total == 5
        assert report.latency_quality == "ok"
        assert report.latency_p50 == 300
        assert report.latency_p95 == 500
        assert report.latency_p99 == 500
        assert report.success == 5
        assert report.failure == 0
        assert report.success_rate == 1.0
        json.loads(json.dumps(report.to_dict()))  # JSON 可序列化

    def test_insufficient_sample_percentiles_none(self, tmp_path: Path) -> None:
        """样本不足：quality=INSUFFICIENT_SAMPLE 且分位数为 None。"""
        latencies = [100, 200, 300, 400, 500]
        records = [
            _record(f"r{i}", latency_ms=lat) for i, lat in enumerate(latencies)
        ]
        _log_all(tmp_path, records)
        conn = _conn(tmp_path)
        try:
            report = summary(conn, min_sample=20)
        finally:
            conn.close()
        assert report.latency_quality == "INSUFFICIENT_SAMPLE"
        assert report.latency_p50 is None
        assert report.latency_p95 is None
        assert report.latency_p99 is None


# ------------------------------------------------------------------ 各比率
class TestRates:
    def test_mixed_status_rates_and_error_counts(self, tmp_path: Path) -> None:
        """混合状态：成功率/429/5xx/超时率/重试率与 error_counts。"""
        records = [
            _record("s1", status_code=200, retry_count=1),
            _record("s2", status_code=200),
            _record("s3", status_code=200),
            _record("r429", status_code=429, error_category=ErrorCategory.RATE_LIMITED),
            _record("r500", status_code=500, error_category=ErrorCategory.PROVIDER_SERVER_ERROR),
            _record(
                "rtime",
                status_code=None,
                error_category=ErrorCategory.TIMEOUT_UNKNOWN,
            ),
        ]
        _log_all(tmp_path, records)
        conn = _conn(tmp_path)
        try:
            report = summary(conn, min_sample=20)
        finally:
            conn.close()
        assert report.total == 6
        assert report.success == 3
        assert report.failure == 3
        assert report.success_rate == 0.5
        assert report.rate_429 == 1 / 6
        assert report.rate_5xx == 1 / 6
        assert report.timeout_rate == 1 / 6
        assert report.retry_rate == 1 / 6  # s1 有 retry_count=1
        assert report.error_counts == {
            "rate_limited": 1,
            "provider_server_error": 1,
            "timeout_unknown": 1,
        }


# ------------------------------------------------------------------ 成本
class TestCost:
    def test_cost_sum_and_unknown_count(self, tmp_path: Path) -> None:
        """成本求和（estimated）与 unknown 计数。"""
        records = [
            _record("c1", estimated_cost=0.001, cost_status="estimated"),
            _record("c2", estimated_cost=0.002, cost_status="estimated"),
            _record("c3", estimated_cost=None, cost_status="unknown"),
        ]
        _log_all(tmp_path, records)
        conn = _conn(tmp_path)
        try:
            report = summary(conn)
        finally:
            conn.close()
        assert report.estimated_cost == 0.003
        assert report.cost_unknown_count == 1


# ------------------------------------------------------------------ 去重
class TestDedup:
    def test_logging_same_record_twice_not_duplicated(self, tmp_path: Path) -> None:
        """同一 request_id 重复写入：api_request_logs 与 cost_records 各一行。"""
        db_file = _db_file(tmp_path)
        logger = DbRequestLogger(db_file)
        rec = _record("dup-1", estimated_cost=0.001, cost_status="estimated")
        logger.log_request(rec)
        logger.log_request(rec)  # 幂等

        conn = db.connect(db_file)
        try:
            logs = conn.execute(
                "SELECT COUNT(*) AS c FROM api_request_logs WHERE request_id='dup-1'"
            ).fetchone()
            assert logs is not None
            assert logs["c"] == 1
            costs = conn.execute(
                "SELECT COUNT(*) AS c FROM cost_records WHERE request_id='dup-1'"
            ).fetchone()
            assert costs is not None
            assert costs["c"] == 1
        finally:
            conn.close()

    def test_cost_record_written_once_via_guard(self, tmp_path: Path) -> None:
        """cost_records 一行写入一次（UNIQUE 无显式单请求键，靠 SELECT 守卫）。"""
        db_file = _db_file(tmp_path)
        logger = DbRequestLogger(db_file)
        rec = _record("cost-dup", estimated_cost=0.005, cost_status="estimated")
        logger.log_request(rec)
        logger.log_request(rec)
        conn = db.connect(db_file)
        try:
            row = conn.execute(
                "SELECT estimated_cost, cost_status, basis, source, effective_at "
                "FROM cost_records WHERE request_id='cost-dup'"
            ).fetchone()
            assert row is not None
            assert row["estimated_cost"] == 0.005
            assert row["cost_status"] == "estimated"
            assert row["basis"] == "api_response_usage"
            assert row["source"] == "platform_estimate"
            assert row["effective_at"] == T0
        finally:
            conn.close()


# ------------------------------------------------------------------ 分组
class TestGroupBy:
    def test_group_by_model(self, tmp_path: Path) -> None:
        """按 model 分组：count / success_rate / avg_latency_ms。"""
        records = [
            _record("m1", model="deepseek-chat", latency_ms=100, status_code=200),
            _record("m2", model="deepseek-chat", latency_ms=200, status_code=200),
            _record("r1", model="deepseek-reasoner", latency_ms=300, status_code=200),
            _record(
                "r2",
                model="deepseek-reasoner",
                latency_ms=500,
                status_code=500,
                error_category=ErrorCategory.PROVIDER_SERVER_ERROR,
            ),
        ]
        _log_all(tmp_path, records)
        conn = _conn(tmp_path)
        try:
            report = summary(conn, group_by="model")
        finally:
            conn.close()
        assert set(report.groups.keys()) == {"deepseek-chat", "deepseek-reasoner"}
        chat = report.groups["deepseek-chat"]
        assert chat["count"] == 2
        assert chat["success_rate"] == 1.0
        assert chat["avg_latency_ms"] == 150.0
        reasoner = report.groups["deepseek-reasoner"]
        assert reasoner["count"] == 2
        assert reasoner["success_rate"] == 0.5
        assert reasoner["avg_latency_ms"] == 400.0
        assert reasoner["error_counts"] == {"provider_server_error": 1}


# ------------------------------------------------------------------ 时间窗
class TestTimeWindow:
    def test_since_until_filter(self, tmp_path: Path) -> None:
        """since/until 过滤 started_at。"""
        records = [
            _record("early", started_at="2026-10-10T00:00:00+00:00"),
            _record("in1", started_at="2026-10-12T00:00:00+00:00"),
            _record("in2", started_at="2026-10-12T23:00:00+00:00"),
            _record("late", started_at="2026-10-14T00:00:00+00:00"),
        ]
        _log_all(tmp_path, records)
        conn = _conn(tmp_path)
        try:
            report = summary(
                conn,
                since="2026-10-12T00:00:00+00:00",
                until="2026-10-13T00:00:00+00:00",
            )
        finally:
            conn.close()
        # in1 (>= since) 命中，in2 (< until 且 >= since) 命中；early/late 排除。
        assert report.total == 2
        assert report.to_dict()["window"] == {
            "since": "2026-10-12T00:00:00+00:00",
            "until": "2026-10-13T00:00:00+00:00",
        }
