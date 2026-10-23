from cosens_rsi.mcp_server import mcp


def run():
    mcp.run(transport="stdio")
