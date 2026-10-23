import argparse

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="CoSens RSI API and MCP server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18104)
    args = parser.parse_args()
    uvicorn.run("cosens_rsi.api:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
