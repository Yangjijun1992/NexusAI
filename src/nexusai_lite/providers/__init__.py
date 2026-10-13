"""Provider Adapter 包（S1-03）。

提供统一 API Client 可插拔的供应商适配器。首期仅 DeepSeek。
"""

from __future__ import annotations

from nexusai_lite.providers.base import AdapterRequest, ProviderAdapter
from nexusai_lite.providers.deepseek import DeepSeekAdapter

__all__ = ["AdapterRequest", "DeepSeekAdapter", "ProviderAdapter"]
