"""Import an authorized local Codex --json log; no service credentials needed."""
import argparse
import json
from pathlib import Path

from cosens_rsi.codex_trace import import_trace
from cosens_rsi.service import RSIService

parser = argparse.ArgumentParser()
parser.add_argument("logs", nargs="+")
parser.add_argument("--db", default=str(Path(__file__).resolve().parents[1] / "var/cosens-rsi.sqlite3"))
args = parser.parse_args()
service = RSIService(db_path=args.db, memory_token="trace-import-unused")
for path in args.logs:
    print(json.dumps(import_trace(service, path), ensure_ascii=False))
