"""Focused tests: chunking, deltas (incl. vector reuse), graph views, degradation notes. No GPU / models needed."""
from __future__ import annotations

import json

import numpy as np
import pytest

from nncq import answers, chunker, config, graph, health, indexer
from nncq.repos import Repo
from nncq.store import Store

CPP = """#include "billing.h"
namespace acct {
struct Account { double balance, rate; };

void
accrue(Account& a, double days)
{
    a.balance += a.rate * days;
}

void
accrueAll(Account* accounts, int n, double days)
{
    for (int i = 0; i < n; ++i)
        accrue(accounts[i], days);
}

int
runBatch()
{
    Account a[4] = {};
    accrueAll(a, 4, 30.0);
    return 0;
}
}
"""

LUA = """local M = {}

local function clamp(v, lo, hi)
  if v < lo then return lo end
  return v
end

function M.open(pool, kind)
  local c = pool:create(kind)
  M.applyDefaults(c)
  return c
end

function M.applyDefaults(c)
  c.retries = clamp(c.retries, 0, 10)
end

return M
"""


def test_namespace_is_split_into_functions_with_full_signatures():
    r = chunker.process("billing.cpp", CPP.encode(), "cpp")
    syms = {c.symbol for c in r.chunks}
    assert {"acct::accrue", "acct::accrueAll", "acct::runBatch", "acct::Account"} <= syms
    assert "acct" not in syms                     # no header-only / punctuation chunks for the namespace
    lines = [(c.start_line, c.end_line) for c in r.chunks]
    assert len(lines) == len(set(lines))          # nothing duplicated
    sig = {s.qualname: s.signature for s in r.symbols}
    assert sig["acct::accrue"] == "void accrue(Account& a, double days)"


def test_lua_methods_and_calls():
    r = chunker.process("pool.lua", LUA.encode(), "lua")
    kinds = {s.qualname: (s.kind, s.parent) for s in r.symbols}
    assert kinds["M.open"] == ("method", "M")
    assert ("M.open", "applyDefaults") in {(e.src, e.dst) for e in r.edges if e.kind == "call"}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "db")
    monkeypatch.setenv("NNCQ_INDEX_PROCS", "1")
    monkeypatch.setenv("NNCQ_DATA", "false")
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "src" / "billing.cpp").write_text(CPP, encoding="utf-8")
    (root / "scripts" / "pool.lua").write_text(LUA, encoding="utf-8")
    r = Repo("t", root)
    indexer.update(r, log=lambda m: None)
    return r


def _store(r):
    return Store(r.db, cache_mb=8)


def test_call_trace_and_called_by(repo):
    st = _store(repo)
    try:
        t = graph.call_trace(st, "accrue")
        assert [f.split()[1] for f in t["stacks"][0]] == ["accrue", "acct::accrueAll", "acct::runBatch"]
        rel = graph.call_trace(st, "clamp", frm="M.open")
        assert rel["from_reaches_to"]
        assert [x.split()[0] for x in graph.called_by(st, "applyDefaults")["callers"]] == ["M.open"]
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
    p = repo.root / "src" / "billing.cpp"
    p.write_text(CPP.replace("int\nrunBatch()", "void\naccrueOne(Account& a, double days)\n{\n    accrue(a, days);\n}\n\n"
                             "int\nrunBatch()"), encoding="utf-8")
    res = indexer.update_paths(repo, ["src/billing.cpp"], log=lambda m: None)
    assert res["changed"] == 1
    st = _store(repo)
    try:
        tiers = dict(st.q("SELECT c.symbol, c.tier FROM chunks c"))
        assert tiers["acct::accrue"] == 2 and tiers["acct::accrueAll"] == 2   # unchanged text: vector kept
        assert tiers["acct::accrueOne"] == 0                                 # new code: pending
        assert any("acct::accrueOne" in c for c in graph.called_by(st, "accrue")["callers"])
        st_, _ = health.compute(st, 2)
        assert any("not embedded yet" in d and "src/billing.cpp" in d for d in st_["degraded"])
        out = health.annotate({}, st, ["src/billing.cpp"], 2, semantic=True)
        assert "1 of 1" in out["index_state"]["affects_these_results"]
        assert "index_state" not in health.annotate({}, st, None, 2, semantic=False)
    finally:
        st.close()


PP = """namespace app {
#if HAS_FEATURE(NEW_PATH)
void Server::onRequest(int a)
#else
void Server::onRequest(int a, int b)
#endif
{
    reply(a);
#if 0
    legacyReply();
#elif defined(USE_TLS)
    tlsReply();
#endif
}

void Server::onClose() { close(); }
}
"""


def test_preprocessor_branches_keep_one_parse_and_recover_the_rest():
    r = chunker.process("server.cpp", PP.encode(), "cpp")
    ext = {s.qualname: (s.start_line, s.end_line) for s in r.symbols}
    assert ext["app::Server::onRequest"] == (3, 14)        # not swallowing onClose
    assert ext["app::Server::onClose"] == (16, 16)
    calls = {(e.src, e.dst) for e in r.edges if e.kind == "call"}
    assert ("app::Server::onRequest", "reply") in calls
    assert ("app::Server::onRequest", "tlsReply") in calls    # from the branch the parser did not see
    assert not any(e.src == "" for e in r.edges if e.kind == "call")


def test_callers_stay_in_the_language_family(repo):
    (repo.root / "scripts" / "jobs.lua").write_text("function nightly(a)\n  accrue(a, 1)\nend\n", encoding="utf-8")
    indexer.update_paths(repo, ["scripts/jobs.lua"], log=lambda m: None)
    st = _store(repo)
    try:
        assert [c.split()[0] for c in graph.called_by(st, "accrue")["callers"]] == ["acct::accrueAll"]
        assert graph.called_by(st, "accrue", caller_path="nothing/here")["callers"] == []
    finally:
        st.close()


def test_shared_names_resolve_to_the_callers_own_class(repo):
    (repo.root / "src" / "shared.cpp").write_text(
        "void A::update() {}\nvoid A::refresh() { update(); }\nvoid B::update() {}\nvoid B::run() { update(); }\n"
        "void Test_A::update() {}\nvoid TEST_F() { A a; a.update(); }\nvoid C::poke(A* a) { a->update(); }\n"
        "void A::flush() { m_child->update(); }\n",
        encoding="utf-8")
    indexer.update_paths(repo, ["src/shared.cpp"], log=lambda m: None)
    st = _store(repo)
    try:
        assert graph.calls(st, "A::refresh")["calls"][0].split(" -> ")[1].startswith("A::update")
        assert "(ambiguous)" in graph.calls(st, "A::flush")["calls"][0]          # m_child->update(): not ours
        rows = graph.called_by(st, "A::update")["callers"]
        names = [r.split()[0] for r in rows]
        assert names[0] == "A::refresh" and set(names[1:3]) == {"A::flush", "C::poke"} and names[3] == "TEST_F"
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
        out = asker.ask(st, repo.root, "accrue interest on accounts", 5)
        assert "acct::accrue" in [h["sym"] for h in out["hits"]]
        assert out["portfolio"]["strength"].startswith("unrated (lite")
        assert out["interpreted"]["meaning"].startswith("off (lite")
        assert "note" not in out                          # no "no vectors yet" noise in lite
        assert health.compute(st, s.embed_tier) == (None, [])
    finally:
        st.close()
    assert "torch" not in sys.modules and "transformers" not in sys.modules


def test_hit_relevance_comes_from_raw_scores_not_rank():
    from nncq.ask import _relevance
    shown = [{"cid": i, "score": 0.0} for i in (1, 2, 3)]
    _relevance(shown, {1: -8.0, 2: -8.0, 3: -2.0}, {}, False)      # BM25: lower is better
    assert [it["rel"] for it in shown] == [1.0, 1.0, 0.25]       # ties stay ties, a weak hit stays weak


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
name = 'flows'
glob = '*.flow'
instance = '<step type="(?P<type>[A-Za-z_]+)"'
reference = 'next="(?P<ref>[^"]+)"'
type_suffix = 'Data'
label = 'flow'
"""


@pytest.fixture
def prof(tmp_path, monkeypatch):
    from nncq import profiles
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "db")
    monkeypatch.setattr(config, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setenv("NNCQ_INDEX_PROCS", "1")
    monkeypatch.setenv("NNCQ_DATA", "true")
    ws = tmp_path / "acme"
    (ws / "src").mkdir(parents=True)
    (ws / "vendor").mkdir()
    (ws / "build.txt").write_text("", encoding="utf-8")
    (ws / "src" / "order.cpp").write_text("struct Order { void submit(); };\nvoid Order::submit() {}\n", encoding="utf-8")
    (ws / "src" / "order.idl").write_text("struct OrderData {\n  bool paid;\n}\n", encoding="utf-8")
    (ws / "vendor" / "lib.cpp").write_text("void vendored() {}\n", encoding="utf-8")
    (ws / "a.flow").write_text('<step type="OrderData" next="b"/>\n', encoding="utf-8")
    (ws / "b.flow").write_text('<step type="OrderData"/>\n', encoding="utf-8")
    d = config.PROFILES_DIR / "acme"
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
        assert "src/order.idl" in paths and "vendor/lib.cpp" not in paths
        d = search.find_symbol(st, "OrderData", "", "")
        assert d["match"] == "exact" and "order.idl" in d["definitions"][0]
        uses = json.dumps(datarefs.data_for_code(st, "Order", 10))
        assert "a.flow" in uses and "b.flow" in uses
        assert "a.flow" in json.dumps(datarefs.data_file(st, "b.flow", 10))
    finally:
        st.close()


def test_profile_vcs_field_and_detection(prof, tmp_path):
    from nncq import profiles, repos, vcs
    p = profiles.get("acme")
    assert p.vcs == "" and repos.get("acme").vcs == "none"                   # not set: detected (plain folder)
    f = p.path
    f.write_text(f.read_text(encoding="utf-8").replace("[workspace]\n", "[workspace]\nvcs = 'p4'\n", 1), encoding="utf-8")
    assert profiles.parse(f).vcs == "p4" and repos.get("acme").vcs == "p4"
    assert vcs.for_root(prof, "p4").kind == "p4" and vcs.for_root(prof, "none").kind == "none"
    f.write_text(f.read_text(encoding="utf-8").replace("vcs = 'p4'", "vcs = 'svn'"), encoding="utf-8")
    with pytest.raises(ValueError):
        profiles.parse(f)
    g = tmp_path / "gitws"
    (g / ".git").mkdir(parents=True)
    assert vcs.detect(g) == "git" and vcs.detect(tmp_path / "acme") == "none"
    out = profiles.write("acme3", prof, [], [], vcs="git")
    assert profiles.parse(out).vcs == "git"


def test_profile_export_import_round_trip(prof):
    from nncq import profiles
    f = profiles.export("acme")
    assert f.parent == config.PROFILES_DIR / "archive" and f.suffix == profiles.ARCHIVE_EXT
    with pytest.raises(ValueError):
        profiles.import_(f)
    dst = profiles.import_(f, "acme2")
    assert profiles.get("acme2").root == prof.resolve()
    assert "name = 'acme2'" in (dst / "profile.toml").read_text(encoding="utf-8")


def _answers(monkeypatch, *xs):
    it = iter(xs)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(it))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)


def test_init_new_profile_takes_a_pasted_quoted_path_and_asks_for_its_name(prof, tmp_path, monkeypatch):
    from nncq import init, profiles
    _answers(monkeypatch, str(tmp_path / "nope"), f'  "{prof}"  ', "acme", "bad name!", "ws1", "Acme game", "", "", "")
    r = init.profile_wizard("", "", ask=True)
    p = profiles.get("ws1")
    assert r.name == "ws1" and p.root == prof.resolve() and p.description == "Acme game"


def test_init_imports_an_exported_profile_and_asks_where_its_workspace_is(prof, tmp_path, monkeypatch):
    import shutil
    import types
    from nncq import init, profiles
    src = config.PROFILES_DIR / "acme" / "profile.toml"
    src.write_text(src.read_text(encoding="utf-8").replace(prof.as_posix(), "Z:/elsewhere/acme"), encoding="utf-8")
    f = profiles.export("acme")
    shutil.rmtree(src.parent)
    _answers(monkeypatch, f'"{f}"', str(tmp_path), str(prof))      # import; wrong folder (no marker); right one
    a = types.SimpleNamespace(yes=False, profile="", name="", workspace="", dirs=None)
    r = init._workspace(a)
    assert r.name == "acme" and r.root == prof.resolve()
    assert profiles.get("acme").roots[0] == str(prof.resolve()).replace("\\", "/")


def test_demo_is_a_self_contained_home(tmp_path, monkeypatch):
    from nncq import profiles, repos
    monkeypatch.delenv("NNCQ_WORKSPACE_ORBITKIT", raising=False)
    monkeypatch.delenv("NNCQ_PORT", raising=False)
    monkeypatch.delenv("NNCQ_MODE", raising=False)
    try:
        config.use_home(config.DEMO)
        assert config.home_args() == ["--home", str(config.DEMO.resolve())]
        assert config.PROFILES_DIR == config.DEMO / "profiles" and config.DATA_DIR == config.DEMO / "cache" / "db"
        assert config.settings().lite and config.settings().port == 8766           # from demo/.env
        r = repos.get()
        assert r.name == "orbitkit" and r.root == (config.DEMO / "orbitkit").resolve()   # relative to the home
        assert r.db == config.DEMO / "cache" / "db" / "orbitkit" / "index.db"
        assert "overlaps" in r.profile.accepts(config.DEMO)              # only workspaces below demo/ are allowed
        assert profiles.archive_dir() == config.DEMO / "profiles" / "archive"
    finally:
        config.use_home(config.ROOT)
    assert config.home_args() == [] and "orbitkit" not in profiles.load_all()


def test_results_citing_files_changed_on_disk_are_flagged(repo):
    st = _store(repo)
    try:
        assert health.divergence(st, repo.root, ["src/billing.cpp"]) == []
        (repo.root / "src" / "billing.cpp").write_text(CPP + "\n// edited\n", encoding="utf-8")
        (repo.root / "scripts" / "pool.lua").unlink()
        div = health.divergence(st, repo.root, health.result_paths(
            {"results": [{"path": "src/billing.cpp"}, {"file": "scripts/pool.lua"}]}))
        assert [d["path"] for d in div] == ["src/billing.cpp", "scripts/pool.lua"]
        assert div[1]["state"] == "missing on disk"
    finally:
        st.close()


def test_public_mode_is_read_only(monkeypatch):
    import asyncio
    from nncq import server
    monkeypatch.setenv("NNCQ_PUBLIC", "1")
    mcp = server.build()
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names and "ask" in names and "uml_view" in names and not names & server.PUBLIC_DROP
    assert asyncio.run(server.web_tool("uml_build", {}))["error"].startswith("not available")
    assert server.control("pause")["error"] and not server.worker.paused()