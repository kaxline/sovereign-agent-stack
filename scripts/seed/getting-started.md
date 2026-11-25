# Getting started

Welcome to your local knowledge base (**${WORKSPACE}**).

Drop documents into this folder (PDF, DOCX, MD, TXT and more), then do one of:

1. Upload them in the LightRAG web UI at http://localhost:9621/webui/.
2. Run `make corpus-use SLUG=${WORKSPACE}`, then `make corpus-ingest SLUG=${WORKSPACE}`.
3. With the automation add-on on, run the **LightRAG Scan Inputs** n8n workflow.

After ingestion, explore the knowledge graph in the LightRAG UI or Neo4j Browser.
