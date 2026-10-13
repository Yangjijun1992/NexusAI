"""Provider Adapter 抽象基类与请求载体（S1-03）。

Provider Adapter 把供应商特有的请求格式、错误分类、``Retry-After``、
Usage 与模型能力封装起来（reference §0.3 原则 8），使统一 API Client
与业务模块不依赖任何一家供应商的具体语义，可独立测试、独立升级。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

    from nexusai_lite.records import ApiRequest, CostEstimate, ErrorCategory, Usage


@dataclass(frozen=True)
class AdapterRequest:
    """适配器产出的 HTTP 请求载体：POST URL 与 JSON 请求体。

    认证由 API Client 统一添加，适配器不接触密钥。
    """

    url: str
    json_body: dict[str, Any]


class ProviderAdapter(ABC):
    """对某个供应商 API 行为的封装契约。

    子类需实现以下抽象方法；名称（:attr:`name`）用于标识供应商。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """供应商标识名（如 ``"deepseek"``）。"""

    @abstractmethod
    def build_request(self, request: ApiRequest) -> AdapterRequest:
        """把统一 :class:`ApiRequest` 转换成具体的 HTTP 请求载体。"""

    @abstractmethod
    def classify_error(
        self,
        *,
        status_code: int | None,
        response_body: str | None,
        exc: BaseException | None,
    ) -> ErrorCategory:
        """把状态码 / 响应体 / 异常映射为统一错误分类。"""

    @abstractmethod
    def extract_usage(self, response_json: Mapping[str, Any]) -> Usage | None:
        """从响应 JSON 中提取 Token 用量；缺失或非法则返回 None。"""

    @abstractmethod
    def extract_retry_after(self, headers: Mapping[str, str]) -> float | None:
        """从响应头提取 ``Retry-After``（秒数）；无效或缺失则返回 None。"""

    @abstractmethod
    def estimate_cost(
        self,
        usage: Usage,
        model: str,
        pricing_version: str | None = None,
    ) -> CostEstimate:
        """根据用量估算成本；无价格表时返回 unknown。"""

    @abstractmethod
    def get_capabilities(self, model: str) -> Mapping[str, Any]:
        """返回模型能力（保守映射，不臆造限制）。"""
