# Contracts

Each page here describes one category of the stack: the interface a replacement has to provide,
then the implementation the stack ships.

| Page | Category | Seam |
|---|---|---|
| [models](models.md) | Model gateway | HTTP, OpenAI-compatible |
| [tools](tools.md) | MCP add-ons, the add-on manifest and the instance store | MCP, files |
| [agent](agent.md) | Agent runtime requirements, and how Hermes meets them | add-on plans, chat API |
| [credentials](credentials.md) | Agent logins through the credential broker | HTTP |
| [secrets](secrets.md) | Service configuration secrets | files |
| [storage](storage.md) | The data root, shared databases and add-on stores | files, Postgres, Neo4j |
| [search](search.md) | Web search | HTTP, MCP |
| [upstreams](upstreams.md) | Every fork, vendored copy and overlay, with its pin and filing status | — |

Browser, messaging channels and the client API Boundary uses are still Hermes-specific; the
[agent](agent.md) page lists where.
