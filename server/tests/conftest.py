"""测试隔离：避免开发者本地 .env 的模型配置影响用例断言。

pydantic-settings 对空字符串环境变量会回退到 .env，因此这里使用不可用的
占位 Key（长度不足 12 且非真实地址）来确保测试默认走离线降级路径。
"""

import os
import re

import pytest
from dotenv import dotenv_values

from app.core.config import SERVER_ROOT, get_settings

_PROFILE_KEY = re.compile(r"^INDOCQA_LLM_PROFILE_(\d+)_(BASE_URL|API_KEY|MODEL|NAME)$")
_DISABLED_LLM = {
    "INDOCQA_LLM_BASE_URL": "http://disabled.invalid/v1",
    "INDOCQA_LLM_API_KEY": "x",
    "INDOCQA_LLM_MODEL": "disabled",
    "INDOCQA_LLM_BACKUP_BASE_URL": "http://disabled.invalid/v1",
    "INDOCQA_LLM_BACKUP_API_KEY": "x",
    "INDOCQA_LLM_BACKUP_MODEL": "disabled",
}


@pytest.fixture(autouse=True)
def _isolate_model_profiles(monkeypatch):
    for key, value in _DISABLED_LLM.items():
        monkeypatch.setenv(key, value)

    env_file = SERVER_ROOT / ".env"
    keys = set(dotenv_values(env_file)) if env_file.exists() else set()
    keys |= {key for key in os.environ if _PROFILE_KEY.match(key)}
    for key in keys:
        if _PROFILE_KEY.match(key):
            monkeypatch.setenv(key, "x" if key.endswith("_API_KEY") else "disabled")

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
