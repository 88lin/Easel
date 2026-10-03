import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VIDEO_SCRIPT = PROJECT_ROOT / "skills" / "shared" / "scripts" / "ai_video.py"


def test_ai_video_check_accepts_explicit_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / "video.env"
    env_file.write_text("DASHSCOPE_API_KEY=from-file\n", encoding="utf-8")

    env = os.environ.copy()
    for name in ("DASHSCOPE_API_KEY", "DASHSCOPE_KEY", "ALIYUN_API_KEY"):
        env.pop(name, None)
    env["PYTHONPATH"] = str(VIDEO_SCRIPT.parent)

    result = subprocess.run(
        [
            sys.executable,
            str(VIDEO_SCRIPT),
            "check",
            "--provider",
            "dashscope",
            "--env-file",
            str(env_file),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "[OK]   DASHSCOPE_API_KEY" in result.stdout
