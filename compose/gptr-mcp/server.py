"""
GPT Researcher MCP Server

This script implements an MCP server for GPT Researcher, allowing AI assistants
to conduct web research and generate reports via the MCP protocol.
"""

import os
import uuid
import logging
from typing import Dict, Any, Optional
from dotenv import load_dotenv
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from gpt_researcher import GPTResearcher

from utils import (
    research_store,
    create_success_response,
    handle_exception,
    get_researcher_by_id,
    format_sources_for_response,
    format_context_with_sources,
    store_research_results,
    create_research_prompt,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s] - %(message)s",
)

logger = logging.getLogger(__name__)

mcp = FastMCP(name="GPT Researcher")

if not hasattr(mcp, "researchers"):
    mcp.researchers = {}


@mcp.resource("research://{topic}")
async def research_resource(topic: str) -> str:
    """Provide research context for a given topic directly as a resource."""
    if topic in research_store:
        logger.info(f"Returning cached research for topic: {topic}")
        return research_store[topic]["context"]

    logger.info(f"Conducting new research for resource on topic: {topic}")
    researcher = GPTResearcher(topic)

    try:
        await researcher.conduct_research()
        context = researcher.get_research_context()
        sources = researcher.get_research_sources()
        source_urls = researcher.get_source_urls()
        formatted_context = format_context_with_sources(topic, context, sources)
        store_research_results(topic, context, sources, source_urls, formatted_context)
        return formatted_context
    except Exception as e:
        return f"Error conducting research on '{topic}': {str(e)}"


@mcp.tool()
async def deep_research(query: str) -> Dict[str, Any]:
    """Conduct web deep research on a given query using GPT Researcher."""
    logger.info(f"Conducting research on query: {query}...")
    research_id = str(uuid.uuid4())
    researcher = GPTResearcher(query)

    try:
        await researcher.conduct_research()
        mcp.researchers[research_id] = researcher
        logger.info(f"Research completed for ID: {research_id}")

        context = researcher.get_research_context()
        sources = researcher.get_research_sources()
        source_urls = researcher.get_source_urls()
        store_research_results(query, context, sources, source_urls)

        return create_success_response(
            {
                "research_id": research_id,
                "query": query,
                "source_count": len(sources),
                "context": context,
                "sources": format_sources_for_response(sources),
                "source_urls": source_urls,
            }
        )
    except Exception as e:
        return handle_exception(e, "Research")


@mcp.tool()
async def quick_search(query: str) -> Dict[str, Any]:
    """Perform a quick web search optimized for speed over quality."""
    logger.info(f"Performing quick search on query: {query}...")
    search_id = str(uuid.uuid4())
    researcher = GPTResearcher(query)

    try:
        search_results = await researcher.quick_search(query=query)
        mcp.researchers[search_id] = researcher
        logger.info(f"Quick search completed for ID: {search_id}")

        return create_success_response(
            {
                "search_id": search_id,
                "query": query,
                "result_count": len(search_results) if search_results else 0,
                "search_results": search_results,
            }
        )
    except Exception as e:
        return handle_exception(e, "Quick search")


@mcp.tool()
async def write_report(research_id: str, custom_prompt: Optional[str] = None) -> Dict[str, Any]:
    """Generate a report based on previously conducted research."""
    success, researcher, error = get_researcher_by_id(mcp.researchers, research_id)
    if not success:
        return error

    logger.info(f"Generating report for research ID: {research_id}")

    try:
        report = await researcher.write_report(custom_prompt=custom_prompt)
        sources = researcher.get_research_sources()
        costs = researcher.get_costs()

        return create_success_response(
            {
                "report": report,
                "source_count": len(sources),
                "costs": costs,
            }
        )
    except Exception as e:
        return handle_exception(e, "Report generation")


@mcp.tool()
async def get_research_sources(research_id: str) -> Dict[str, Any]:
    """Get the sources used in the research."""
    success, researcher, error = get_researcher_by_id(mcp.researchers, research_id)
    if not success:
        return error

    sources = researcher.get_research_sources()
    source_urls = researcher.get_source_urls()

    return create_success_response(
        {
            "sources": format_sources_for_response(sources),
            "source_urls": source_urls,
        }
    )


@mcp.tool()
async def get_research_context(research_id: str) -> Dict[str, Any]:
    """Get the full context of the research."""
    success, researcher, error = get_researcher_by_id(mcp.researchers, research_id)
    if not success:
        return error

    context = researcher.get_research_context()
    return create_success_response({"context": context})


@mcp.prompt()
def research_query(topic: str, goal: str, report_format: str = "research_report") -> str:
    """Create a research query prompt for GPT Researcher."""
    return create_research_prompt(topic, goal, report_format)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    return JSONResponse({"status": "healthy", "service": "mcp-server"})


def run_server():
    """Run the MCP server using FastMCP's built-in event loop handling."""
    if not os.getenv("OPENAI_API_KEY"):
        logger.error("OPENAI_API_KEY not found. Please set it in your .env file.")
        return

    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()

    if os.path.exists("/.dockerenv") or os.getenv("DOCKER_CONTAINER"):
        transport = "sse"
        logger.info("Docker environment detected, using SSE transport")

    logger.info(f"Starting GPT Researcher MCP Server with {transport} transport...")
    print(f"GPT Researcher MCP Server starting with {transport} transport...")

    try:
        if transport == "stdio":
            logger.info("Using STDIO transport (Claude Desktop compatible)")
            mcp.run(transport="stdio")
        elif transport == "sse":
            mcp.run(transport="sse", host="0.0.0.0", port=8000)
        elif transport == "streamable-http":
            mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
        else:
            raise ValueError(f"Unsupported transport: {transport}")

        logger.info("MCP Server is running...")
        while True:
            pass
    except Exception as e:
        logger.error(f"Error running MCP server: {str(e)}")
        print(f"MCP Server error: {str(e)}")
        return

    print("MCP Server stopped")


if __name__ == "__main__":
    run_server()
