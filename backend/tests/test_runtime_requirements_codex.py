"""Regression checks for the candidate's declared runtime contract.

The checks are deliberately static except for small standard-library and SQLite
capability probes.  They never import the live project, open its database/cache,
or use the network.
"""

from __future__ import annotations

import ast
import importlib
import json
import pathlib
import sqlite3
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "RUNTIME_REQUIREMENTS.json"


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _source_files(manifest: dict) -> list[pathlib.Path]:
    files: set[pathlib.Path] = set()
    for pattern in manifest["scope"]["source_globs"]:
        files.update(p for p in ROOT.glob(pattern) if p.is_file())
    return sorted(files)


def _top_level_imports(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.add(node.module.split(".", 1)[0])
    out.discard("__future__")
    return out


def _local_module_names(files: list[pathlib.Path]) -> set[str]:
    names = {p.stem for p in files}
    names.update(p.relative_to(ROOT).parts[0] for p in files
                 if len(p.relative_to(ROOT).parts) > 1)
    return names


def _called_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    if isinstance(func, ast.Name):
        return func.id
    return ""


class RuntimeManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = _manifest()
        cls.files = _source_files(cls.manifest)
        cls.python_files = [path for path in cls.files if path.suffix == ".py"]
        cls.trees: dict[pathlib.Path, ast.AST] = {}
        for path in cls.python_files:
            text = path.read_text(encoding="utf-8")
            cls.trees[path] = ast.parse(text, filename=str(path))

    def test_python_version_and_dependency_contract(self) -> None:
        py = self.manifest["python"]
        self.assertEqual("CPython", py["implementation"])
        self.assertEqual([], py["third_party_packages"])
        self.assertIsNone(py["dependency_install_command"])
        version = sys.version_info[:3]
        minimum = tuple(map(int, py["minimum_version"].split(".")))
        maximum = tuple(map(int, py["maximum_version_exclusive"].split(".")))
        self.assertGreaterEqual(version, minimum)
        self.assertLess(version, maximum)

    def test_every_candidate_source_file_compiles(self) -> None:
        self.assertGreater(len(self.trees), 50)
        # Parsing in setUpClass is the assertion; compile catches code-generation
        # errors while avoiding imports and their possible side effects.
        for path in self.python_files:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")

    def test_import_inventory_is_exact_and_stdlib_is_importable(self) -> None:
        observed: set[str] = set()
        for tree in self.trees.values():
            observed.update(_top_level_imports(tree))
        local = _local_module_names(self.python_files)
        observed_stdlib = observed - local
        declared = set(self.manifest["python"]["stdlib_modules_imported"])
        self.assertEqual(declared, observed_stdlib,
                         "runtime manifest is stale or an undeclared package was imported")
        for module in sorted(declared):
            importlib.import_module(module)

        dynamic_prefixes = set(
            self.manifest["python"]["permitted_dynamic_import_prefixes"])
        dynamic_seen: set[str] = set()
        for path, tree in self.trees.items():
            if path.resolve() == pathlib.Path(__file__).resolve():
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                called = _called_name(node)
                if called not in {"importlib.import_module", "__import__"}:
                    continue
                target = node.args[0]
                if isinstance(target, ast.Constant) and isinstance(target.value, str):
                    top = target.value.split(".", 1)[0]
                    self.assertTrue(top in local or top in declared,
                                    f"undeclared dynamic import {target.value!r} in {path}")
                elif isinstance(target, ast.JoinedStr):
                    literal_prefix = "".join(
                        part.value for part in target.values
                        if isinstance(part, ast.Constant) and isinstance(part.value, str))
                    dynamic_seen.add(literal_prefix)
                elif isinstance(target, ast.Name):
                    values: list[str] = []
                    source_names = {target.id}
                    for loop in ast.walk(tree):
                        if isinstance(loop, ast.For) and isinstance(loop.target, ast.Name) \
                                and loop.target.id == target.id and isinstance(loop.iter, ast.Name):
                            source_names.add(loop.iter.id)
                    for assignment in ast.walk(tree):
                        if not isinstance(assignment, (ast.Assign, ast.AnnAssign)):
                            continue
                        targets = (assignment.targets if isinstance(assignment, ast.Assign)
                                   else [assignment.target])
                        if not any(isinstance(t, ast.Name) and t.id in source_names
                                   for t in targets):
                            continue
                        value = assignment.value
                        if isinstance(value, (ast.Tuple, ast.List)):
                            values.extend(x.value for x in value.elts
                                          if isinstance(x, ast.Constant)
                                          and isinstance(x.value, str))
                    self.assertTrue(values,
                                    f"uninspectable dynamic import variable in {path}")
                    for value in values:
                        top = value.split(".", 1)[0]
                        self.assertIn(top, local,
                                      f"undeclared dynamic import {value!r} in {path}")
                        dynamic_seen.add(top + ".")
                else:
                    self.fail(f"uninspectable dynamic import in {path}")
        self.assertEqual(dynamic_prefixes, dynamic_seen)

    def test_subprocess_and_command_boundaries_are_exact(self) -> None:
        subprocess_apis = {"subprocess.run", "subprocess.Popen", "subprocess.call",
                           "subprocess.check_call", "subprocess.check_output"}
        shell_apis = {"os.system", "os.popen", "os.execv", "os.execve",
                      "os.execvp", "os.execvpe"}
        callers: set[str] = set()
        forbidden: list[str] = []
        shell_true: list[str] = []
        for path, tree in self.trees.items():
            rel = path.relative_to(ROOT).as_posix()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                called = _called_name(node)
                if called in subprocess_apis:
                    callers.add(rel)
                    for keyword in node.keywords:
                        if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) \
                                and keyword.value.value is True:
                            shell_true.append(f"{rel}:{node.lineno}")
                if called in shell_apis:
                    forbidden.append(f"{rel}:{node.lineno}:{called}")
        policy = self.manifest["subprocess_policy"]
        self.assertEqual(set(policy["callers"]), callers)
        self.assertEqual([], forbidden)
        self.assertEqual([], shell_true)
        self.assertFalse(policy["shell_execution_permitted"])

        plan = json.loads((ROOT / "config/run_plan.json").read_text(encoding="utf-8"))
        allowed = set(policy["run_plan_program_tokens"])
        programs = {step["argv"][0] for step in plan["steps"]}
        self.assertTrue(programs <= allowed,
                        f"run plan names undeclared external programs: {programs - allowed}")

        pdftoppm_sources = {p.relative_to(ROOT).as_posix() for p in self.files
                            if p.resolve() != pathlib.Path(__file__).resolve()
                            and "pdftoppm" in p.read_text(encoding="utf-8")}
        declared_tool = next(x for x in self.manifest["external_executables"]
                             if x["name"] == "pdftoppm")
        self.assertEqual(set(declared_tool["source_literals"]), pdftoppm_sources)
        self.assertTrue(declared_tool["invoked_by_candidate_code"])
        self.assertTrue(declared_tool["mandatory_for_full_release_acceptance"])

        vision_tool = next(x for x in self.manifest["external_executables"]
                           if x["name"] == "Swift + macOS Vision")
        self.assertTrue(vision_tool["invoked_by_candidate_code"])
        self.assertTrue(vision_tool["mandatory_for_full_release_acceptance"])
        self.assertIn("tools/vision_ocr.swift", vision_tool["source_literals"])
        for relative in vision_tool["source_literals"]:
            self.assertTrue((ROOT / relative).is_file(),
                            f"declared Vision source is absent: {relative}")

    def test_sqlite_runtime_supports_declared_features(self) -> None:
        sqlite_req = self.manifest["sqlite"]
        minimum = tuple(map(int, sqlite_req["minimum_library_version"].split(".")))
        linked = tuple(map(int, sqlite3.sqlite_version.split(".")))
        self.assertGreaterEqual(linked, minimum)
        self.assertFalse(sqlite_req["external_sqlite3_cli_required"])

        con = sqlite3.connect(":memory:")
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript("""
            CREATE TABLE parent(id INTEGER PRIMARY KEY);
            CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id INTEGER
                               REFERENCES parent(id));
            INSERT INTO parent VALUES(1);
            INSERT INTO child VALUES(1, 1);
            CREATE TABLE upsert_probe(k TEXT PRIMARY KEY, v INTEGER);
            INSERT INTO upsert_probe VALUES('x', 1);
            INSERT INTO upsert_probe VALUES('x', 2)
              ON CONFLICT(k) DO UPDATE SET v=excluded.v;
        """)
        self.assertEqual(2, con.execute(
            "SELECT v FROM upsert_probe WHERE k='x'").fetchone()[0])
        with self.assertRaises(sqlite3.IntegrityError):
            con.execute("INSERT INTO child VALUES(2, 999)")
        con.close()

        with tempfile.TemporaryDirectory(prefix="ferc-runtime-") as td:
            db = pathlib.Path(td) / "wal.sqlite"
            disk = sqlite3.connect(db)
            mode = disk.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            self.assertEqual("wal", mode.lower())
            disk.execute("BEGIN IMMEDIATE")
            disk.execute("CREATE TABLE committed(x INTEGER)")
            disk.commit()
            disk.close()

    def test_image_runtime_is_mandatory_and_unrelated_clis_remain_optional(self) -> None:
        tools = {item["name"]: item for item in self.manifest["external_executables"]}
        self.assertTrue(tools["pdftoppm"]["mandatory"])
        self.assertTrue(tools["Swift + macOS Vision"]["mandatory"])
        self.assertFalse(tools["zip/unzip CLI"]["mandatory"])
        self.assertFalse(tools["sqlite3 CLI"]["mandatory"])
        capability = next(c for c in self.manifest["capabilities"]
                          if c["name"] == "fresh page rendering or OCR")
        self.assertEqual("supported but review-gated", capability["status"])
        self.assertIn("macOS Vision", " ".join(capability["requirements"]))


if __name__ == "__main__":
    unittest.main()
