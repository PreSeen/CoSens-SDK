"""Install the local CoSens plugin/MCP with scoped tool policies.

Run with: uv run --with tomlkit python scripts/install-codex-local.py
No credentials are copied into Codex config; the stdio wrapper loads local.env.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import time

import tomlkit


def main():
    os.umask(0o077)
    root = Path(__file__).resolve().parents[1]
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    home.mkdir(parents=True, exist_ok=True)
    config_path = home / "config.toml"
    if config_path.exists():
        backup = root / "var" / f"codex-config.backup-{time.time_ns()}.toml"
        shutil.copy2(config_path, backup)
        backup.chmod(0o600)
    document = tomlkit.parse(config_path.read_text()) if config_path.exists() else tomlkit.document()
    servers = document.setdefault("mcp_servers", tomlkit.table())
    server = servers.setdefault("cosens_rsi", tomlkit.table())
    server["command"] = str(root / "scripts/cosens-rsi-mcp")
    server["cwd"] = str(root)
    server["enabled"] = True
    server["tool_timeout_sec"] = 45
    server["default_tools_approval_mode"] = "prompt"
    tools = server.setdefault("tools", tomlkit.table())
    # Standard task operations are authorized through the RSI Run protocol.
    # Learning requests retain their existing approval behavior.
    for name in ("cosens_prepare_task", "cosens_request_guidance", "cosens_checkpoint",
                 "cosens_next_step", "cosens_store_artifact", "cosens_submit_step",
                 "cosens_complete_run", "cosens_submit_result", "cosens_get_run",
                 "cosens_get_active_recipe", "cosens_record_feedback", "cosens_get_run_events",
                 "cosens_get_decision"):
        tools.setdefault(name, tomlkit.table())["approval_mode"] = "approve"
    plugins = document.setdefault("plugins", tomlkit.table())
    plugin = plugins.setdefault("cosens-rsi@cosens-local", tomlkit.table())
    plugin["enabled"] = True
    # Local use reuses the stdio connection above. Avoid a second HTTP server
    # registration and a separate API-key connection for the same local service.
    plugin.setdefault("mcp_servers", tomlkit.table()).setdefault("cosens-rsi", tomlkit.table())["enabled"] = False
    temp = config_path.with_name(f".config.toml.cosens-{os.getpid()}")
    temp.write_text(tomlkit.dumps(document))
    temp.chmod(0o600)
    temp.replace(config_path)
    subprocess.run(["codex", "plugin", "marketplace", "add", str(root)], check=True)
    subprocess.run(["codex", "plugin", "add", "cosens-rsi@cosens-local", "--json"], check=True)
    print("CoSens plugin enabled; local cosens_rsi stdio MCP configured with scoped task-tool approvals.")


if __name__ == "__main__":
    main()
