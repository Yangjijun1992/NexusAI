"""阶段 1 集成测试（S1-04 / S1-05）。

(a) 端到端：``ApiClient`` + ``DeepSeekAdapter`` + ``DbRequestLogger`` 写入真实
    成功记录，``summary`` 与 ``period_stats`` 应反映该记录。
(b) CLI：``stats --json`` 与 ``analyze --json`` 通过 ``cli.main`` 分派并以 JSON
    输出可解析结果。使用 ``httpx.MockTransport``，无网络。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest

from nexusai_lite import db
from nexusai_lite.analysis import period_stats
from nexusai_lite.api_client import ApiClient
from nexusai_lite.cli import main as cli_main
from nexusai_lite.providers.deepseek import DeepSeekAdapter
from nexusai_lite.records import ApiRequest, RequestRecord
from nexusai_lite.stats import DbRequestLogger, summary

if TYPE_CHECKING:
    from pathlib import Path

_MODEL = "deepseek-chat"
_OK_JSON = json.dumps(
    {
        "choices": [{"message": {"role": "assistant", "content": "hello"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }
)


def _init_tmp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """指向并初始化一个临时数据库，同时覆盖环境变量路径解析。"""
    db_file = tmp_path / "nexusai.db"
    data_dir = tmp_path / "data"
    monkeypatch.setenv("NEXUSAI_DB_PATH", str(db_file))
    monkeypatch.setenv("NEXUSAI_DATA_DIR", str(data_dir))
    db.init_db(db_file)
    return db_file


def _seed_record(db_file: Path, *, started_at: str | None = None) -> None:
    """通过 DbRequestLogger 写入一条成功记录。"""
    record = RequestRecord(
        request_id="seed-integration",
        provider="deepseek",
        model=_MODEL,
        started_at=started_at or db.utcnow_iso(),
        ended_at=started_at or db.utcnow_iso(),
        latency_ms=120,
        status_code=200,
        retry_count=0,
        input_tokens=3,
        output_tokens=2,
        estimated_cost=0.0001,
        cost_status="estimated",
        error_category=None,
        safe_error_message=None,
        raw_error_reference=None,
        streaming=False,
        task_id="task-1",
        attempts=(),
    )
    DbRequestLogger(db_file).log_request(record)


# ------------------------------------------------------------------ (a) e2e
class TestEndToEnd:
    def test_api_client_writes_record_and_stats_reflect_it(
        self, tmp_path: Path
    ) -> None:
        """ApiClient（MockTransport）经 DbRequestLogger 落库，统计可查询。"""
        db_file = _init_tmp_db(tmp_path, pytest.MonkeyPatch())

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_OK_JSON)

        adapter = DeepSeekAdapter(
            price_table={
                _MODEL: {
                    "input_per_million": 1.0,
                    "output_per_million": 2.0,
                    "currency": "CNY",
                }
            }
        )
        http = httpx.Client(transport=httpx.MockTransport(handler), timeout=5.0)
        client = ApiClient(
            adapter,
            http_client=http,
            logger=DbRequestLogger(db_file),
            sleep_fn=lambda d: None,
        )
        resp = client.chat(ApiRequest(model=_MODEL, messages=[{"role": "user", "content": "hi"}]))

        assert resp.status_code == 200
        assert resp.record.cost_status == "estimated"

        conn = db.connect(db_file)
        try:
            report = summary(conn)
            assert report.total == 1
            assert report.success_rate == 1.0
            assert report.input_tokens == 3
            assert report.output_tokens == 2
            assert report.estimated_cost == pytest.approx(7e-06)

            period = period_stats(conn, days=30)
            # 记录应恰好落入一个桶，全桶样本数之和与总样本一致。
            assert period.sample_count == 1
            assert sum(b.sample_count for b in period.buckets) == 1
        finally:
            conn.close()


# ------------------------------------------------------------------ (b) CLI
class TestCli:
    def test_cli_stats_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``stats --json`` 经 main 返回 0 并输出可解析 JSON。"""
        db_file = _init_tmp_db(tmp_path, monkeypatch)
        _seed_record(db_file)

        ret = cli_main(["stats", "--json"])
        captured = capsys.readouterr()
        assert ret == 0
        payload = json.loads(captured.out)
        assert payload["total"] == 1
        assert payload["success_rate"] == 1.0
        assert payload["input_tokens"] == 3
        assert "quality" in payload
        assert payload["window"]["since"] is not None
        assert payload["window"]["until"] is None

    def test_cli_analyze_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``analyze --json`` 经 main 返回 0 并输出含固定桶的 JSON。"""
        db_file = _init_tmp_db(tmp_path, monkeypatch)
        _seed_record(db_file)

        ret = cli_main(["analyze", "--json"])
        captured = capsys.readouterr()
        assert ret == 0
        payload = json.loads(captured.out)
        names = [b["bucket"] for b in payload["buckets"]]
        assert names == ["workday_peak", "workday_offpeak", "workday_night", "weekend", "holiday"]
        assert payload["sample_count"] == 1
        assert sum(b["sample_count"] for b in payload["buckets"]) == 1
