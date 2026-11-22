"""Paths and settings. Everything the tool writes lives under ROOT/cache (git-ignored):
    cache/models/hf   Nemotron weights + their remote code     cache/db     indexes and state
    cache/tools       pip cache, tree-sitter parsers, torch     cache/logs   server / worker logs

Settings come from the process environment, then ROOT/.env (written by `nncq init`), then defaults.
Importing this module also points every model / parser cache into the repo and switches the
Hugging Face stack offline, so nothing reaches the network after `nncq init` has fetched the models.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]   # repo root (package lives in src/nncq)
ENV_FILE = ROOT / ".env"
CACHE = ROOT / "cache"
DATA_DIR = CACHE / "db"
LOG_DIR = CACHE / "logs"
TOOLS_DIR = CACHE / "tools"
MODELS_DIR = CACHE / "models"
HF_HOME = MODELS_DIR / "hf"
TS_CACHE = TOOLS_DIR / "tree-sitter"
CONTROL_FILE = DATA_DIR / "control.json"
QUERY_BUSY_FILE = DATA_DIR / "query.busy"     # mtime = last GPU use by a search; the worker yields to it
SERVER_FILE = DATA_DIR / "server.json"
REPOS_FILE = DATA_DIR / "repos.json"


def read_env_file(path: Path = ENV_FILE) -> dict[str, str]:
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


for _k, _v in read_env_file().items():
    if _v != "":
        os.environ.setdefault(_k, _v)

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
    data: bool              # maintain the engine .xdata type/reference cross-reference
    ignore: str
    repos: str              # name=path;name2=path2 (what `nncq index` with no args indexes)
    index_procs: int        # CPU processes for chunking (T0)
    embed_tier: int         # 0 = no vectors, 1 = card vectors only, 2 = card then full-chunk vectors
    rerank: bool
    text_langs: str         # non-code formats to index besides code + engine DSLs
    max_response_kb: int    # tool answers above this are trimmed and paged with more(cursor); 0 = no limit
    cache_minutes: float    # identical tool calls within this window are answered from memory; 0 = off
    lite: bool              # NNCQ_PROFILE=lite: no torch, no models, no GPU (keyword + graph + data xref only)
    uml: bool               # build the UML/SysML model at index time (else on demand: nncq uml init / uml_build)


def settings() -> Settings:
    e = os.environ.get
    lite = e("NNCQ_PROFILE", "full").strip().lower() == "lite"
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
    )


def is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")
