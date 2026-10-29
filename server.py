from mcp.server.fastmcp import FastMCP

mcp = FastMCP("stackexchange")


@mcp.tool()
def ping() -> str:
    """Check that the server is running. Returns 'pong'."""
    return "yes!!!! the server is running"


if __name__ == "__main__":
    mcp.run()  # stdio transport