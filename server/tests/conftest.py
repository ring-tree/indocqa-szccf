"""测试隔离：避免开发者本地 .env 的扩展模型配置影响用例断言。"""

import re

import pytest
from dotenv import dotenv_values

from app.core.config import SERVER_ROOT

_PROFILE_KEY = re.compile(r"^INDOCQA_LLM_PROFILE_\d+_(BASE_URL|API_KEY|MODEL|NAME)$")


@pytest.fixture(autouse=True)
def _isolate_model_profiles(monkeypatch):
    env_file = SERVER_ROOT / ".env"
    if not env_file.exists():
        return
    for key in dotenv_values(env_file):
        if _PROFILE_KEY.match(key):
            monkeypatch.setenv(key, "")
