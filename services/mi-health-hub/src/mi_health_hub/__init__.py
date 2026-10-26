"""mi-health-hub —— 小米手表健康数据自托管分析平台。

数据流：小米健康云 → cloud/ 适配层拉取 → SQLite 事实表 → analysis/ 指标计算 → FastAPI + Web 看板。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
