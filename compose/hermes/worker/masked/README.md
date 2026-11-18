This directory is mounted read-only over the stack checkout's `data/` folder
inside the agent's terminal container. That folder holds Hermes' own state:
session databases, profile `.env` files and each profile's `SOUL.md`. The agent
does not read or change it through a coding root. A profile's persona is edited
from the client, or on the host at `data/hermes/profiles/<profile>/SOUL.md`.
