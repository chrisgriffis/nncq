"""Focused tests: chunking, deltas (incl. vector reuse), graph views, degradation notes. No GPU / models needed."""
from __future__ import annotations

import json

import numpy as np
import pytest

from nncq import answers, chunker, config, graph, health, indexer
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


PP = """namespace app {
#if FB_USING(NEW_PATH)
void Renderer::onFrame(int a)
#else
void Renderer::onFrame(int a, int b)
#endif
{
    present(a);
#if 0
    legacyPresent();
#elif defined(CONSOLE)
    consolePresent();
#endif
}

void Renderer::onFlip() { flip(); }
}
"""


def test_preprocessor_branches_keep_one_parse_and_recover_the_rest():
    r = chunker.process("r.cpp", PP.encode(), "cpp")
    ext = {s.qualname: (s.start_line, s.end_line) for s in r.symbols}
    assert ext["app::Renderer::onFrame"] == (3, 14)        # not swallowing onFlip
    assert ext["app::Renderer::onFlip"] == (16, 16)
    calls = {(e.src, e.dst) for e in r.edges if e.kind == "call"}
    assert ("app::Renderer::onFrame", "present") in calls
    assert ("app::Renderer::onFrame", "consolePresent") in calls    # from the branch the parser did not see
    assert not any(e.src == "" for e in r.edges if e.kind == "call")


def test_callers_stay_in_the_language_family(repo):
    (repo.root / "scripts" / "sim.lua").write_text("function tick(b)\n  integrate(b, 1)\nend\n", encoding="utf-8")
    indexer.update_paths(repo, ["scripts/sim.lua"], log=lambda m: None)
    st = _store(repo)
    try:
        assert [c.split()[0] for c in graph.called_by(st, "integrate")["callers"]] == ["eng::stepWorld"]
        assert graph.called_by(st, "integrate", caller_path="nothing/here")["callers"] == []
    finally:
        st.close()


def test_shared_names_resolve_to_the_callers_own_class(repo):
    (repo.root / "engine" / "loop.cpp").write_text(
        "void A::update() {}\nvoid A::tick() { update(); }\nvoid B::update() {}\nvoid B::run() { update(); }\n"
        "void Test_A::update() {}\nvoid TEST_F() { A a; a.update(); }\nvoid C::poke(A* a) { a->update(); }\n"
        "void A::flush() { m_child->update(); }\n",
        encoding="utf-8")
    indexer.update_paths(repo, ["engine/loop.cpp"], log=lambda m: None)
    st = _store(repo)
    try:
        assert graph.calls(st, "A::tick")["calls"][0].split(" -> ")[1].startswith("A::update")
        assert "(ambiguous)" in graph.calls(st, "A::flush")["calls"][0]          # m_child->update(): not ours
        rows = graph.called_by(st, "A::update")["callers"]
        names = [r.split()[0] for r in rows]
        assert names[0] == "A::tick" and set(names[1:3]) == {"A::flush", "C::poke"} and names[3] == "TEST_F"
        assert "[same class]" in rows[0] and not any("[same class]" in r for r in rows[1:])   # flush: m_child->
        assert rows[-1].endswith("(test)")
    finally:
        st.close()


def test_long_answers_are_paged_and_ask_is_served_from_a_larger_k(monkeypatch):
    monkeypatch.setenv("NNCQ_CACHE_MINUTES", "5")
    big = {"tree": "\n".join(f"line {i} " + "x" * 60 for i in range(400)), "note": "n"}
    out = answers.fit(big, "call_tree", 4096)
    assert answers.size(out) <= 4096 and "tree" in out["paged"]
    lines, nxt = out["tree"].split("\n"), out
    while "paged" in nxt:
        nxt = answers.more(nxt["paged"]["tree"].split("'")[1])
        lines += nxt["tree"].split("\n")
    assert lines == big["tree"].split("\n")

    hits = [{"ref": f"c{i}"} for i in range(8)]
    full = {"hits": hits, "links": ["c1 f calls c7 g (line 3)", "c0 f calls c1 g (line 2)"],
            "portfolio": {"shown": "8 of 30 distinct", "areas": ["A: 2 (c0, c1)", "B: 1 (c7)"]}}
    args = {"question": "q", "k": 8, "path": "", "lang": "", "repo": ""}
    key = answers.key("ask", args)
    answers.put(key, args, full)
    small = answers.get(key, {**args, "k": 3})
    assert [h["ref"] for h in small["hits"]] == ["c0", "c1", "c2"]
    assert small["links"] == ["c0 f calls c1 g (line 2)"] and small["portfolio"]["areas"] == ["A: 2 (c0, c1)"]
    assert answers.get(key, {**args, "k": 12}) is None
    answers.bump()
    assert answers.get(key, args) is None


def test_env_inline_comments(tmp_path):
    f = tmp_path / ".env"
    f.write_text('A=1          # port\nB="x # y"\nC=\n# D=4\n', encoding="utf-8")
    assert config.read_env_file(f) == {"A": "1", "B": "x # y", "C": ""}


def test_lite_profile_never_touches_models_and_reports_no_degradation(repo, monkeypatch):
    import sys
    from nncq import ask as asker, search
    monkeypatch.setenv("NNCQ_PROFILE", "lite")
    monkeypatch.setenv("NNCQ_EMBED_TIER", "2")           # ignored under lite
    s = config.settings()
    assert s.lite and s.embed_tier == 0 and not s.rerank
    monkeypatch.setattr(search, "_model", lambda name: pytest.fail("lite loaded a model"))
    st = _store(repo)
    try:
        out = asker.ask(st, repo.root, "integrate bodies over time", 5)
        assert "eng::integrate" in [h["sym"] for h in out["hits"]]
        assert out["portfolio"]["strength"].startswith("unrated (lite")
        assert out["interpreted"]["meaning"].startswith("off (lite")
        assert "note" not in out                          # no "no vectors yet" noise in lite
        assert health.compute(st, s.embed_tier) == (None, [])
    finally:
        st.close()
    assert "torch" not in sys.modules and "transformers" not in sys.modules


def test_agents_setup_merges_configs_and_installs_skill(tmp_path, monkeypatch):
    import json
    from nncq import agents
    monkeypatch.setenv("NNCQ_AGENT_HOME", str(tmp_path))
    cfg = tmp_path / ".copilot" / "mcp-config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"other": {"type": "local", "command": "x"}}}), encoding="utf-8")
    assert agents.run(["copilot", "claude", "vs"], ask=False) == 0
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert set(data["mcpServers"]) == {"other", "nncq"} and data["mcpServers"]["nncq"]["args"] == ["serve", "--stdio"]
    assert (cfg.parent / "mcp-config.json.bak-nncq").exists()
    assert json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["servers"]["nncq"]["type"] == "stdio"
    for d in (".copilot", ".claude"):
        skill = (tmp_path / d / "skills" / "nncq-user" / "SKILL.md").read_text(encoding="utf-8")
        assert skill.startswith("---\nname: nncq-user\n")
    assert "already configured" in agents.setup("copilot")[0]     # idempotent
    assert agents.run(["nope"], ask=False) == 1

# ---------------------------------------------------------------- codebase profiles

PROFILE = """name = 'acme'
[workspace]
roots = ['{root}']
markers = ['build.txt']
dirs = []
ignore = ['vendor/**']

[[dsl]]
lang = 'idl'
extensions = ['.idl']

[[xref]]
name = 'scenes'
glob = '*.scene'
instance = '<node type="(?P<type>[A-Za-z_]+)"'
reference = 'ref="(?P<ref>[^"]+)"'
type_suffix = 'Data'
label = 'scene'
"""


@pytest.fixture
def prof(tmp_path, monkeypatch):
    from nncq import profiles
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "db")
    monkeypatch.setattr(profiles, "DIR", tmp_path / "profiles")
    monkeypatch.setattr(profiles, "ARCHIVE_DIR", tmp_path / "archive")
    monkeypatch.setenv("NNCQ_INDEX_PROCS", "1")
    monkeypatch.setenv("NNCQ_DATA", "true")
    ws = tmp_path / "acme"
    (ws / "src").mkdir(parents=True)
    (ws / "vendor").mkdir()
    (ws / "build.txt").write_text("", encoding="utf-8")
    (ws / "src" / "door.cpp").write_text("struct Door { void open(); };\nvoid Door::open() {}\n", encoding="utf-8")
    (ws / "src" / "door.idl").write_text("component DoorData {\n  bool locked;\n}\n", encoding="utf-8")
    (ws / "vendor" / "lib.cpp").write_text("void vendored() {}\n", encoding="utf-8")
    (ws / "a.scene").write_text('<node type="DoorData" ref="b"/>\n', encoding="utf-8")
    (ws / "b.scene").write_text('<node type="DoorData"/>\n', encoding="utf-8")
    d = profiles.DIR / "acme"
    d.mkdir(parents=True)
    (d / "profile.toml").write_text(PROFILE.format(root=ws.as_posix()), encoding="utf-8")
    return ws


def test_profile_links_its_workspace_and_drives_dsl_ignore_and_data_rules(prof, tmp_path):
    from nncq import datarefs, profiles, repos, search
    p = profiles.get("acme")
    assert p.root == prof.resolve()
    assert "missing marker" in p.accepts(tmp_path)
    assert "overlaps" in p.accepts(config.ROOT)
    r = repos.get("acme")
    indexer.update(r, log=lambda m: None)
    st = Store(r.db, cache_mb=8)
    try:
        paths = set(st.file_stats())
        assert "src/door.idl" in paths and "vendor/lib.cpp" not in paths
        d = search.find_symbol(st, "DoorData", "", "")
        assert d["match"] == "exact" and "door.idl" in d["definitions"][0]
        uses = json.dumps(datarefs.data_for_code(st, "Door", 10))
        assert "a.scene" in uses and "b.scene" in uses
        assert "a.scene" in json.dumps(datarefs.asset_info(st, "b.scene", 10))
    finally:
        st.close()


def test_profile_export_import_round_trip(prof):
    from nncq import profiles
    f = profiles.export("acme")
    assert f.parent == profiles.ARCHIVE_DIR and f.suffix == profiles.ARCHIVE_EXT
    with pytest.raises(ValueError):
        profiles.import_(f)
    dst = profiles.import_(f, "acme2")
    assert profiles.get("acme2").root == prof.resolve()
    assert "name = 'acme2'" in (dst / "profile.toml").read_text(encoding="utf-8")


def test_results_citing_files_changed_on_disk_are_flagged(repo):
    st = _store(repo)
    try:
        assert health.divergence(st, repo.root, ["engine/physics.cpp"]) == []
        (repo.root / "engine" / "physics.cpp").write_text(CPP + "\n// edited\n", encoding="utf-8")
        (repo.root / "scripts" / "spawner.lua").unlink()
        div = health.divergence(st, repo.root, health.result_paths(
            {"results": [{"path": "engine/physics.cpp"}, {"file": "scripts/spawner.lua"}]}))
        assert [d["path"] for d in div] == ["engine/physics.cpp", "scripts/spawner.lua"]
        assert div[1]["state"] == "missing on disk"
    finally:
        st.close()
