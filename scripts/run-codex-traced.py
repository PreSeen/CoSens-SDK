"""Run an explicitly requested local Codex job and import its observable trace."""
import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from cosens_rsi.codex_trace import import_trace
from cosens_rsi.service import RSIService

parser = argparse.ArgumentParser()
parser.add_argument("--prompt-file", required=True)
parser.add_argument("--name", default="case")
parser.add_argument("codex_args", nargs=argparse.REMAINDER)
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
safe_name = "".join(c for c in args.name if c.isalnum() or c in "-_")[:60] or "case"
stamp = str(time.time_ns())
output = root / "var" / f"{safe_name}-{stamp}.jsonl"
error = output.with_suffix(".stderr.log")
os.umask(0o077)
service = RSIService(db_path=str(root / "var/cosens-rsi.sqlite3"), memory_token="trace-import-unused")
prompt_text = Path(args.prompt_file).read_text()
extra = args.codex_args[1:] if args.codex_args[:1] == ["--"] else args.codex_args
command = ["codex", "exec", "--skip-git-repo-check", "--ephemeral", "--sandbox", "workspace-write",
           "-C", str(root), "--json", *extra, "-"]
with open(args.prompt_file) as prompt, output.open("w") as stdout, error.open("w") as stderr:
    process = subprocess.Popen(command, cwd=root, stdin=prompt, stdout=stdout, stderr=stderr, start_new_session=True)
    try:
        previous_size = -1
        while process.poll() is None:
            size = output.stat().st_size
            if size != previous_size:
                import_trace(service, output, user_prompt=prompt_text)
                previous_size = size
            time.sleep(1)
    except KeyboardInterrupt:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    finally:
        result = import_trace(service, output, user_prompt=prompt_text)
print(json.dumps({"exit_code": process.returncode, "trace": str(output), **result}, ensure_ascii=False))
raise SystemExit(process.returncode)
