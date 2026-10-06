"""保存自定义供应商时必须给模型条目写 name。

背景：面板保存走 _sync_openclaw_chat，原先只写 models[0].id。OpenClaw 的配置 schema 要求
每个模型条目有非空 name，缺了会让**网关下次启动直接拒绝加载配置**
（models.providers.<p>.models[0].name: Invalid input: expected string, received undefined），
也就是"面板显示保存成功、下次重启整个对话通道起不来"。
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "web"))

import pytest
from fastapi.testclient import TestClient

import web.app as web


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("EASEL_OPENCLAW_STATE_DIR", str(tmp_path))
    oc = tmp_path / "openclaw.json"
    oc.write_text(json.dumps({
        "models": {"providers": {"openai": {"api": "openai-completions"}}},
        "agents": {"defaults": {"model": {"primary": "openai/gpt-x"}}},
    }), encoding="utf-8")
    monkeypatch.setattr(web, "_read_env", lambda: {})
    local = "http://127.0.0.1:7860"
    c = TestClient(web.app, base_url=local, client=("127.0.0.1", 51234),
                   headers={"Origin": local})
    return c, oc


def _row(**kw):
    base = {"slot": "custom", "name": "newrelay", "model": "deepseek-v3",
            "baseUrl": "https://relay.example.com/v1", "key": "sk-test"}
    base.update(kw)
    return base


def _providers(oc):
    return json.loads(oc.read_text(encoding="utf-8"))["models"]["providers"]


def test_new_custom_provider_gets_model_name(client):
    """新增自定义供应商：模型条目要带上非空 name（缺了网关下次启动会拒绝加载）。"""
    c, oc = client
    r = c.post("/api/settings/models/save", json={"channel": "chat", "rows": [_row()]})
    assert r.status_code == 200, r.text
    entry = _providers(oc)["newrelay"]["models"][0]
    assert entry["id"] == "deepseek-v3"
    assert isinstance(entry.get("name"), str) and entry["name"].strip()


def test_existing_model_name_is_kept(client):
    """已经有 name 的（例如 OpenClaw 自己探测写下的）不能被覆盖。"""
    c, oc = client
    cfg = json.loads(oc.read_text(encoding="utf-8"))
    cfg["models"]["providers"]["newrelay"] = {
        "baseUrl": "https://relay.example.com/v1",
        "apiKey": "sk-old",
        "models": [{"id": "deepseek-v3", "name": "DeepSeek V3 (relay)"}],
    }
    oc.write_text(json.dumps(cfg), encoding="utf-8")
    r = c.post("/api/settings/models/save", json={"channel": "chat", "rows": [_row(key="")]})
    assert r.status_code == 200, r.text
    assert _providers(oc)["newrelay"]["models"][0]["name"] == "DeepSeek V3 (relay)"
