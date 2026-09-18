"""Phase 6: identity semantic grep — internal_id must not be a digit PK."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

PROD = (
    ROOT / "core",
    ROOT / "services",
    ROOT / "ui",
)

# Keyword assignment of a bare digit / quoted digit to internal_id.
_KW_DIGIT = re.compile(
    r"""internal_id\s*=\s*(?:['"]\d+['"]|\d+)"""
)
# f-string / log `internal_id={pk}` is too noisy; flag explicit PK literals.


def _iter_py(roots: tuple[Path, ...]) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        out.extend(p for p in root.rglob("*.py") if p.is_file())
    return out


def test_production_internal_id_keyword_is_not_digit_pk() -> None:
    hits: list[str] = []
    for path in _iter_py(PROD):
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if _KW_DIGIT.search(line):
                rel = path.relative_to(ROOT).as_posix()
                hits.append(f"{rel}:{i}:{line.strip()}")
    assert hits == [], "internal_id=PK leftovers:\n" + "\n".join(hits)


def test_allocate_internal_id_returns_uuid_not_pk() -> None:
    from services.deploy_identity import is_frozen_internal_uuid
    from services.identity_service import allocate_internal_id

    token = allocate_internal_id(None)
    assert is_frozen_internal_uuid(token)
    assert not str(token).isdigit()


def test_deploy_identity_rejects_digit_pk() -> None:
    from services.deploy_identity import DeployIdentityError, resolve_deploy_entity

    class _Db:
        def find_mod_by_internal_id(self, token):  # noqa: ANN001
            raise AssertionError("must not look up digit PK as Frozen UUID")

    with pytest.raises(DeployIdentityError):
        resolve_deploy_entity("296", db=_Db())
    with pytest.raises(DeployIdentityError):
        resolve_deploy_entity("1", db=_Db())


def test_deploy_result_does_not_promote_pk_to_internal_id() -> None:
    from services.deploy_identity import is_frozen_internal_uuid
    from services.deploy_result import normalize_deploy_dict

    out = normalize_deploy_dict(
        {
            "success": True,
            "mod_id": "296",
            "mod_pk": 296,
        }
    )
    assert not is_frozen_internal_uuid(str(out.get("internal_id") or ""))
    assert not str(out.get("internal_id") or "").isdigit()
    assert int(out.get("mod_pk") or 0) == 296


def test_deploy_mod_uuid_ok_pk_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.db_manager import get_db
    from core.game_info import GameInfo
    from services.deploy import ModDeployer
    from services.deploy_identity import (
        DeployIdentityError,
        is_frozen_internal_uuid,
        resolve_deploy_entity,
    )
    from tests.helpers.identity import create_steam_test_mod, prove_managed_folder

    db = get_db()
    db.upsert_game(GameInfo(app_id=99, name="TestGame", folder_name="TestGame"))
    library = tmp_path / "mod"
    mod_dir = library / "TestGame" / "DeployMe"
    mod_dir.mkdir(parents=True)
    (mod_dir / "pak.txt").write_text("data", encoding="utf-8")
    created = create_steam_test_mod(
        db, external_id="8001", title="DeployMe", app_id=99, game_name="TestGame"
    )
    pk = prove_managed_folder(
        db,
        mod_dir,
        handle=created.mod_id,
        title="DeployMe",
        app_id=99,
        game_name="TestGame",
    )
    frozen_id = str(created.internal_id)
    assert is_frozen_internal_uuid(frozen_id)
    assert str(pk).isdigit()
    assert frozen_id != str(pk)
    assert str(db.find_mod_by_internal_id(frozen_id) or "") == str(pk)

    with pytest.raises(DeployIdentityError):
        resolve_deploy_entity(pk, db=db)
    entity = resolve_deploy_entity(frozen_id, db=db)
    assert entity.internal_id == frozen_id
    assert int(entity.mod_pk) == int(pk)

    deployer = ModDeployer(library_root=library, db=db)
    rejected = deployer.deploy_mod(pk)
    assert rejected.get("success") is False
    assert rejected.get("error_code") == "invalid_internal_uuid"
    assert not str(rejected.get("internal_id") or "").isdigit()

    received: list[str] = []
    real = ModDeployer.deploy_mod

    def _wrap(self, internal_id, **kwargs):  # noqa: ANN001
        received.append(str(internal_id))
        return real(self, internal_id, **kwargs)

    monkeypatch.setattr(ModDeployer, "deploy_mod", _wrap)
    deployer.deploy_mod(frozen_id)
    assert received == [frozen_id]
    assert is_frozen_internal_uuid(received[0])
