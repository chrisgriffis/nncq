"""Focused tests: chunking, deltas (incl. vector reuse), graph views, degradation notes. No GPU / models needed."""
from __future__ import annotations

import numpy as np
import pytest

from nncq import chunker, config, graph, health, indexer
from nncq.repos import Repo
from nncq.store import Store

CPP = """#include "physics.h"
namespace eng {
struct Body { float x, v; };

void
integrate(Body& b, float dt)
{
    b.x += b.v * dt;
}

void
stepWorld(Body* bodies, int n, float dt)
{
    for (int i = 0; i < n; ++i)
        integrate(bodies[i], dt);
}

int
mainLoop()
{
    Body b[4] = {};
    stepWorld(b, 4, 0.016f);
    return 0;
}
}
"""

LUA = """local M = {}

local function clamp(v, lo, hi)
  if v < lo then return lo end
  return v
end

function M.spawn(world, kind)
  local e = world:create(kind)
  M.applyDefaults(e)
  return e
end

function M.applyDefaults(e)
  e.speed = clamp(e.speed, 0, 10)
end

return M
"""


def test_namespace_is_split_into_functions_with_full_signatures():
    r = chunker.process("physics.cpp", CPP.encode(), "cpp")
    syms = {c.symbol for c in r.chunks}
    assert {"eng::integrate", "eng::stepWorld", "eng::mainLoop", "eng::Body"} <= syms
    assert "eng" not in syms                     # no header-only / punctuation chunks for the namespace
    lines = [(c.start_line, c.end_line) for c in r.chunks]
    assert len(lines) == len(set(lines))          # nothing duplicated
    sig = {s.qualname: s.signature for s in r.symbols}
    assert sig["eng::integrate"] == "void integrate(Body& b, float dt)"


def test_lua_methods_and_calls():
    r = chunker.process("spawner.lua", LUA.encode(), "lua")
    kinds = {s.qualname: (s.kind, s.parent) for s in r.symbols}
    assert kinds["M.spawn"] == ("method", "M")
    assert ("M.spawn", "applyDefaults") in {(e.src, e.dst) for e in r.edges if e.kind == "call"}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "db")
    monkeypatch.setenv("NNCQ_INDEX_PROCS", "1")
    monkeypatch.setenv("NNCQ_DATA", "false")
    root = tmp_path / "ws"
    (root / "engine").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "engine" / "physics.cpp").write_text(CPP, encoding="utf-8")
    (root / "scripts" / "spawner.lua").write_text(LUA, encoding="utf-8")
    r = Repo("t", root)
    indexer.update(r, log=lambda m: None)
    return r


def _store(r):
    return Store(r.db, cache_mb=8)


def test_call_trace_and_called_by(repo):
    st = _store(repo)
    try:
        t = graph.call_trace(st, "integrate")
        assert [f.split()[1] for f in t["stacks"][0]] == ["integrate", "eng::stepWorld", "eng::mainLoop"]
        rel = graph.call_trace(st, "clamp", frm="M.spawn")
        assert rel["from_reaches_to"]
        assert [x.split()[0] for x in graph.called_by(st, "applyDefaults")["callers"]] == ["M.spawn"]
    finally:
        st.close()


def test_delta_reuses_vectors_and_updates_graph(repo):
    st = _store(repo)
    try:
        ids = dict(st.q("SELECT c.symbol, c.id FROM chunks c"))
        with st.tx() as db:                      # pretend the worker embedded everything
            for cid in ids.values():
                db.execute("UPDATE chunks SET tier=2 WHERE id=?", (cid,))
                db.execute("INSERT INTO vecs(chunk_id, tier, v) VALUES(?,?,?)",
                           (cid, 2, np.ones(4, np.float16).tobytes()))
    finally:
        st.close()
    p = repo.root / "engine" / "physics.cpp"
    p.write_text(CPP.replace("int\nmainLoop()", "void\nsubstep(Body& b, float dt)\n{\n    integrate(b, dt);\n}\n\n"
                             "int\nmainLoop()"), encoding="utf-8")
    res = indexer.update_paths(repo, ["engine/physics.cpp"], log=lambda m: None)
    assert res["changed"] == 1
    st = _store(repo)
    try:
        tiers = dict(st.q("SELECT c.symbol, c.tier FROM chunks c"))
        assert tiers["eng::integrate"] == 2 and tiers["eng::stepWorld"] == 2   # unchanged text: vector kept
        assert tiers["eng::substep"] == 0                                     # new code: pending
        assert any("eng::substep" in c for c in graph.called_by(st, "integrate")["callers"])
        st_, _ = health.compute(st, 2)
        assert any("not embedded yet" in d and "engine/physics.cpp" in d for d in st_["degraded"])
        out = health.annotate({}, st, ["engine/physics.cpp"], 2, semantic=True)
        assert "1 of 1" in out["index_state"]["affects_these_results"]
        assert "index_state" not in health.annotate({}, st, None, 2, semantic=False)
    finally:
        st.close()


def test_env_inline_comments(tmp_path):
    f = tmp_path / ".env"
    f.write_text('A=1          # port\nB="x # y"\nC=\n# D=4\n', encoding="utf-8")
    assert config.read_env_file(f) == {"A": "1", "B": "x # y", "C": ""}
