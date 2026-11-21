"""NVIDIA Nemotron embedder + cross-encoder reranker, run in-process (no server, no network).

Weights live in ROOT/models/hf (pinned revisions, fetched once by `nncq init`); every load is offline.
Both models are ~1B params; in bf16 on a GPU each takes ~2.5 GB of VRAM.

The embedder is asymmetric: documents and queries get different internal prompts
(encode_document / encode_query). It is Matryoshka-trained (native dim 2048), so vectors are
truncated to NNCQ_EMBED_DIM and renormalized.
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
import time

import numpy as np

from nncq import config

EMBED_MODEL = "nvidia/llama-nemotron-embed-1b-v2"
EMBED_REV = "b4caa8456edd360b3b4e938d94ed4398dd437fad"
RERANK_MODEL = "nvidia/llama-nemotron-rerank-1b-v2"
RERANK_REV = "8fd3e5d962d44cfe65d4ba0784eebed44cf136b0"
PINNED = [(EMBED_MODEL, EMBED_REV), (RERANK_MODEL, RERANK_REV)]

# One GPU pass is bounded by tokens, not text count: memory ~ count x padded length.
BATCH_TOKENS = int(os.environ.get("NNCQ_GPU_BATCH_TOKENS", "32768"))
# Background embedding pauses the GPU this long every BREAK_EVERY seconds, so the desktop, screen capture
# and other CUDA processes can get in (a saturated GPU starves them).
BREAK_EVERY_S = float(os.environ.get("NNCQ_GPU_BREAK_EVERY_S", "5"))
BREAK_MS = int(os.environ.get("NNCQ_GPU_BREAK_MS", "100"))
# VRAM nncq never takes: each process is capped at total minus this, and a GPU pass is split in halves
# while less than this is free on the card (other apps grew) or when it runs out of memory.
RESERVE_MB = int(os.environ.get("NNCQ_GPU_RESERVE_MB", "1024"))
_last_break = time.time()
_capped = False


def _cap_vram() -> None:
    global _capped
    if _capped:
        return
    _capped = True
    torch = sys.modules.get("torch")
    if RESERVE_MB <= 0 or torch is None or not torch.cuda.is_available():
        return
    total = torch.cuda.get_device_properties(0).total_memory
    torch.cuda.set_per_process_memory_fraction(max(0.2, 1 - RESERVE_MB * 2**20 / total))


def _guarded(g: list[int], run) -> None:
    """run(g) on the GPU; split g in halves while the card is short of free VRAM or on out-of-memory."""
    torch = sys.modules.get("torch")
    cuda = torch is not None and torch.cuda.is_available()
    if cuda and RESERVE_MB > 0 and len(g) > 1 and torch.cuda.mem_get_info()[0] < RESERVE_MB * 2**20:
        torch.cuda.empty_cache()
        if torch.cuda.mem_get_info()[0] < RESERVE_MB * 2**20:
            h = len(g) // 2
            _guarded(g[:h], run)
            _guarded(g[h:], run)
            return
    try:
        run(g)
    except Exception as e:
        if not (cuda and isinstance(e, torch.cuda.OutOfMemoryError)) or len(g) == 1:
            raise
        torch.cuda.empty_cache()
        h = len(g) // 2
        _guarded(g[:h], run)
        _guarded(g[h:], run)


def gpu_break() -> None:
    """Give the GPU up for BREAK_MS if BREAK_EVERY_S has passed. Call only after the GPU work was synced."""
    global _last_break
    if BREAK_MS > 0 and time.time() - _last_break >= BREAK_EVERY_S:
        time.sleep(BREAK_MS / 1000)
        _last_break = time.time()
MAX_BATCH = 256
MAX_TOKENS = 2048          # per text; chunks are ~3000 chars, so this rarely truncates
RERANK_MAX_CHARS = 6000


def gpu_dtype():
    """NNCQ_DTYPE: bf16 (default) or fp32. Only applies on CUDA; CPU always runs fp32."""
    import torch
    return torch.float32 if os.environ.get("NNCQ_DTYPE", "bf16").lower() in ("fp32", "float32") \
        else torch.bfloat16


def resolve_device(force_cpu: bool = False) -> str:
    want = config.settings().device
    if force_cpu or want == "cpu":
        return "cpu"
    import torch
    if want.startswith("cuda") or want == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    return want


def token_batches(lengths: list[int], budget: int, cap: int) -> list[list[int]]:
    """Indices grouped longest-first so each group's count x longest stays within budget."""
    groups: list[list[int]] = []
    for i in sorted(range(len(lengths)), key=lambda i: -lengths[i]):
        g = groups[-1] if groups else None
        if g and len(g) < cap and (len(g) + 1) * lengths[g[0]] <= budget:
            g.append(i)
        else:
            groups.append([i])
    return groups


def fast_attention():
    """Memory-linear attention kernels. PyTorch's flash kernel is missing on Windows, and the
    fallback materializes the full length x length score matrix."""
    torch = sys.modules.get("torch")
    if torch is None or not torch.cuda.is_available():
        return contextlib.nullcontext()
    from torch.nn.attention import SDPBackend, sdpa_kernel
    return sdpa_kernel([SDPBackend.FLASH_ATTENTION, SDPBackend.EFFICIENT_ATTENTION,
                        SDPBackend.CUDNN_ATTENTION, SDPBackend.MATH])


def _free_cache() -> None:
    torch = sys.modules.get("torch")
    if torch is not None and torch.cuda.is_available():
        torch.cuda.empty_cache()


def _renorm(m: np.ndarray, dim: int) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)[:, :dim]
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return m / n


class _Lazy:
    """A model loaded on first use and dropped after `idle` seconds without use."""

    def __init__(self, name: str, idle: int, device: str | None = None) -> None:
        self.name, self.idle, self._device = name, idle, device
        self.model = None
        self.device = None
        self.last_used = 0.0
        self.lock = threading.RLock()
        if idle > 0:
            threading.Thread(target=self._reaper, daemon=True, name=f"{name}-idle").start()

    def _reaper(self) -> None:
        while True:
            time.sleep(15)
            with self.lock:
                if self.model is not None and time.time() - self.last_used > self.idle:
                    self.unload()

    def unload(self) -> None:
        with self.lock:
            if self.model is not None:
                self.model = None
                import gc
                gc.collect()
                _free_cache()

    def get(self, force_cpu: bool = False):
        with self.lock:
            dev = self._device or resolve_device(force_cpu)
            if self.model is not None and self.device != dev:
                self.unload()
            if self.model is None:
                if dev.startswith("cuda"):
                    _cap_vram()
                self.model = self._load(dev)
                self.device = dev
            self.last_used = time.time()
            return self.model

    def _load(self, device: str):
        raise NotImplementedError


class Embedder(_Lazy):
    def __init__(self, idle: int | None = None, device: str | None = None) -> None:
        s = config.settings()
        self.dim = s.embed_dim
        super().__init__("embedder", s.model_idle_seconds if idle is None else idle, device)

    def _load(self, device: str):
        import torch
        from sentence_transformers import SentenceTransformer
        kw = {"dtype": gpu_dtype() if device.startswith("cuda") else torch.float32}
        m = SentenceTransformer(EMBED_MODEL, revision=EMBED_REV, trust_remote_code=True,
                                device=device, model_kwargs=kw)
        m.max_seq_length = min(m.max_seq_length or MAX_TOKENS, MAX_TOKENS)
        return m

    def encode_documents(self, texts: list[str], force_cpu: bool = False) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        with self.lock:
            m = self.get(force_cpu)
            lengths = [len(t) for t in m.tokenizer(texts, truncation=True,
                                                   max_length=m.max_seq_length)["input_ids"]]
            out = np.zeros((len(texts), self.dim), dtype=np.float32)
            try:
                with fast_attention():
                    def run(g):
                        v = m.encode_document([texts[i] for i in g], batch_size=len(g),
                                              convert_to_numpy=True, show_progress_bar=False)
                        out[g] = _renorm(v, self.dim)
                    for g in token_batches(lengths, BATCH_TOKENS, MAX_BATCH):
                        _guarded(g, run)
                        gpu_break()  # convert_to_numpy already waited for the GPU
            finally:
                _free_cache()
                self.last_used = time.time()
            return out

    def encode_query(self, text: str, force_cpu: bool = False) -> np.ndarray:
        with self.lock:
            m = self.get(force_cpu)
            with fast_attention():
                v = m.encode_query([text], convert_to_numpy=True, show_progress_bar=False)
            self.last_used = time.time()
            return _renorm(v, self.dim)[0]


class Reranker(_Lazy):
    def __init__(self, idle: int | None = None, device: str | None = None) -> None:
        s = config.settings()
        super().__init__("reranker", s.model_idle_seconds if idle is None else idle, device)
        self.tokenizer = None

    def _load(self, device: str):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        self.tokenizer = AutoTokenizer.from_pretrained(RERANK_MODEL, revision=RERANK_REV,
                                                       trust_remote_code=True)
        m = AutoModelForSequenceClassification.from_pretrained(
            RERANK_MODEL, revision=RERANK_REV, trust_remote_code=True,
            dtype=gpu_dtype() if device.startswith("cuda") else torch.float32,
        ).to(device)
        m.eval()
        return m

    def score(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        import torch
        with self.lock:
            m = self.get()
            tok = self.tokenizer
            texts = [f"question:{query}\n\npassage:{p[:RERANK_MAX_CHARS]}" for p in passages]
            lengths = [len(i) for i in tok(texts, truncation=True, max_length=MAX_TOKENS)["input_ids"]]
            scores = [0.0] * len(texts)
            try:
                def run(g):
                    batch = tok([texts[i] for i in g], return_tensors="pt", truncation=True,
                                max_length=MAX_TOKENS, padding=True).to(m.device)
                    with torch.no_grad(), fast_attention():
                        logits = m(**batch).logits.reshape(-1).float().cpu().tolist()
                    for i, s in zip(g, logits):
                        scores[i] = s
                for g in token_batches(lengths, BATCH_TOKENS, len(texts)):
                    _guarded(g, run)
            finally:
                _free_cache()
                self.last_used = time.time()
            return scores


def models_present() -> list[str]:
    """Names of pinned models missing from ROOT/models/hf (empty list = all there)."""
    hub = config.HF_HOME / "hub"
    missing = []
    for name, rev in PINNED:
        snap = hub / ("models--" + name.replace("/", "--")) / "snapshots" / rev
        if not (snap.exists() and any(snap.glob("*.safetensors"))):
            missing.append(name)
    return missing


def fetch() -> int:
    """Download the pinned models into ROOT/models/hf (the only step that touches the network)."""
    env = {**os.environ, "HF_HUB_OFFLINE": "0", "TRANSFORMERS_OFFLINE": "0",
           "HF_HOME": str(config.HF_HOME)}
    return subprocess.call([sys.executable, "-m", "nncq.models", "--fetch"], env=env)


def _fetch_main() -> None:
    from huggingface_hub import snapshot_download
    for name, rev in PINNED:
        print(f"fetching {name}@{rev[:10]} ...", flush=True)
        p = snapshot_download(name, revision=rev)
        print(f"  -> {p}", flush=True)


if __name__ == "__main__" and "--fetch" in sys.argv:
    _fetch_main()
