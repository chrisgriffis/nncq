"""Paths and settings.

Two folders:
  ROOT   the nncq install: .venv, cache/models/hf (Nemotron weights), cache/tools (pip cache, parsers, torch).
  HOME   one nncq instance: profiles/ (+ profiles/archive), cache/db (indexes, state), cache/logs, .env.
         Default: ROOT. `nncq --home <dir> ...` picks another one; the bundled demo is ROOT/demo, a complete home
         of its own (its workspace, profile, settings and index all live in that folder).

Settings come from the process environment, then HOME/.env, then ROOT/.env (written by `nncq init`), then defaults.
Importing this module also points every model / parser cache into the install and switches the Hugging Face stack
offline, so nothing reaches the network after `nncq init` has fetched the models.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]   # repo root (package lives in src/nncq)
DEMO = ROOT / "demo"                         # the bundled demo home: the one place inside nncq a workspace may live
TOOLS_DIR = ROOT / "cache" / "tools"
MODELS_DIR = ROOT / "cache" / "models"
HF_HOME = MODELS_DIR / "hf"
TS_CACHE = TOOLS_DIR / "tree-sitter"

# per-home paths: set by use_home()
HOME = ROOT
ENV_FILE = PROFILES_DIR = CACHE = DATA_DIR = LOG_DIR = CONTROL_FILE = QUERY_BUSY_FILE = SERVER_FILE = REPOS_FILE = ROOT


def read_env_file(path: Path | None = None) -> dict[str, str]:
    """KEY=value lines of path (default: this home's .env)."""
    path = path or ENV_FILE
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if v[:1] not in "\"'" and " #" in v:
            v = v.split(" #", 1)[0].rstrip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


_from_files: set[str] = set()       # keys this module put into os.environ from .env files (the process env wins)


def use_home(home: Path) -> None:
    """Point every per-instance path at home and reload the .env layering (HOME/.env over ROOT/.env)."""
    global HOME, ENV_FILE, PROFILES_DIR, CACHE, DATA_DIR, LOG_DIR, CONTROL_FILE, QUERY_BUSY_FILE, SERVER_FILE, \
        REPOS_FILE
    HOME = Path(home).resolve()
    ENV_FILE = HOME / ".env"
    PROFILES_DIR = HOME / "profiles"
    CACHE = HOME / "cache"
    DATA_DIR = CACHE / "db"
    LOG_DIR = CACHE / "logs"
    CONTROL_FILE = DATA_DIR / "control.json"
    QUERY_BUSY_FILE = DATA_DIR / "query.busy"     # mtime = last GPU use by a search; the worker yields to it
    SERVER_FILE = DATA_DIR / "server.json"
    REPOS_FILE = DATA_DIR / "repos.json"
    for k in _from_files:
        os.environ.pop(k, None)
    _from_files.clear()
    for f in dict.fromkeys((ENV_FILE, ROOT / ".env")):
        for k, v in read_env_file(f).items():
            if v != "" and k not in os.environ:
                os.environ[k] = v
                _from_files.add(k)


def home_args() -> list[str]:
    """`--home <dir>` for child nncq processes (worker, server) when this one runs on a non-default home."""
    return [] if HOME == ROOT.resolve() else ["--home", str(HOME)]


use_home(ROOT)

os.environ.setdefault("HF_HOME", str(HF_HOME))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TORCH_HOME", str(TOOLS_DIR / "torch"))
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def _int(name: str, default: int) -> int:
    v = os.environ.get(name, "")
    return int(v) if v.strip() else default


def _float(name: str, default: float) -> float:
    v = os.environ.get(name, "")
    return float(v) if v.strip() else default


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name, "").strip().lower()
    return default if not v else v in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    token: str
    device: str
    embed_dim: int
    chunk_chars: int
    max_file_kb: int
    model_idle_seconds: int
    rerank_candidates: int
    watch: bool
    watch_debounce: float
    gpu_min_free_mb: int
    bg_when: str            # always | idle
    idle_minutes: float
    extra_extensions: str
    data: bool              # run the profile's data cross-reference rules ([[xref]] in profile.toml)
    ignore: str
    repos: str              # name=path;name2=path2 (what `nncq index` with no args indexes)
    index_procs: int        # CPU processes for chunking (T0)
    embed_tier: int         # 0 = no vectors, 1 = card vectors only, 2 = card then full-chunk vectors
    rerank: bool
    text_langs: str         # non-code formats to index besides code + profile DSLs
    max_response_kb: int    # tool answers above this are trimmed and paged with more(cursor); 0 = no limit
    cache_minutes: float    # identical tool calls within this window are answered from memory; 0 = off
    lite: bool              # NNCQ_MODE=lite: no torch, no models, no GPU (keyword + graph + data xref only)
    uml: bool               # build the UML/SysML model at index time (else on demand: nncq uml init / uml_build)
    public: bool            # NNCQ_PUBLIC: read-only public demo (no token, no write tools, no pause/resume)
    start: str              # NNCQ_START: dashboard hash a bare URL opens on (e.g. the hosted demo's tour start)


def mode() -> str:
    """full | lite. NNCQ_MODE; NNCQ_PROFILE=full|lite is the old name (profiles now mean codebase profiles)."""
    v = os.environ.get("NNCQ_MODE", "").strip().lower()
    if not v and os.environ.get("NNCQ_PROFILE", "").strip().lower() in ("full", "lite"):
        v = os.environ["NNCQ_PROFILE"].strip().lower()
    return "lite" if v == "lite" else "full"


def settings() -> Settings:
    e = os.environ.get
    lite = mode() == "lite"
    return Settings(
        host=e("NNCQ_HOST", "127.0.0.1"),
        port=_int("NNCQ_PORT", 8765),
        token=e("NNCQ_TOKEN", ""),
        device=e("NNCQ_DEVICE", "auto"),
        embed_dim=_int("NNCQ_EMBED_DIM", 1024),
        chunk_chars=_int("NNCQ_CHUNK_CHARS", 3000),
        max_file_kb=_int("NNCQ_MAX_FILE_KB", 1024),
        model_idle_seconds=_int("NNCQ_MODEL_IDLE_SECONDS", 600),
        rerank_candidates=_int("NNCQ_RERANK_CANDIDATES", 40),
        watch=_bool("NNCQ_WATCH", True),
        watch_debounce=_float("NNCQ_WATCH_DEBOUNCE", 5.0),
        gpu_min_free_mb=_int("NNCQ_GPU_MIN_FREE_MB", 0),
        bg_when=e("NNCQ_BG_WHEN", "always"),
        idle_minutes=_float("NNCQ_IDLE_MINUTES", 5.0),
        extra_extensions=e("NNCQ_EXTRA_EXTENSIONS", ""),
        data=_bool("NNCQ_DATA", True),
        ignore=e("NNCQ_IGNORE", ""),
        repos=e("NNCQ_REPOS", ""),
        index_procs=_int("NNCQ_INDEX_PROCS", max(1, min(8, (os.cpu_count() or 2) // 2))),
        embed_tier=0 if lite else _int("NNCQ_EMBED_TIER", 2),
        rerank=False if lite else _bool("NNCQ_RERANK", True),
        text_langs=e("NNCQ_TEXT_LANGS", "markdown,cmake,make,dockerfile,proto,batch"),
        max_response_kb=_int("NNCQ_MAX_RESPONSE_KB", 16),
        cache_minutes=_float("NNCQ_CACHE_MINUTES", 15.0),
        lite=lite,
        uml=_bool("NNCQ_UML", True),
        public=_bool("NNCQ_PUBLIC", False),
        start=e("NNCQ_START", ""),
    )


def is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")
