"""Foundation tests only: never import UI/core/services or create Qt widgets."""
from pathlib import Path
from contextlib import closing
import json
import os
import sqlite3
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ui_validation_d0.safety import Boundary, FixtureDatabase, IsolationError, launch_checks, require_ui_admission, suspects
from ui_validation_d0.fixtures import build, constants, SCENARIOS, synthetic_png
from ui_validation_d0.async_control import PLANS, validate_controlled_kind
from ui_validation_d0.settings import SettingsFactory
from ui_validation_d0.qt_harness import ImportFence, HarnessGuard, main as harness_main

REPO = Path(__file__).resolve().parents[3]


class InfrastructureTests(unittest.TestCase):
    def setUp(self):
        output = Path(os.environ["SMM_D0_TEST_OUTPUT"]).resolve()
        output.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="unit-", dir=output)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.boundary = Boundary(self.root, REPO)

    def test_repository_root_rejected(self):
        for target in (REPO, REPO / "temp", REPO.parent, Path(REPO.anchor)):
            with self.assertRaises(IsolationError):
                Boundary(target, REPO)

    def test_prefix_sibling_not_allowed(self):
        with self.assertRaises(IsolationError):
            self.boundary.target(self.root.with_name(self.root.name + "-foreign") / "db")

    def test_dotdot_escape_rejected(self):
        with self.assertRaises(IsolationError):
            self.boundary.target(self.root / ".." / "foreign")

    def test_production_data_read_rejected(self):
        with self.assertRaises(IsolationError):
            self.boundary.read(REPO / "data" / "mod_manager.db")
        self.assertEqual(self.boundary.read(REPO / "core" / "db_manager.py"), REPO / "core" / "db_manager.py")

    def test_sqlite_destination_guard(self):
        with self.assertRaises(IsolationError):
            self.boundary.audit("sqlite3.connect", (str(REPO / "data" / "mod_manager.db"),))
        self.boundary.audit("sqlite3.connect", (str(self.root / "fixture.db"),))

    def test_external_capabilities_refused(self):
        for event in ("socket.connect", "socket.getaddrinfo", "subprocess.Popen", "os.system", "os.startfile", "winreg.OpenKey"):
            with self.assertRaises(IsolationError):
                self.boundary.audit(event, ())

    def test_write_and_rename_guards(self):
        with self.assertRaises(IsolationError):
            self.boundary.audit("open", (str(REPO / "config" / "x"), "w", 0))
        with self.assertRaises(IsolationError):
            self.boundary.audit("os.rename", (str(self.root / "x"), str(REPO / "x")))

    def test_audit_hook_in_real_child(self):
        probe = REPO / "tools" / "ui_validation_d0" / "guard_probe.py"
        childroot = self.root / "child"
        result = subprocess.run([sys.executable, "-B", str(probe), str(childroot), str(REPO)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        evidence = json.loads(result.stdout)
        self.assertTrue(all(x["blocked"] for x in evidence["observations"]))
        self.assertFalse(evidence["native_sandbox_proven"])
        self.assertFalse((self.root / "denied-write.txt").exists())
        self.assertFalse((self.root / "denied.db").exists())

    def test_no_ui_even_without_production(self):
        with self.assertRaises(IsolationError):
            require_ui_admission([])

    def test_running_production_refused(self):
        active = suspects([{"Name": "python.exe", "CommandLine": f'python "{REPO}/main.py"'}], REPO)
        self.assertEqual(len(active), 1)
        with self.assertRaises(IsolationError):
            require_ui_admission(active)

    def test_unrelated_process_and_audit_shell(self):
        self.assertEqual(suspects([{"Name": "python.exe", "CommandLine": "other.py"}, {"Name": "powershell.exe", "CommandLine": str(REPO) + "/main.py"}], REPO), [])

    def test_fixture_identity_and_collection_contract(self):
        result = build(self.boundary, 20)
        self.assertEqual(result["count"], 20)
        with closing(sqlite3.connect(result["db"])) as db:
            rows = db.execute("SELECT mod_id,internal_id,workspace_id FROM mods").fetchall()
            for pk, frozen, workspace in rows:
                self.assertEqual(str(uuid.UUID(frozen)), frozen)
                self.assertNotEqual(str(pk), workspace)
                self.assertNotEqual(frozen, workspace)
            self.assertEqual(db.execute("SELECT count(*) FROM collection_mods").fetchone()[0], 5)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("SELECT count(*) FROM mods WHERE app_id=910001").fetchone()[0], 0)
        self.assertEqual(len(json.loads((self.root / "fixture_manifest.json").read_text(encoding="utf-8"))["identities"]), 20)

    def test_no_overwrite_existing_fixture(self):
        build(self.boundary, 1)
        with self.assertRaises(IsolationError):
            build(self.boundary, 1)

    def test_fixture_size_bounds(self):
        for count in (0, 10001):
            with self.assertRaises(IsolationError):
                build(self.boundary, count)

    def test_current_schema_ast_only(self):
        self.assertIn("CREATE TABLE", constants(REPO / "core" / "db_manager.py")["_SCHEMA"])
        self.assertEqual(synthetic_png(2, 2)[:8], b"\x89PNG\r\n\x1a\n")

    def test_settings_target_only(self):
        self.assertTrue(SettingsFactory(self.boundary).path.is_relative_to(self.root))
        # No real Qt/registry settings are instantiated by this test.

    def test_explicit_fixture_database_connection_and_close(self):
        fixture = build(self.boundary, 1)
        capability = object()
        env = {"SMM_TEST_DB": fixture["db"]}
        provider = FixtureDatabase(self.boundary, fixture["db"], env, capability)
        db = provider.connect(capability)
        self.assertEqual(db.execute("SELECT count(*) FROM mods").fetchone()[0], 1)
        db.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")
        env.clear()
        with self.assertRaises(IsolationError):
            provider.connect(capability)

    def test_explicit_database_negative_cases(self):
        path = self.root / "data" / "fixture.db"
        capability = object()
        env = {"SMM_TEST_DB": str(path)}
        provider = FixtureDatabase(self.boundary, path, env, capability)
        with self.assertRaises(IsolationError):
            provider.connect(capability)  # missing path must not create
        self.assertFalse(path.exists())
        env["SMM_TEST_DB"] = str(REPO / "data" / "mod_manager.db")
        with self.assertRaises(IsolationError):
            provider.connect(capability)
        env["SMM_TEST_DB"] = str(path)
        with self.assertRaises(IsolationError):
            provider.connect(object())

    def test_database_symlink_escape(self):
        # Target directory is synthetic, outside this boundary. Never use a
        # production target even for a negative-case symlink.
        link = self.root / "escape"
        try:
            link.symlink_to(self.root.parent, target_is_directory=True)
        except OSError as exc:
            self.skipTest("Windows symlink privilege unavailable: " + str(exc))
        with self.assertRaises(IsolationError):
            self.boundary.target(link / "data" / "fixture.db")

    def test_windows_junction_escape(self):
        if os.name != "nt":
            self.skipTest("Windows junction requires Windows")
        link = self.root / "junction"
        result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(self.root.parent)], capture_output=True)
        if result.returncode:
            self.skipTest("junction creation unavailable: " + result.stderr.decode(errors="replace"))
        try:
            with self.assertRaises(IsolationError):
                self.boundary.target(link / "outside.db")
            with self.assertRaises(IsolationError):
                Boundary(link, REPO)
        finally:
            # Remove ONLY the newly-created reparse entry, never recurse into
            # its target and never touch previous blocked temporary directories.
            os.rmdir(link)
        self.assertTrue(self.root.parent.is_dir())

    def test_relative_unc_and_device_paths_rejected(self):
        for value in ("relative.db", r"\\server\share\fixture.db", r"\\?\D:\fixture.db", str(self.root / "stream:secret")):
            with self.assertRaises(IsolationError):
                self.boundary.target(value)

    def test_case_alias_within_root_and_sibling_escape(self):
        if os.name != "nt":
            self.skipTest("Windows case-insensitive paths")
        self.assertEqual(self.boundary.target(str(self.root).upper() + "\\test.db"), self.root / "test.db")
        with self.assertRaises(IsolationError):
            self.boundary.target(str(self.root).upper() + "-sibling\\test.db")

    def test_all_launch_refusals_reported_and_flags_cannot_waive(self):
        checks = launch_checks(self.boundary, [], {"TEST_MODE": "1", "OS_ISOLATED": "1", "SAFE_UI_LAUNCH": "PASS"})
        by_name = {item["check"]: item for item in checks}
        for name in ("sqlite_path", "qsettings", "os_boundary", "network", "workers", "startup_shutdown"):
            self.assertEqual(by_name[name]["status"], "NOT CLOSED")
        with self.assertRaises(IsolationError):
            require_ui_admission([], boundary=self.boundary, environment={"TEST_MODE": "1"})

    def test_native_worker_descendant_detected(self):
        processes = [
            {"ProcessId": 100, "ParentProcessId": 5, "Name": "python.exe", "CommandLine": str(REPO / "main.py")},
            {"ProcessId": 101, "ParentProcessId": 100, "Name": "helper.exe", "CommandLine": "helper.exe"},
            {"ProcessId": 102, "ParentProcessId": 101, "Name": "helper.exe", "CommandLine": "helper.exe"},
            {"ProcessId": 200, "ParentProcessId": 7, "Name": "helper.exe", "CommandLine": "other.exe"},
        ]
        self.assertEqual({p["ProcessId"] for p in suspects(processes, REPO)}, {100, 101, 102})

    def test_removing_display_blocker_list_cannot_admit_ui(self):
        with patch("ui_validation_d0.safety.NATIVE_BLOCKERS", ()):
            with self.assertRaises(IsolationError):
                require_ui_admission([])
            with self.assertRaises(IsolationError):
                require_ui_admission([], boundary=self.boundary, environment={"TEST_MODE": "1"})

    def test_controlled_task_allowlist_rejects_business_tasks(self):
        for kind in ("game", "metadata", "library"):
            validate_controlled_kind(kind)
        for kind in ("deploy", "sync", "import", "backup", "cas_repair", "unknown"):
            with self.assertRaises(ValueError):
                validate_controlled_kind(kind)

    def test_pure_qt_import_fence(self):
        fence = ImportFence()
        for name in ("main", "ui.mod_card", "core.db_manager", "services.cover_loader",
                     "ui_validation_d0.fixtures", "ui_validation_d0.async_control", "unapproved_package"):
            with self.assertRaises(IsolationError):
                fence.find_spec(name)
        for name in ("json", "PySide6.QtWidgets", "shiboken6", "ui_validation_d0.capture"):
            self.assertIsNone(fence.find_spec(name))

    def test_pure_qt_guard_refuses_production_code_and_database(self):
        guard = HarnessGuard(self.root, REPO)
        for path in (REPO / "ui" / "styles.py", REPO / "core" / "db_manager.py",
                     REPO / "data" / "mod_manager.db"):
            with self.assertRaises(IsolationError):
                guard.read(path)
        with self.assertRaises(IsolationError):
            guard.audit("sqlite3.connect", (str(self.root / "synthetic.db"),))
        self.assertEqual(guard.read(REPO / "tools" / "ui_validation_d0" / "capture.py"), REPO / "tools" / "ui_validation_d0" / "capture.py")

    def test_pure_qt_runner_refuses_site_startup_before_writes(self):
        target = self.root / "must-not-create"
        if sys.flags.isolated and sys.flags.no_site:
            self.skipTest("Parent already isolated; covered by real -I -S child evidence")
        with self.assertRaisesRegex(IsolationError, "-I -S -B"):
            harness_main(["--root", str(target)])
        self.assertFalse(target.exists())

    def test_all_required_scenarios_declared(self):
        self.assertEqual(len(SCENARIOS), 15)
        self.assertEqual(len(PLANS), 8)
        for events in PLANS.values():
            self.assertEqual([x.at_ms for x in events], sorted(x.at_ms for x in events))
        self.assertEqual(PLANS["metadata_a_to_b"][-1].identity, "101")


if __name__ == "__main__":
    unittest.main()
