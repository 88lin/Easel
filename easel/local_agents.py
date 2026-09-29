"""探测本机已装的 AI agent CLI，并把「能直接用」的那些接进 Easel。

为什么需要：Easel 默认要求用户填一个 LLM API key。但本机常常已经装着 Claude Code、
Gemini CLI、Codex、OpenCode 这类 agent CLI，它们自己维护着登录态 —— 这种情况用户
完全不需要再配 key。

OpenClaw（Easel 的底座）对其中两类有原生后端，会把 CLI 的登录态直接当成模型来源：
  * ``claude-cli``  ← Claude Code（anthropic 插件）
  * ``google-gemini-cli`` ← Gemini CLI（google 插件）
其余 CLI（codex / opencode / qwen …）OpenClaw 目前没有内置 provider；探测出来是为了
在界面上如实展示「本机有什么、哪些能直接用」，而不是假装支持。

设计上刻意与 AionUi 的「先探测后配置」一致：先告诉用户现成有什么，再谈要不要填 key。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

# 已知的 agent CLI 及其在 OpenClaw 里的后端 id。
# openclaw_provider 为 None 表示：能探测到，但 OpenClaw 没有原生 provider（不可直接接入）。
KNOWN_AGENT_CLIS: tuple[dict[str, object], ...] = (
    {
        "id": "claude-code",
        "label": "Claude Code",
        "commands": ("claude",),
        "openclaw_provider": "claude-cli",
        # openclaw.json 里的 provider 键名与后端 id 不同名：CLI 后端 = claude-cli，
        # 配置块 = anthropic（models.providers.anthropic）。
        "config_provider": "anthropic",
        "login_hint": "在终端运行 claude 完成登录后，本机即无需再填 API Key。",
    },
    {
        "id": "gemini-cli",
        "label": "Gemini CLI",
        "commands": ("gemini",),
        "openclaw_provider": "google-gemini-cli",
        "config_provider": "google",
        "login_hint": "在终端运行 gemini 完成登录后，本机即无需再填 API Key。",
    },
    {
        "id": "codex",
        "label": "Codex CLI",
        "commands": ("codex",),
        "openclaw_provider": None,
        "config_provider": None,
        "login_hint": "Codex CLI 登录态暂未被底座识别为模型来源，可用其自身命令直接使用。",
    },
    {
        "id": "opencode",
        "label": "OpenCode",
        "commands": ("opencode",),
        "openclaw_provider": None,
        "config_provider": None,
        "login_hint": "OpenCode 登录态暂未被底座识别为模型来源，可用其自身命令直接使用。",
    },
    {
        "id": "qwen-code",
        "label": "Qwen Code",
        "commands": ("qwen",),
        "openclaw_provider": None,
        "config_provider": None,
        "login_hint": "Qwen Code 登录态暂未被底座识别为模型来源，可用其自身命令直接使用。",
    },
    {
        "id": "copilot-cli",
        "label": "GitHub Copilot CLI",
        "commands": ("copilot",),
        # 底座的 copilot-proxy 是连到 localhost:3000 的本地代理 provider（cliBackends
        # 为空），跟「本机装了 copilot CLI」没有因果关系 —— 装了 CLI 也不代表能免 key
        # 用，所以要另起代理服务。这里如实标为不支持，避免给出假的「一键接入」。
        "openclaw_provider": None,
        "config_provider": None,
        "login_hint": "Copilot 走底座的 copilot-proxy 需要另行运行本地代理（默认 localhost:3000），不能用本机 CLI 登录态免 key。",
    },
)


def _openclaw_config_path() -> Path:
    """OpenClaw 配置位置：Easel 用独立 profile（~/.openclaw-easel）。"""
    override = os.environ.get("EASEL_OPENCLAW_CONFIG", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".openclaw-easel" / "openclaw.json"


def _configured_providers() -> set[str]:
    """读 openclaw.json，返回已写入的 provider 名（用于判断本地 CLI 是否已接好）。"""
    cfg = _openclaw_config_path()
    if not cfg.is_file():
        return set()
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    providers = (data.get("models") or {}).get("providers") or {}
    return {k for k in providers if isinstance(k, str)}


def detect_local_agents() -> list[dict[str, object]]:
    """扫一遍 PATH，返回每个已知 agent CLI 的探测结果。

    字段：
      id / label            标识与展示名
      installed             是否在 PATH 上找到
      command / path        命中的可执行名与绝对路径
      openclaw_provider     底座的 provider id（None = 无原生后端）
      usable_without_key    True 表示「装了就能不填 API key 用」（需 provider 且已接入）
      configured            该 provider 是否已出现在 openclaw.json 里
      login_hint            给用户的一句话说明
    """
    configured = _configured_providers()
    out: list[dict[str, object]] = []
    for spec in KNOWN_AGENT_CLIS:
        found_cmd = ""
        found_path = ""
        for cmd in spec["commands"]:  # type: ignore[union-attr]
            p = shutil.which(cmd)  # type: ignore[arg-type]
            if p:
                found_cmd, found_path = cmd, p
                break
        provider = spec["openclaw_provider"]
        config_provider = spec.get("config_provider")
        installed = bool(found_path)
        is_configured = bool(config_provider) and config_provider in configured
        out.append({
            "id": spec["id"],
            "label": spec["label"],
            "installed": installed,
            "command": found_cmd,
            "path": found_path,
            # 只有「有原生后端」的 CLI 才可能免 key 使用；其余如实标注不支持。
            "openclawProvider": provider,
            "configProvider": config_provider,
            "supported": provider is not None,
            "configured": is_configured,
            "usableWithoutKey": bool(installed and provider is not None),
            "loginHint": spec["login_hint"],
        })
    return out


def summarize_local_agents() -> dict[str, object]:
    """给界面/doctor 用的摘要：本机装了哪些、其中哪些能免 key 用。"""
    agents = detect_local_agents()
    installed = [a for a in agents if a["installed"]]
    usable = [a for a in installed if a["usableWithoutKey"]]
    return {
        "agents": agents,
        "installedCount": len(installed),
        "usableWithoutKeyCount": len(usable),
        "usableWithoutKey": [a["id"] for a in usable],
    }
