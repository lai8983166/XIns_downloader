"""配置项与环境变量（design.md D12 配置表）。

所有取值均可经环境变量覆盖，默认值取保守档。`settings` 为全局单例，
直接 `from collector.config import settings` 使用。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

# 项目根目录（collector/ 的父目录），保证路径与启动 cwd 无关
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_str(key: str, default: str) -> str:
    v = os.getenv(key)
    return v if v is not None else default


def _env_int(key: str, default: int) -> int:
    v = os.getenv(key)
    return int(v) if v and v.strip() else default


def _env_float(key: str, default: float) -> float:
    v = os.getenv(key)
    return float(v) if v and v.strip() else default


def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_path(key: str, default: Path) -> Path:
    v = os.getenv(key)
    return Path(v) if v and v.strip() else default


def _env_float_pair(min_key: str, max_key: str, default: Tuple[float, float]) -> Tuple[float, float]:
    return (_env_float(min_key, default[0]), _env_float(max_key, default[1]))


def _env_int_list(key: str, default: List[int]) -> List[int]:
    v = os.getenv(key)
    if not v or not v.strip():
        return list(default)
    # 兼容 "15,30,60" 与 "15;30;60"
    return [int(x) for x in v.replace(";", ",").split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    # 浏览器持久化
    browser_user_data_dir: Path = field(
        default_factory=lambda: _env_path("XINS_BROWSER_USER_DATA_DIR", PROJECT_ROOT / ".browser" / "profile")
    )
    browser_storage_state: Path = field(
        default_factory=lambda: _env_path("XINS_BROWSER_STORAGE_STATE", PROJECT_ROOT / ".browser" / "state.json")
    )

    # 人类化节奏（随机抖动区间，单位：秒）
    nav_stabilize: Tuple[float, float] = field(
        default_factory=lambda: _env_float_pair("XINS_NAV_STABILIZE_MIN_SEC", "XINS_NAV_STABILIZE_MAX_SEC", (2.0, 4.0))
    )
    action_delay: Tuple[float, float] = field(
        default_factory=lambda: _env_float_pair("XINS_ACTION_DELAY_MIN_SEC", "XINS_ACTION_DELAY_MAX_SEC", (3.0, 7.0))
    )
    scroll_delay: Tuple[float, float] = field(
        default_factory=lambda: _env_float_pair("XINS_SCROLL_DELAY_MIN_SEC", "XINS_SCROLL_DELAY_MAX_SEC", (1.5, 3.5))
    )

    # 配额上限
    session_window_minutes: int = field(default_factory=lambda: _env_int("XINS_SESSION_WINDOW_MINUTES", 60))
    session_action_limit: int = field(default_factory=lambda: _env_int("XINS_SESSION_ACTION_LIMIT", 60))
    daily_action_limit: int = field(default_factory=lambda: _env_int("XINS_DAILY_ACTION_LIMIT", 120))

    # 冷却退避（分钟）
    cooldown_steps_min: List[int] = field(
        default_factory=lambda: _env_int_list("XINS_COOLDOWN_STEPS_MIN", [15, 30, 60, 120])
    )
    cooldown_max_minutes: int = field(default_factory=lambda: _env_int("XINS_COOLDOWN_MAX_MINUTES", 360))

    # Profile 分页
    profile_page_size: int = field(default_factory=lambda: _env_int("PROFILE_PAGE_SIZE", 6))

    # Profile 持久 page 池容量（LRU，跨请求复用同一 tab 续传滚动）
    profile_page_pool_max: int = field(default_factory=lambda: _env_int("XINS_PROFILE_PAGE_POOL_MAX", 4))

    # instaloader 应急兜底开关（默认关闭）
    enable_instaloader_fallback: bool = field(default_factory=lambda: _env_bool("ENABLE_INSTALOADER_FALLBACK", False))

    # 采集主路径开关：True=Playwright collector（默认）；False=回退 instaloader（仅单帖，且需 enable_instaloader_fallback）
    use_collector: bool = field(default_factory=lambda: _env_bool("USE_COLLECTOR", True))


settings = Settings()
