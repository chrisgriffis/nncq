# nncq demo

A self-contained nncq home with a small, fictional, original codebase to try nncq on: `orbitkit`, a satellite
ground-station toolkit (four C++ libraries, a daemon, tests, CMake presets, an aarch64 toolchain, a Python tools
package, a three-level inheritance hierarchy, a call chain from `main` to a CRC check, a deliberate include cycle).

## Run it

| OS            | Run                       |
|---------------|---------------------------|
| Windows       | double-click `demo.cmd`   |
| Linux / macOS | `./demo.sh`               |

First run: installs nncq into `../.venv` in lite mode (no torch, no GPU, about 150 MB) and fetches the
init assets (tree-sitter parsers, the pinned Mermaid build the dashboard draws diagrams with). Every run: indexes
`orbitkit/` (a second or two, incremental after that), starts the server, seeds the model once, opens the dashboard
on http://127.0.0.1:8766/ (preset to the class view of `python/orbitkit_tools/frames.py`, `DecodeResult` in
`Decoder.h` and an "encoder" query; `START` in `run.py`). Close the window (or Ctrl+C) to stop it.

Options (passed through to `run.py`): `--fresh` (re-parse and re-seed), `--no-seed`, `--no-browser`.

## Layout

```
demo/
  demo.cmd, demo.sh        launchers: set up ../.venv if missing, then run.py
  run.py                   index -> serve -> seed (once) -> open the dashboard
  seed_model.py            generated diagrams + one agent-made view and composition, over MCP like an agent
  .env                     this home's settings: lite mode, port 8766 (layered over ../.env)
  profiles/orbitkit/       the demo's codebase profile (workspace root "orbitkit" = demo/orbitkit)
  orbitkit/                the example codebase
  docs/                    exports of the demo's diagrams quoted in the main README
  cache/                   this home's index, state and logs (git-ignored)
```

## How it stays self-contained

An nncq *home* is the folder holding `profiles/`, `cache/db`, `cache/logs` and `.env`. The default home is the nncq
folder; `nncq --home <dir>` picks another. `demo/` is one, so every command `run.py` issues is
`nncq --home demo ...`, and child processes (server, embedding worker) inherit the flag. Nothing here reads or
writes the main install's profiles or indexes; only `.venv` and the parser / model caches are shared.

Use any nncq command on it the same way, e.g. with the demo server running:

```
..\.venv\Scripts\nncq --home . graph trace Crc::check --from main
..\.venv\Scripts\nncq --home . search "where are frames with a bad CRC dropped"
..\.venv\Scripts\nncq --home . mcp-config        # point an agent at the demo (adds --home to the server args)
```
