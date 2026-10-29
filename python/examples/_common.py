"""Shared configuration for examples only; the SDK does not load .env files."""
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class ConfigurationError(ValueError):
    """Safe local configuration guidance containing no supplied values."""


@dataclass(frozen=True)
class Settings:
    openai_key: str = field(repr=False)
    rsi_key: str = field(repr=False)
    model: str
    base_url: str | None
    mode: str
    prompt: str
    region: str


def settings():
    load_dotenv(ENV_FILE, override=False)
    region = os.getenv("COSENS_REGION", "cn").strip().lower()
    if region not in {"cn", "us"}:
        raise ConfigurationError("COSENS_REGION 仅支持 cn 或 us。")
    regional_key = os.getenv("RSI_API_KEY_" + region.upper(), "").strip()
    rsi_key = regional_key or os.getenv("RSI_API_KEY", "")
    required = ("OPENAI_API_KEY", "OPENAI_MODEL")
    missing = [name for name in required if not os.getenv(name, "").strip() or os.environ[name].startswith("your-")]
    if not rsi_key.strip() or rsi_key.startswith("your-"):
        missing.append("RSI_API_KEY_" + region.upper() + " 或 RSI_API_KEY")
    if missing:
        raise ConfigurationError(f"请在 {ENV_FILE} 配置 {', '.join(missing)}；可复制同目录的 .env.example。")
    mode = os.getenv("COSENS_MODE", "auto").strip()
    if mode not in {"auto", "disable"}:
        raise ConfigurationError("COSENS_MODE 仅支持 auto 或 disable。")
    print(f"CoSens region: {region}")
    return Settings(os.environ["OPENAI_API_KEY"], rsi_key,
                    os.environ["OPENAI_MODEL"], os.getenv("OPENAI_BASE_URL") or None,
                    mode, os.getenv("COSENS_INPUT") or "请用三点说明如何为 Python API 编写可重复运行的集成测试。", region)


def show_turn(client):
    turn_id = client.cosens.last_turn_id
    capture = getattr(client.cosens, "rsi_capture", None)
    if capture is not None:
        print(f"capture: {capture}")
    if turn_id:
        turn = client.cosens.get_turn(turn_id)
        print(f"turn_id: {turn_id}\nrun_id: {turn.get('run_id')}\nstate: {turn.get('state')}")
    print("若模型已返回而上报未完成，请按 README 恢复流程处理，勿直接重跑任务。")


def launch(main):
    """Keep remote error bodies (which may contain input) out of console logs."""
    try:
        main()
    except ConfigurationError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as error:
        print(f"运行失败：{type(error).__name__}；status={getattr(error, 'status_code', None)}；"
              f"request_id={getattr(error, 'request_id', None)}。请按 README 排查；模型已执行时不要直接重跑。", file=sys.stderr)
        raise SystemExit(1) from None
