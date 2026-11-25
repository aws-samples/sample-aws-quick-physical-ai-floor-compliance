"""Entry point for Amazon Bedrock AgentCore Runtime (direct code deployment).

deploy.sh zips this file, agent/, perception/ and the pinned dependencies for
linux/arm64 into one archive; the runtime starts it with `python main.py`.
It serves MCP over streamable HTTP on port 8000 at /mcp, stateless. The bind address
comes from MCP_HOST: loopback by default, all interfaces when the CloudFormation
template sets it for the runtime.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.server import mcp  # noqa: E402

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
