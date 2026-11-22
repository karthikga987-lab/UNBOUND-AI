"""MCP server entry point for standalone stdio transport.

Run:
    python -m backend.app.mcp.run --storage-dir ./document_storage

Or with uv:
    uv run python -m backend.app.mcp.run
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="UNBOUND AI Document/Context MCP Server")
    parser.add_argument(
        "--storage-dir",
        default="./document_storage",
        help="Directory for document storage (default: ./document_storage)",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio"],
        default="stdio",
        help="MCP transport (currently only stdio is supported)",
    )
    args = parser.parse_args()

    storage_dir = Path(args.storage_dir).resolve()
    logger.info("Starting UNBOUND AI MCP server, storage: %s", storage_dir)

    try:
        from mcp.server.stdio import stdio_server
    except ImportError:
        logger.error(
            "MCP SDK not installed. Run: pip install mcp\n"
            "Or add 'mcp' to requirements.txt and reinstall."
        )
        sys.exit(1)

    from backend.app.documents.search import DocumentSearcher
    from backend.app.documents.store import DocumentStore
    from backend.app.intelligence.context import ContextIntelligence
    from backend.app.mcp.server import DocumentMCPServer

    store = DocumentStore(storage_dir)
    searcher = DocumentSearcher(store)
    intelligence = ContextIntelligence(store, searcher)
    mcp_server = DocumentMCPServer(store, searcher, intelligence)
    server = mcp_server.build_server()

    async def run():
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    asyncio.run(run())


if __name__ == "__main__":
    main()
