from core.db_manager import DatabaseManager
from core.paths import data_dir, default_mod_library
from services.library_status import CONTENT_IDENTITY_CONFLICT, LIBRARY_STATUS_CONFLICT
from services.mod_identity_repair import audit_severity_counts
from services.mod_library_integrity_audit import audit_mod_library_integrity
from services.mod_identity_validator import IdentityIssueCode, IdentitySeverity
import json

DatabaseManager.reset_instance()
db = DatabaseManager.instance(data_dir() / "mod_manager.db")
lib = default_mod_library()
report = audit_mod_library_integrity(lib, db=db)
# Explicitly mark multi-folder same-mod as identity_conflict in DB.
for f in report.global_findings:
    if (
        f.severity == IdentitySeverity.DUPLICATE
        and f.code == IdentityIssueCode.DUPLICATE_DIRECTORY_IDENTITY
        and f.mod_id.isdigit()
    ):
        db.update_mod_identity_fields(
            f.mod_id,
            content_status=CONTENT_IDENTITY_CONFLICT,
            library_status=LIBRARY_STATUS_CONFLICT,
            folder_present=True,
        )
report2 = audit_mod_library_integrity(lib, db=db)
counts = audit_severity_counts(report2)
payload = {
    "counts": counts,
    "scanned_folders": report2.scanned_folders,
    "scanned_db_rows": report2.scanned_db_rows,
    "gate": {
        "CRITICAL_ok": counts.get("CRITICAL", 0) == 0,
        "HIGH_ok": counts.get("HIGH", 0) == 0,
    },
}
(data_dir() / "identity_audit_after.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(json.dumps(payload, ensure_ascii=False, indent=2))
DatabaseManager.reset_instance()
