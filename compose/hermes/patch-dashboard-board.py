#!/usr/bin/env python3
"""Board links and embed mode for the dashboard's kanban page.

Compatibility overlay (not upstream), for Boundary change request 0007 R2/R3.

- ``/kanban?task=<id>[&board=<slug>]`` opens that task's drawer once the board
  has loaded and scrolls its card into view. ``board`` shows that board without
  changing the board the dashboard remembers. An unknown id opens the board as
  usual. Closing the drawer drops ``task`` from the URL
  (plugins/kanban/dashboard/dist/index.js).
- ``?embed=1`` on any dashboard URL hides the dashboard's sidebar, mobile
  header and page header for the rest of that tab's life (sessionStorage);
  ``?embed=0`` turns it off. It is set in the served index.html, before the SPA
  renders, so the shell never flashes (hermes_cli/web_dist/index.html).

See docs/extending.md ("Dashboard links") for the contract.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("HERMES_ROOT", "/opt/hermes"))
MARKER = "assistant-stack: board links"
EMBED_MARKER = "assistant-stack: board embed"
TAG = "[patch-dashboard-board]"


def _edit(path: Path, label: str, marker: str, edits: list[tuple[str, str, int]]) -> None:
    """Apply (old, new, expected_count) edits to *path* once; *marker* makes it idempotent."""
    text = path.read_text()
    if marker in text:
        print(f"{TAG} already applied ({label})")
        return
    for old, new, count in edits:
        found = text.count(old)
        if found != count:
            raise SystemExit(
                f"{TAG} expected {count} x {old.strip()[:60]!r} in {path}, found {found}. "
                "Hermes image may have changed — update this patch.")
        text = text.replace(old, new)
    path.write_text(text)
    print(f"{TAG} patched {label}")


HELPERS = f"""\
  // {MARKER}. /kanban?task=<id>[&board=<slug>] opens that task's drawer;
  // the link's board is shown without changing the stored board pin.
  function readLinkParam(name) {{
    try {{
      return (new URLSearchParams(window.location.search).get(name) || "").trim() || null;
    }} catch (_e) {{ return null; }}
  }}

  function dropLinkParam(name) {{
    try {{
      const url = new URL(window.location.href);
      if (!url.searchParams.has(name)) return;
      url.searchParams.delete(name);
      window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
    }} catch (_e) {{ /* ignore */ }}
  }}

  function scrollCardIntoView(taskId) {{
    window.requestAnimationFrame(function () {{
      const card = document.querySelector(`[data-task-id="${{CSS.escape(taskId)}}"]`);
      if (card && card.scrollIntoView) card.scrollIntoView({{ block: "center", inline: "center" }});
    }});
  }}

"""

# Runs once the first board payload arrives. A card on the board scrolls into
# view; a task the board leaves out (archived) still opens if the API knows it.
OPEN_LINKED = f"""\
    // {MARKER}: open the linked task once the board has loaded.
    useEffect(function () {{
      const linked = linkTaskRef.current;
      if (!linked || !boardData) return;
      linkTaskRef.current = null;
      const onBoard = boardData.columns.some(function (col) {{
        return col.tasks.some(function (task) {{ return task.id === linked; }});
      }});
      if (onBoard) {{
        setSelectedTaskId(linked);
        scrollCardIntoView(linked);
        return;
      }}
      SDK.fetchJSON(withBoard(`${{API}}/tasks/${{encodeURIComponent(linked)}}`, board))
        .then(function () {{ setSelectedTaskId(linked); }})
        .catch(function () {{ dropLinkParam("task"); }});
    }}, [boardData]);  // eslint-disable-line react-hooks/exhaustive-deps

"""


def _patch_kanban() -> None:
    ws = "    // --- WebSocket ---------------------------------------------------------\n"
    close = "          onClose: function () { setSelectedTaskId(null); },\n"
    switch = "      setBoard(nextSlug);\n"
    _edit(ROOT / "plugins" / "kanban" / "dashboard" / "dist" / "index.js", "kanban task links", MARKER, [
        ("  function withBoard(url, board) {\n", HELPERS + "  function withBoard(url, board) {\n", 1),
        ("const [board, setBoard] = useState(() => readSelectedBoard() || null);",
         'const [board, setBoard] = useState(() => readLinkParam("board") || readSelectedBoard() || null);', 1),
        ("const [selectedTaskId, setSelectedTaskId] = useState(null);\n",
         "const [selectedTaskId, setSelectedTaskId] = useState(null);\n"
         '    const linkTaskRef = useRef(readLinkParam("task"));\n', 1),
        (ws, OPEN_LINKED + ws, 1),
        (close, '          onClose: function () { setSelectedTaskId(null); dropLinkParam("task"); },\n', 1),
        # Picking another board in the switcher replaces the link's board.
        (switch, switch + '      dropLinkParam("board");\n', 1),
    ])


# The selectors are the shell's own landmarks in web/src/App.tsx and
# contexts/PageHeaderProvider.tsx: the sidebar, the mobile header and its
# spacer (direct children of the layout root), and the page header.
EMBED = f"""\
    <!-- {EMBED_MARKER}: ?embed=1 hides the dashboard shell in this tab. -->
    <script>(function(){{try{{var k="hermes.embed",v=new URLSearchParams(location.search).get("embed");if(v==="1")sessionStorage.setItem(k,"1");else if(v==="0")sessionStorage.removeItem(k);if(sessionStorage.getItem(k)==="1")document.documentElement.setAttribute("data-hermes-embed","");}}catch(e){{}}}})();</script>
    <style>html[data-hermes-embed] #app-sidebar,html[data-hermes-embed] [data-layout-variant]>header,html[data-hermes-embed] [data-layout-variant]>div[aria-hidden].h-14,html[data-hermes-embed] header[role="banner"]{{display:none!important}}</style>
"""


def _patch_index() -> None:
    _edit(ROOT / "hermes_cli" / "web_dist" / "index.html", "dashboard embed", EMBED_MARKER, [
        ("  </head>\n", EMBED + "  </head>\n", 1),
    ])


def main() -> None:
    _patch_kanban()
    _patch_index()


if __name__ == "__main__":
    main()
