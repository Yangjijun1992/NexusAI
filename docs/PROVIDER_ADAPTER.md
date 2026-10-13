# NexusAI Lite Provider Adapter 契约

> 对应 S1-03（统一 API Client + Provider Adapter）。所有模型请求统一经 `ApiClient`（§12 规则 1），业务模块不直接调用任何供应商 SDK。适配器封装供应商特有的请求格式、错误分类、`Retry-After`、Usage 与成本估算（§0.3 原则 8）。相关：[`ARCHITECTURE.md`](ARCHITECTURE.md)、[`records.py`](../src/nexusai_lite/records.py)。

## 抽象契约 `ProviderAdapter`（`providers/base.py`）

`ABC`，子类需实现（`name` 用于标识供应商）：

| 抽象方法 | 签名 | 职责 |
|---|---|---|
| `name` | `-> str` | 供应商标识名（如 `"deepseek"`） |
| `build_request` | `(request: ApiRequest) -> AdapterRequest` | 把统一 `ApiRequest` 转成 HTTP 请求载体（POST URL + JSON body）；**不放 API Key**（由 Client 统一加认证） |
| `classify_error` | `(*, status_code, response_body, exc) -> ErrorCategory` | 把状态码/响应体/异常映射为统一错误分类 |
| `extract_usage` | `(response_json) -> Usage | None` | 提取 Token 用量；缺失/非法返回 None |
| `extract_retry_after` | `(headers) -> float | None` | 从响应头提取 `Retry-After`（秒）；无效返回 None |
| `estimate_cost` | `(usage, model, pricing_version=None) -> CostEstimate` | 按价格表估算成本；无价格条目返回 `unknown` |
| `get_capabilities` | `(model) -> Mapping[str, Any]` | 返回模型能力（保守映射） |

- `AdapterRequest`：`{url, json_body}` 冻结 dataclass。
- `ApiRequest`（`records.py`）：`model` / `messages` / `stream` / `max_tokens` / `temperature` / `timeout_seconds` / `max_retries` / `task_id` / `idempotency_key` / `cost_limit` / `allow_retry_on_timeout` / `extra_body`。

## 错误分类 `ErrorCategory`（`records.py`）

适配器把供应商特有状态/异常映射到这些稳定分类，业务模块据此决定是否重试/告警：

`AUTHENTICATION_ERROR`、`PERMISSION_ERROR`、`RATE_LIMITED`、`PROVIDER_OVERLOADED`、`PROVIDER_SERVER_ERROR`、`NETWORK_ERROR`、`TIMEOUT_UNKNOWN`、`INVALID_REQUEST`、`BUDGET_EXCEEDED`、`CANCELLED`、`UNKNOWN_ERROR`。

- **可安全重试分类**（`RETRYABLE_CATEGORIES`）：`RATE_LIMITED`、`PROVIDER_OVERLOADED`、`PROVIDER_SERVER_ERROR`、`NETWORK_ERROR`。
- `TIMEOUT_UNKNOWN` 仅当显式 `allow_retry_on_timeout` 才重试（不确定是否已计费）。
- `UNKNOWN_ERROR` 保守处理，不无限重试。

## DeepSeek 实现（`providers/deepseek.py`）

- 请求：POST `{base_url}/chat/completions`；流式请求追加 `stream_options.include_usage`。
- 状态码 → 分类（`_classify_status`）：
  - `400`/`422` → `INVALID_REQUEST`；`401` → `AUTHENTICATION_ERROR`；`402` → `BUDGET_EXCEEDED`（余额不足）；`403` → `PERMISSION_ERROR`；`429` → `RATE_LIMITED`；`500`/`503`/`5xx` → `PROVIDER_SERVER_ERROR` / `PROVIDER_OVERLOADED`；其余 `4xx` → `INVALID_REQUEST`。
- 异常 → 分类：`ConnectTimeout/ConnectError` → `NETWORK_ERROR`（未计费）；`Read/Write/PoolTimeout` → `TIMEOUT_UNKNOWN`（可能已计费）；其余 `RequestError` → `NETWORK_ERROR`。
- Usage：`usage.prompt_tokens` / `completion_tokens` / `total_tokens`。
- `Retry-After`：秒数或 HTTP-date（RFC 7231）。
- 成本：`input/1e6*in_price + output/1e6*out_price`；价格表为空时返回 `unknown`（不臆造价格）。

## 统一 API Client（`api_client.py`）

- `ApiClient.chat(request)` / `chat_stream(request)`：构造请求、加 `Authorization: Bearer` 与 `X-Request-Id`、有限重试、错误分类、用量/成本、请求记录。
- **重试语义（尊重 `Retry-After`）**：对 `RATE_LIMITED` / `PROVIDER_OVERLOADED` 优先用 `Retry-After`；否则指数退避 `base*2^(attempt-1)`，上限 `backoff_max_seconds`，加抖动 `uniform(0, jitter_ratio*base)`。
- 重试受 `max_retries` / `allow_retry_on_timeout` / `cost_limit` 约束。
- 流式中断言弃/读失败时保守保留已捕获用量，记 `may_be_billed=True`。
- 请求记录经 `RequestLogger` 协议（`stats.DbRequestLogger`）落库，`request_id` 去重；错误只存脱敏消息与 `sha256:<前16>` 原始引用。

## 有限重试策略（`retry_policy.py`）

- `RetryPolicy.decide(...)` 判定顺序：不可重试分类（认证/权限/非法/预算/取消/未知，其中认证 `stop_and_alert=True`）→ 超时未允许 → 非可重试 → 次数上限 → 时间上限 → 成本上限 → 计算退避。
- 返回 `RetryDecision(should_retry, delay_seconds, reason, stop_and_alert)`。

## 新增一个 Provider（以假设的 `ExampleProvider` 为例）

1. 在 `providers/` 下新建 `example.py`，继承 `ProviderAdapter`，实现全部抽象方法（把供应商状态码映射到 `ErrorCategory`，实现 `extract_usage` / `extract_retry_after` / `estimate_cost`）。
2. 在 `providers/__init__.py` 导出 `ExampleAdapter`。
3. 通过 `ApiClient(adapter=ExampleAdapter(...), api_key=..., ...)` 使用；业务代码只依赖 `ProviderAdapter` 契约，不依赖具体供应商语义。
4. 补充价格表（`config.pricing.table`）以使 `estimate_cost` 返回可估算结果；否则默认返回 `unknown`。
5. 为适配器编写独立单元测试（§0.3 原则 8：可独立测试、独立升级）。

> 多 Provider 统一网关属于延后范围（§0.2「多 Provider 统一网关」延后）。当前 `ApiClient` 一次使用一个适配器；`records` 的 `provider` 字段与 `providers/__init__` 已为多适配器预留结构。见 [`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md)。
