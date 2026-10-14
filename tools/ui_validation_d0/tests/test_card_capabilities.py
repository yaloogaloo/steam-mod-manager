import gc
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from ui_validation_d0.card_capabilities import (Resources, ControlledCoverProvider,
    SyntheticDiagnosticSink, synthetic_dto, card_preflight)
from ui_validation_d0.import_boundary import inspect_module
from ui_validation_d0.safety import Boundary, IsolationError


class CardCapabilityTests(unittest.TestCase):
    def setUp(self):
        output = Path(os.environ["SMM_D0_TEST_OUTPUT"])
        output.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(prefix="d05-", dir=output)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = Path(__file__).resolve().parents[3]
        self.resource = Resources(Boundary(self.root, self.repo))
        self.provider = ControlledCoverProvider(self.resource)
        self.received = []
        (self.root / "synthetic.bin").write_bytes(b"synthetic-image-payload-not-decoded")
        (self.root / "settings").mkdir()
        (self.root / "settings/debug.json").write_text('{"ui_trace": true}', encoding="utf-8")

    def request(self, token="A", callback=None):
        return self.provider.request(token, self.root / "mods/A", cover_ref="synthetic.bin",
                                     width=180, height=112, on_ready=callback or (lambda *x: self.received.append(x)))

    def test_resource_positive_and_escape_negatives(self):
        self.assertEqual(self.resource.read("synthetic.bin"), b"synthetic-image-payload-not-decoded")
        self.assertEqual(self.resource.read(self.root / "synthetic.bin"), b"synthetic-image-payload-not-decoded")
        for value in ["../escape", self.repo / "data/mod_manager.db", r"\\server\share\x", r"\\?\C:\x", str(self.root / "synthetic.bin") + ":stream"]:
            with self.subTest(value=str(value)), self.assertRaises(IsolationError):
                self.resource.read(value)

    def test_missing_and_read_error_fail_closed(self):
        with self.assertRaises(IsolationError): self.resource.read("missing")
        with patch.object(Path, "read_bytes", side_effect=OSError("synthetic denied")):
            with self.assertRaises(IsolationError): self.resource.read("synthetic.bin")

    def test_symlink_escape(self):
        link = self.root / "escape-link"
        try: link.symlink_to(self.root.parent, target_is_directory=True)
        except OSError as exc: self.skipTest("Actual Windows symlink unavailable: " + repr(exc))
        with self.assertRaises(IsolationError): self.resource.path(link / "new")

    def test_delayed_a_then_b_and_cancel(self):
        a=self.request("A"); self.provider.cancel("A"); b=self.request("B")
        self.assertFalse(self.provider.complete("A",a))
        self.assertTrue(self.provider.complete("B",b))
        self.assertEqual([x[0] for x in self.received],["B"])

    def test_duplicate_same_token(self):
        first=self.request();second=self.request()
        self.assertFalse(self.provider.complete("A",first))
        self.assertTrue(self.provider.complete("A",second))
        self.assertFalse(self.provider.complete("A",second))

    def test_release_and_close_with_late_result(self):
        a=self.request();self.provider.release_path(self.root / "mods/A")
        self.assertFalse(self.provider.complete("A",a))
        b=self.request("B");self.provider.close()
        self.assertFalse(self.provider.complete("B",b))
        self.assertFalse(self.provider.pending)
        with self.assertRaises(IsolationError): self.request()

    def test_dead_receiver_and_callback_error(self):
        class Receiver:
            def ready(self,*args): raise AssertionError("should not run dead receiver")
        obj=Receiver();a=self.request(callback=obj.ready);del obj;gc.collect()
        self.assertFalse(self.provider.complete("A",a))
        def failure(*args): raise ValueError("synthetic callback failure")
        b=self.request("B",failure)
        with self.assertRaisesRegex(ValueError,"synthetic callback"):self.provider.complete("B",b)
        self.assertFalse(self.provider.pending)

    def test_empty_cover_and_invalid_request(self):
        a=self.provider.request("A",self.root/"mods/A",width=180,height=112,on_ready=lambda *x:self.received.append(x))
        self.provider.complete("A",a);self.assertIsNone(self.received[0][1])
        with self.assertRaises(IsolationError):self.provider.request("",self.root,width=0,height=0,on_ready=lambda *x:None)

    def test_diagnostic_cache_environment_missing_and_tooltip(self):
        sink=SyntheticDiagnosticSink(self.resource)
        with patch.dict(os.environ,{},clear=True):sink.popup("synthetic tooltip")
        self.assertEqual(sink.records[0]["kind"],"popup")
        self.assertTrue(sink.log_path.is_file())
        sink.config_path.unlink();sink.popup("cached synthetic")
        missing=SyntheticDiagnosticSink(self.resource)
        with self.assertRaises(IsolationError):missing.settings()

    def test_exception_and_sink_failure_preserve_original(self):
        sink=SyntheticDiagnosticSink(self.resource)
        @sink.traced
        def destroyed_receiver():raise ValueError("synthetic destroyed receiver")
        with self.assertRaisesRegex(ValueError,"destroyed receiver"):destroyed_receiver()
        self.assertEqual(sink.records[0]["kind"],"exception")
        with patch.object(Path,"open",side_effect=OSError("synthetic sink denied")):
            with self.assertRaisesRegex(ValueError,"destroyed receiver"):destroyed_receiver()
        self.assertEqual(sink.records[-1]["kind"],"sink_failure")

    def test_diagnostic_foreign_paths_refused(self):
        with self.assertRaises(IsolationError):SyntheticDiagnosticSink(self.resource,config=self.repo/"config/debug.json")
        with self.assertRaises(IsolationError):SyntheticDiagnosticSink(self.resource,log=self.repo/"data/import_crash_trace.log")

    def test_ast_dto_identity_and_no_business_import(self):
        import sys,uuid
        dto=synthetic_dto(self.repo,self.root)
        self.assertEqual(str(uuid.UUID(dto.id)),dto.internal_id)
        self.assertNotEqual(dto.workspace_id,dto.id)
        self.assertEqual(dto.mod_id,"101")
        self.assertEqual(dto.description,"")
        self.assertFalse(any(n.split(".")[0] in {"ui","core","services"} for n in sys.modules))

    def test_preflight_cannot_waive_production_process(self):
        self.assertFalse(card_preflight(production_running=True,imports_proven=True,
            diagnostics_connected=True,covers_connected=True,native_io_confined=True)["admitted"])
        self.assertFalse(card_preflight(production_running=False)["admitted"])
        self.assertFalse(card_preflight(production_running=False,imports_proven=True,
            diagnostics_connected=True,covers_connected=True,native_io_confined=True)["admitted"])

    def test_inspector_distinguishes_decorator_body_condition(self):
        path=self.root/"example.py"
        path.write_text("@decorate()\ndef fn():\n    import delayed\nif condition:\n    import optional\nimport eager\n",encoding="utf-8")
        entries=inspect_module(path)["entries"]
        phases={e["source"]:e["phase"] for e in entries}
        self.assertEqual(phases["decorate()"],"DEFINITION_OR_MODULE_INITIALIZATION")
        self.assertEqual(phases["import delayed"],"DELAYED_BODY")
        self.assertEqual(phases["import optional"],"CONDITIONAL_INITIALIZATION")


if __name__=="__main__":unittest.main()
