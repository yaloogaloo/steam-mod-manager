"""Static admission tests; never constructs Qt or imports SMM packages."""
import hashlib
from pathlib import Path
import unittest
from ui_validation_d0.flow_harness import review_source, REVIEWED_SHA256
from ui_validation_d0.safety import IsolationError


class FlowAdmissionTests(unittest.TestCase):
    def test_real_reviewed_source(self):
        source = Path(__file__).resolve().parents[3] / "ui" / "flow_layout.py"
        tree = review_source(source.read_bytes(), REVIEWED_SHA256)
        self.assertTrue(any(getattr(node, "name", None) == "FlowLayout" for node in tree.body))

    def test_changed_source_rejected(self):
        with self.assertRaises(IsolationError):
            review_source(b"import services\n", REVIEWED_SHA256)

    def test_self_supplied_hash_is_not_admission(self):
        bad = b"import services\n"
        with self.assertRaises(IsolationError):
            review_source(bad, hashlib.sha256(bad).hexdigest())


if __name__ == "__main__":
    unittest.main()
