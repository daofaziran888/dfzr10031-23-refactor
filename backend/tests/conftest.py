"""测试夹具：每个用例都用干净的示例数据和取数模板。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import fetch_service as fetch_module  # noqa: E402
from app.services.fetch_service import FetchService  # noqa: E402
from app.store import store  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state():
    store.reset()
    fresh = FetchService()
    fetch_module.fetch_service._templates = fresh._templates
    fetch_module.fetch_service._template_meta = fresh._template_meta
    fetch_module.fetch_service._reports = fresh._reports
    fetch_module.fetch_service._next_report_id = fresh._next_report_id
    yield
    store.reset()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app)
