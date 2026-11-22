"""UML model, views, compositions, renders and staleness. No GPU / network."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess

import pytest

from nncq import chunker, config, indexer, store as store_mod, vcs
from nncq.repos import Repo
from nncq.store import Store
from nncq.uml import catalog, extract, render_ascii, render_mermaid, views

DEV_HPP = """#pragma once
#include "Conn.hpp"
namespace dev {
class IDevice {
public:
    virtual ~IDevice() = default;
    virtual bool open(const std::string& host) = 0;
};
class Device : public IDevice {
public:
    static Device& instance();
    bool open(const std::string& host) override;
    void run();
private:
    std::unique_ptr<Conn> m_conn;
    std::vector<std::shared_ptr<Item>> m_items;
    Logger* m_log;
    static int s_count;
};
}
"""
DEV_CPP = """#include "Device.hpp"
namespace dev {
Device& Device::instance() { static Device s; return s; }
bool Device::open(const std::string& host) { m_conn->connect(host); return true; }
void Device::run() { open("x"); m_conn->send(); auto it = std::make_unique<Item>(); }
}
"""
CONN_HPP = """#pragma once
namespace dev {
class Conn { public: void connect(const std::string& h); void send(); };
class Item { public: int id; };
class Logger { public: void log(); };
}
"""
CONN_CPP = """#include "Conn.hpp"
namespace dev {
void Conn::connect(const std::string& h) { send(); }
void Conn::send() {}
}
"""
MAIN_CPP = """#include "Device.hpp"
int main() { dev::Device::instance().run(); return 0; }
"""
CMAKE = """project(Demo)
set(SRC ${CMAKE_CURRENT_LIST_DIR}/src/Device.cpp ${CMAKE_CURRENT_LIST_DIR}/src/Conn.cpp)
add_library(DevLib STATIC ${SRC})
target_include_directories(DevLib PUBLIC ${CMAKE_CURRENT_LIST_DIR}/include)
target_link_libraries(DevLib PUBLIC spdlog)
add_executable(DevCLI app/main.cpp)
target_link_libraries(DevCLI PRIVATE DevLib)
option(DEV_FAST "go fast" ON)
"""
PY = """from .store import Store

class Service:
    def __init__(self, store: Store):
        self.store = store
        self._cache = Cache()
        self.__hits: Optional[int] = None

    @staticmethod
    def make() -> "Service":
        return Service(Store())
"""


def test_cpp_members_visibility_relations_and_stereotypes():
    r = chunker.process("Device.hpp", DEV_HPP.encode(), "cpp", uml=True)
    s = {x.qualname: x for x in r.symbols}
    assert s["dev::IDevice"].stereo == "interface"
    assert "singleton" in s["dev::Device"].stereo
    o = s["dev::Device::open"]
    assert (o.kind, o.visibility, o.rtype, o.params) == ("method", "public", "bool", "const std::string& host")
    assert {"decl", "override"} <= set(o.mods.split())
    assert (s["dev::Device::m_conn"].kind, s["dev::Device::m_conn"].visibility) == ("field", "private")
    assert "static" in s["dev::Device::s_count"].mods
    rel = {(e.kind, e.dst, e.role) for e in r.edges if e.src == "dev::Device"}
    assert {("composes", "Conn", "m_conn"), ("aggregates", "Item", "m_items"), ("associates", "Logger", "m_log"),
            ("inherits", "IDevice", "")} <= rel
    assert extract.multiplicity("std::vector<std::shared_ptr<Item>>") == "*"
    assert extract.multiplicity("Logger*") == "0..1"
    r = chunker.process("Device.cpp", DEV_CPP.encode(), "cpp", uml=True)
    calls = [(e.dst, e.role) for e in r.edges if e.kind == "call" and e.src == "dev::Device::run"]
    assert calls[:2] == [("open", ""), ("send", "m_conn")]
    assert ("instantiates", "Item") in {(e.kind, e.dst) for e in r.edges if e.src == "dev::Device::run"}
    assert chunker.process("Device.hpp", DEV_HPP.encode(), "cpp").symbols[0].visibility == ""   # uml off: unchanged


def test_python_fields_relations_and_imports():
    r = chunker.process("svc/service.py", PY.encode(), "python", uml=True)
    f = {x.name: x for x in r.symbols if x.kind == "field"}
    assert (f["store"].rtype, f["_cache"].visibility, f["__hits"].visibility) == ("Store", "protected", "private")
    rel = {(e.kind, e.dst) for e in r.edges if e.src == "Service"}
    assert {("aggregates", "Store"), ("composes", "Cache")} <= rel
    assert "static" in next(x for x in r.symbols if x.name == "make").mods
    assert any(e.kind == "import" and e.dst == ".store" for e in r.edges)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "db")
    monkeypatch.setenv("NNCQ_INDEX_PROCS", "1")
    monkeypatch.setenv("NNCQ_DATA", "false")
    monkeypatch.setenv("NNCQ_UML", "true")
    root = tmp_path / "ws"
    for rel, text in (("lib/include/Device.hpp", DEV_HPP), ("lib/include/Conn.hpp", CONN_HPP),
                      ("lib/src/Device.cpp", DEV_CPP), ("lib/src/Conn.cpp", CONN_CPP), ("lib/app/main.cpp", MAIN_CPP),
                      ("lib/CMakeLists.txt", CMAKE), ("py/svc/service.py", PY), ("py/svc/__init__.py", ""),
                      ("py/svc/store.py", "class Store:\n    pass\n")):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8", newline="\n")
    r = Repo("t", root)
    indexer.update(r, log=lambda m: None)
    return r


def _st(r):
    return Store(r.db, cache_mb=8)


def test_model_components_and_generated_views(repo):
    st = _st(repo)
    try:
        comps = dict(st.q("SELECT name, kind FROM components"))
        assert comps["DevLib"] == "library" and comps["DevCLI"] == "executable" and comps["svc"] == "package"
        own = dict(st.q("SELECT f.path, c.name FROM comp_files cf JOIN files f ON f.id=cf.file_id "
                        "JOIN components c ON c.id=cf.comp_id"))
        assert own["lib/include/Device.hpp"] == "DevLib" and own["lib/app/main.cpp"] == "DevCLI"

        m, deps, _ = views.class_view(st, "Device")
        n = {x["label"]: x for x in m["nodes"]}
        assert {"Device", "IDevice", "Conn", "Item", "Logger"} <= set(n)
        kinds = {(e["kind"], n_["label"]) for e in m["edges"] for n_ in m["nodes"] if n_["id"] == e["to"]}
        assert {("realizes", "IDevice"), ("composes", "Conn"), ("aggregates", "Item")} <= kinds
        mem = {x["name"]: x for x in n["Device"]["members"]}
        assert mem["open"]["vis"] == "+" and mem["open"]["impl"]["path"] == "lib/src/Device.cpp"
        assert n["Device"]["ref"]["abs"].endswith("Device.hpp") and n["Device"]["ref"]["col"] == 0
        assert ("lib/include/Device.hpp", "dev::Device") in deps

        m, _, _ = views.component_view(st)
        dev = next(x for x in m["nodes"] if x["label"] == "DevLib")
        cli = next(x for x in m["nodes"] if x["label"] == "DevCLI")
        assert any(p["name"] == "Device.hpp" and p["dir"] == "provided" for p in dev["ports"])
        assert any(p["dir"] == "required" and p["iface"] == "DevLib" for p in cli["ports"])
        assert any(e["from"] == cli["id"] and e["to"] == dev["id"] for e in m["edges"])

        m, _, _ = views.sequence_view(st, "Device::run")
        msgs = [(x["label"], x["kind"]) for x in m["messages"]]
        assert msgs[:3] == [("run()", "call"), ("open()", "self"), ("connect()", "call")]
        assert ("send()", "call") in msgs

        m, _, _ = views.deployment_view(st)
        labels = {x["label"] for x in m["nodes"]}
        assert {"DevCLI.exe", "DevLib", "spdlog", "Release"} <= labels

        mmd = render_mermaid.render(views.class_view(st, "Device")[0])
        assert mmd.startswith("classDiagram") and "n_IDevice <|.. n_Device" in mmd
        txt = render_ascii.render(views.class_view(st, "Device")[0])
        assert "<<singleton>>" in txt and "Device --|>" not in txt and "..|> IDevice" in txt
    finally:
        st.close()


def test_file_scoped_and_default_views(repo):
    st = _st(repo)
    try:
        m, deps, _ = views.class_view(st, path="lib/include/Conn.hpp")
        assert {x["label"] for x in m["nodes"]} >= {"Conn", "Item", "Logger"} and "Device" not in {
            x["label"] for x in m["nodes"]} and ("lib/include/Conn.hpp", "@file") in deps
        m, _, _ = views.class_view(st, path="lib/src/Device.cpp")          # source: the types it implements
        assert "Device" in {x["label"] for x in m["nodes"]}
        m, _, _ = views.class_view(st, path="py/svc/service.py")
        mod = next(x for x in m["nodes"] if x["type"] == "module" and x["label"] == "service")
        assert any(e["from"] == mod["id"] and e["kind"] == "imports" for e in m["edges"])
        assert any(e["from"] == mod["id"] and e["kind"] == "contains" for e in m["edges"])
        assert render_mermaid.render(m).startswith("classDiagram") and render_ascii.render(m)
        m, _, _ = views.component_view(st, path="lib/CMakeLists.txt")
        assert {"DevLib", "DevCLI"} <= {x["label"] for x in m["nodes"]}
        m, _, _ = views.component_view(st, path="lib/app/main.cpp")
        assert "DevCLI" in {x["label"] for x in m["nodes"]}

        pick = lambda **a: (lambda d: (d["kind"], d["key"]))(catalog.view(st, "default", fmt="json", **a))
        assert pick(subject="dev::Device") == ("class", "class:dev::Device")
        assert pick(subject="dev::Device::run")[0] == "sequence"
        assert pick(path="lib/include/Conn.hpp") == ("class", "class:*|path=lib/include/Conn.hpp")
        assert pick(path="lib/app/main.cpp") == ("component", "component:*|path=lib/app/main.cpp")
        assert pick(path="lib/CMakeLists.txt")[0] == "component"
        assert pick(path="py/svc")[0] == "package" and pick()[1] == "component:*"
        assert catalog.view(st, "default", "dev::Device", fmt="json")["default_for"]["kind"] == "class"
    finally:
        st.close()


def test_ascii_is_bounded():
    m = {"kind": "class", "title": "big", "nodes": [{"id": f"n{i}", "label": f"C{i}", "members": []}
                                                   for i in range(50)], "edges": []}
    out = render_ascii.render(m)
    assert "+20 more elements" in out and "markdown export" in out


def test_agent_views_compositions_list_and_markdown(repo):
    st = _st(repo)
    try:
        v = {"key": "devflow", "kind": "class", "title": "Device flow", "auto_edges": True,
             "nodes": [{"id": "d", "ref": "sym:dev::Device"}, {"id": "c", "ref": "sym:dev::Conn"},
                       {"id": "u", "label": "User", "type": "actor"}, {"id": "g", "ref": "sym:dev::Gone"}],
             "edges": [{"from": "u", "to": "d", "kind": "uses"}, {"from": "c", "to": "d", "kind": "calls"}]}
        out = catalog.upsert(st, v)
        assert out["result"] == "created" and out["unbound"] == [{"id": "g", "ref": "sym:dev::Gone"}]
        assert out["unverified_edges"] == ["c -calls-> d"]
        comp = {"id": "access", "name": "Access", "members": [
            {"id": "lib", "ref": "comp:DevLib"}, {"id": "view", "ref": "view:devflow"},
            {"id": "farm", "bespoke": {"type": "block", "label": "Farm"}}],
            "connectors": [{"from": "lib", "to": "farm", "kind": "flows-to"}]}
        assert catalog.upsert(st, comp)["result"] == "created"
        outer = {"id": "outer", "name": "Outer", "members": [{"id": "a", "ref": "composition:access"}]}
        assert catalog.upsert(st, outer)["result"] == "created"
        loop = catalog.upsert(st, {**comp, "members": comp["members"] + [{"id": "o", "ref": "composition:outer"}]})
        assert "cycle" in loop["error"]
        assert catalog.upsert(st, {"kind": "class", "title": "x", "nodes": [{"id": "1bad"}]})["errors"]
        assert "error" in catalog.get(st, "composition:outer", "ascii")
        mm = catalog.get(st, "composition:outer", "mermaid")["mermaid"]
        assert mm.count('subgraph n_a__frame_access["«composition» Access"]') == 1 and "n_a__farm" in mm
        md = catalog.get(st, "devflow", "markdown")["markdown"]
        assert md.startswith("<!-- nncq uml export: generator=") and "view=devflow" in md
        assert "```mermaid\nclassDiagram" in md and 'click n_d href "http://' in md
        assert "[Device](http://127.0.0.1:" in md and "#/uml/class/sym%3Adev%3A%3ADevice?view=devflow&v=" in md
        catalog.view(st, "class", "Conn")
        by = catalog.list_views(st, "kind", symbol="Device")
        keys = {x["key"] for g in by["groups"].values() for x in g}
        assert "devflow" in keys and "class:Conn" not in keys
        assert catalog.list_views(st, "origin")["groups"]["agent"]
        assert any(e["id"] == "comp:DevLib" for e in catalog.find(st, "DevLib")["elements"])
    finally:
        st.close()


def test_cpp_file_default_view_is_regenerated_after_header_change(repo):
    st = _st(repo)
    try:
        d = catalog.view(st, path="lib/src/Conn.cpp", fmt="json")                 # no kind: the default diagram
        assert d["kind"] == "class" and d["key"] == "class:*|path=lib/src/Conn.cpp"
        assert "Conn" in {n["label"] for n in d["model"]["nodes"]} and d["default_for"]["kind"] == "class"
        rev = d["rev"]
    finally:
        st.close()
    hpp = repo.root / "lib/include/Conn.hpp"
    hpp.write_text(CONN_HPP.replace("void send();", "void send(); void flush();"), encoding="utf-8")
    indexer.update_paths(repo, ["lib/include/Conn.hpp"], log=lambda m: None)
    st = _st(repo)
    try:
        dirty = {k for (k,) in st.q("SELECT v.key FROM view_dirty d JOIN views v ON v.id=d.view_id")}
        assert "class:*|path=lib/src/Conn.cpp" in dirty
        d = catalog.view(st, path="lib/src/Conn.cpp", fmt="json")
        conn = next(n for n in d["model"]["nodes"] if n["label"] == "Conn")
        assert "flush" in {m["name"] for m in conn["members"]} and d["rev"] != rev
        assert "class:*|path=lib/src/Conn.cpp" not in {k for (k,) in st.q(
            "SELECT v.key FROM view_dirty d JOIN views v ON v.id=d.view_id")}
    finally:
        st.close()


def test_staleness_reaches_only_affected_views(repo):
    st = _st(repo)
    try:
        catalog.upsert(st, {"key": "dev", "kind": "class", "title": "dev", "nodes": [{"id": "d", "ref": "sym:dev::Device"}]})
        catalog.upsert(st, {"id": "box", "name": "box", "members": [{"id": "d", "ref": "sym:dev::Device"},
                                                                     {"id": "n", "bespoke": {"label": "mine"}}]})
        catalog.view(st, "class", "Logger")
        catalog.view(st, "package", under="py")
        catalog.get(st, "dev", "ascii")
    finally:
        st.close()
    hpp = repo.root / "lib/include/Device.hpp"
    hpp.write_text(DEV_HPP.replace("    void run();\n", "    void run();\n    // comment only\n"), encoding="utf-8")
    indexer.update_paths(repo, ["lib/include/Device.hpp"], log=lambda m: None)
    st = _st(repo)
    try:
        dirty = {k for (k,) in st.q("SELECT v.key FROM view_dirty d JOIN views v ON v.id=d.view_id")}
        assert {"dev", "composition:box"} <= dirty and "package:*|under=py" not in dirty
        assert "stale" not in catalog.get(st, "dev", "json")          # comment only: fingerprint unchanged
    finally:
        st.close()
    hpp.write_text(DEV_HPP.replace("bool open(const std::string& host) override;",
                                   "bool open(const std::string& host, int port) override;"), encoding="utf-8")
    indexer.update_paths(repo, ["lib/include/Device.hpp"], log=lambda m: None)
    st = _st(repo)
    try:
        g = catalog.get(st, "dev", "json")
        assert g["stale"] is True and g["report"]["staleness"][0]["status"] == "changed"
        assert g["model"]["nodes"][0]["stale"] == "changed"
        assert "port" in json.dumps(g["model"])                         # members re-read from the index
        b = catalog.get(st, "composition:box", "json")
        assert b["report"]["staleness"][0]["members"] == ["d"] and len(b["model"]["nodes"]) == 2
        assert "stale" not in catalog.get(st, "package:*|under=py", "json")
    finally:
        st.close()
    hpp.unlink()
    indexer.update_paths(repo, ["lib/include/Device.hpp"], log=lambda m: None)
    st = _st(repo)
    try:
        b = catalog.get(st, "composition:box", "json")
        assert b["report"]["staleness"][0]["status"] == "missing"
        assert {n["label"] for n in b["model"]["nodes"]} == {"d", "mine"}     # flagged, not dropped
        lg = catalog.get(st, "class:Logger", "json")
        assert "Device" not in {n["label"] for n in lg["model"]["nodes"]}
    finally:
        st.close()


def test_old_index_is_migrated_in_place(tmp_path):
    db = tmp_path / "index.db"
    con = sqlite3.connect(db)
    con.executescript(store_mod.SCHEMA)
    con.execute("INSERT INTO meta VALUES('schema','1')")
    con.commit()
    con.close()
    st = Store(db)
    try:
        cols = {r[1] for r in st.q("PRAGMA table_info(symbols)")}
        assert {"visibility", "mods", "stereo", "start_col"} <= cols and st.meta("schema") == "2"
        assert st.meta("uml_facts") is None and st.q("SELECT COUNT(*) FROM views")[0][0] == 0
    finally:
        st.close()


@pytest.mark.skipif(vcs._exe("git") is None, reason="git not installed")
def test_vcs_git_status_have_rev_revert(tmp_path):
    root = tmp_path / "g"
    root.mkdir()
    git = vcs._exe("git")
    run = lambda *a: subprocess.run([git, *a], cwd=root, capture_output=True, check=True)
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    run("add", "a.txt")
    run("commit", "-qm", "c")
    v = vcs.for_root(root)
    assert v.kind == "git" and v.status("a.txt")["state"] == "clean" and len(v.have_rev("a.txt")) == 40
    (root / "a.txt").write_text("two\n", encoding="utf-8")
    assert v.status("a.txt")["state"] == "modified"
    assert v.revert("a.txt")["ok"] and (root / "a.txt").read_text(encoding="utf-8") == "one\n"
    (root / "b.txt").write_text("x", encoding="utf-8")
    assert v.status("b.txt")["state"] == "untracked" and v.add("b.txt")["status"]["state"] == "added"
    with pytest.raises(ValueError):
        v.status("../outside.txt")
