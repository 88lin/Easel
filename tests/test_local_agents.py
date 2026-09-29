"""本机 agent CLI 探测与接入。

场景：用户本机已经装着 Claude Code / Gemini CLI 并且登录过，这种情况下填 API Key
是多余的 —— OpenClaw 底座有 claude-cli / google-gemini-cli 后端，直接复用 CLI 的
登录态。这个模块负责「探测本机有什么」以及「如实说明哪些能免 key 用」。

关键约束：探测结果必须**诚实**。装了的 CLI 不等于能免 key 用（要有底座后端），
没装的不许报成已装。宁可显示「不支持」，也不能让用户以为配好了。

运行：pytest tests/test_local_agents.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "web"))
sys.path.insert(0, str(PROJECT_ROOT / "skills" / "shared" / "scripts"))

import app as web  # noqa: E402
from easel import local_agents as la  # noqa: E402


# ---- 探测层 ----


def test_detect_reports_installed_and_missing(monkeypatch):
    """PATH 上有 claude、没有 codex → 结果如实反映。"""
    monkeypatch.setattr(la.shutil, "which", lambda c: "/usr/bin/claude" if c == "claude" else None)
    by_id = {a["id"]: a for a in la.detect_local_agents()}
    assert by_id["claude-code"]["installed"] is True
    assert by_id["claude-code"]["command"] == "claude"
    assert by_id["codex"]["installed"] is False
    assert by_id["codex"]["path"] == ""


def test_only_clis_with_backend_are_usable_without_key(monkeypatch):
    """装了 Codex 也不能算「免 key 可用」——底座没有它的 provider。"""
    monkeypatch.setattr(la.shutil, "which", lambda c: f"/usr/bin/{c}")
    by_id = {a["id"]: a for a in la.detect_local_agents()}
    assert by_id["claude-code"]["usableWithoutKey"] is True
    assert by_id["gemini-cli"]["usableWithoutKey"] is True
    assert by_id["codex"]["installed"] is True
    assert by_id["codex"]["supported"] is False
    assert by_id["codex"]["usableWithoutKey"] is False, "没有底座后端就不算免 key 可用"
    assert by_id["opencode"]["usableWithoutKey"] is False


def test_missing_cli_is_never_usable(monkeypatch):
    """没装 CLI 时即便有底座后端，也不能报成可用。"""
    monkeypatch.setattr(la.shutil, "which", lambda c: None)
    for a in la.detect_local_agents():
        assert a["installed"] is False
        assert a["usableWithoutKey"] is False


def test_configured_reflects_openclaw_json(monkeypatch, tmp_path):
    """openclaw.json 里已有 anthropic provider → configured 为真（cli 已接入）。"""
    cfg = tmp_path / "openclaw.json"
    cfg.write_text(json.dumps({"models": {"providers": {"anthropic": {"baseUrl": "x"}}}}),
                   encoding="utf-8")
    monkeypatch.setenv("EASEL_OPENCLAW_CONFIG", str(cfg))
    monkeypatch.setattr(la.shutil, "which", lambda c: "/usr/bin/claude" if c == "claude" else None)
    by_id = {a["id"]: a for a in la.detect_local_agents()}
    assert by_id["claude-code"]["configured"] is True


def test_broken_config_does_not_raise(monkeypatch, tmp_path):
    """openclaw.json 损坏时探测要能降级，不能把界面打挂。"""
    cfg = tmp_path / "openclaw.json"
    cfg.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setenv("EASEL_OPENCLAW_CONFIG", str(cfg))
    monkeypatch.setattr(la.shutil, "which", lambda c: None)
    assert la.summarize_local_agents()["installedCount"] == 0


def test_summary_counts(monkeypatch):
    monkeypatch.setattr(la.shutil, "which", lambda c: "/usr/bin/claude" if c == "claude" else None)
    s = la.summarize_local_agents()
    assert s["installedCount"] == 1
    assert s["usableWithoutKeyCount"] == 1
    assert s["usableWithoutKey"] == ["claude-code"]


# ---- Web 接口 ----


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_BASE_URL=https://api.openai.com/v1\n", encoding="utf-8")
    monkeypatch.setattr(web, "ENV_FILE", env_file)

    oc_dir = tmp_path / ".openclaw-easel"
    oc_dir.mkdir()
    oc = oc_dir / "openclaw.json"
    oc.write_text(json.dumps({
        "models": {"providers": {"openai": {"baseUrl": "https://x/v1", "apiKey": "k",
                                            "models": [{"id": "m"}]}}},
        "agents": {"defaults": {"model": {"primary": "openai/m"}}},
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(web.Path, "home", staticmethod(lambda: tmp_path))

    local = "http://127.0.0.1:7860"
    with TestClient(web.app, base_url=local, client=("127.0.0.1", 51234),
                    headers={"Origin": local}) as c:
        c.oc_file = oc
        yield c


def test_api_local_agents_lists_all(sandbox, monkeypatch):
    monkeypatch.setattr(la.shutil, "which", lambda c: "/usr/bin/claude" if c == "claude" else None)
    resp = sandbox.get("/api/settings/local-agents")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["installedCount"] == 1
    ids = {a["id"] for a in body["agents"]}
    assert {"claude-code", "gemini-cli", "codex", "opencode"} <= ids


def test_enable_rejects_unknown_agent(sandbox):
    resp = sandbox.post("/api/settings/local-agents/enable", json={"id": "not-a-thing"})
    assert resp.status_code == 404


def test_enable_rejects_not_installed(sandbox, monkeypatch):
    """没装 Claude Code 却想接入 → 400，不能凭空造出 provider。"""
    monkeypatch.setattr(la.shutil, "which", lambda c: None)
    before = sandbox.oc_file.read_text(encoding="utf-8")
    resp = sandbox.post("/api/settings/local-agents/enable", json={"id": "claude-code"})
    assert resp.status_code == 400
    assert "没有找到" in resp.json()["detail"]
    assert sandbox.oc_file.read_text(encoding="utf-8") == before, "被拒绝的请求不该动配置"


def test_enable_rejects_cli_without_backend(sandbox, monkeypatch):
    """Codex 装了但底座不支持 → 明确拒绝，而不是写入一个用不了的 provider。"""
    monkeypatch.setattr(la.shutil, "which", lambda c: "/usr/bin/codex" if c == "codex" else None)
    resp = sandbox.post("/api/settings/local-agents/enable", json={"id": "codex"})
    assert resp.status_code == 400
    assert "暂无底座后端" in resp.json()["detail"]
    providers = json.loads(sandbox.oc_file.read_text(encoding="utf-8"))["models"]["providers"]
    assert "codex" not in providers and "anthropic" not in providers


def test_enable_claude_code_writes_provider_and_primary(sandbox, monkeypatch):
    """接入 Claude Code：声明 anthropic provider + 主模型切到 claude-cli。"""
    monkeypatch.setattr(la.shutil, "which", lambda c: "/usr/bin/claude" if c == "claude" else None)
    resp = sandbox.post("/api/settings/local-agents/enable", json={"id": "claude-code"})
    assert resp.status_code == 200, resp.text

    data = json.loads(sandbox.oc_file.read_text(encoding="utf-8"))
    assert data["models"]["providers"]["anthropic"]["baseUrl"] == "https://api.anthropic.com"
    # 不写 apiKey：登录态由 CLI 自己持有，写空 key 反而会覆盖掉已有配置
    assert "apiKey" not in data["models"]["providers"]["anthropic"]
    assert data["agents"]["defaults"]["model"]["primary"] == "claude-cli/claude-sonnet-4-6"
    # 原有 provider 与备份行为不变
    assert data["models"]["providers"]["openai"]["apiKey"] == "k"
    assert (sandbox.oc_file.parent / "openclaw.json.bak-web").is_file()


def test_enable_is_idempotent(sandbox, monkeypatch):
    """重复接入不该反复改写配置（第二次没有变化 → note 为空）。"""
    monkeypatch.setattr(la.shutil, "which", lambda c: "/usr/bin/claude" if c == "claude" else None)
    first = sandbox.post("/api/settings/local-agents/enable", json={"id": "claude-code"})
    assert first.status_code == 200
    snap = sandbox.oc_file.read_text(encoding="utf-8")
    second = sandbox.post("/api/settings/local-agents/enable", json={"id": "claude-code"})
    assert second.status_code == 200
    assert sandbox.oc_file.read_text(encoding="utf-8") == snap, "无变化时不该重写"
