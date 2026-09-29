"""Common order-backend registry dispatch."""

from __future__ import annotations

import inspect
from pathlib import Path

from services.order_backend import (
    Bg3OrderBackend,
    ParadoxOrderBackend,
    Wh3OrderBackend,
    get_order_backend,
)
from services.paradox_activation import CK3_APP_ID, STELLARIS_APP_ID
from services.wh3_activation import WH3_APP_ID

ROOT = Path(__file__).resolve().parents[1]
BG3 = 1086940


def test_registry_bg3() -> None:
    backend = get_order_backend(BG3)
    assert backend is not None
    assert type(backend) is Bg3OrderBackend
    assert type(get_order_backend(0, "博德之门Ⅲ")) is Bg3OrderBackend


def test_registry_ck3_and_stellaris_reuse_paradox() -> None:
    ck3 = get_order_backend(CK3_APP_ID)
    stellaris = get_order_backend(STELLARIS_APP_ID)
    assert type(ck3) is ParadoxOrderBackend
    assert type(stellaris) is ParadoxOrderBackend
    assert ck3.app_id == CK3_APP_ID
    assert stellaris.app_id == STELLARIS_APP_ID


def test_wh3_backend_tokens_are_frozen_internal_id() -> None:
    backend = Wh3OrderBackend()

    class _Obj:
        internal_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"
        mod_id = "12"
        id = "12"
        mod_pk = "12"
        deployed = True

    obj = _Obj()
    assert backend.sort_token(obj) == obj.internal_id
    assert backend.bind_token(obj) == obj.internal_id
    drop_src = inspect.getsource(Wh3OrderBackend.apply_card_drop)
    assert "dal_mod_pk" not in drop_src
    move_src = inspect.getsource(Wh3OrderBackend.apply_order_move)
    assert "apply_order_move" in move_src
    assert "del token" not in move_src
    sort_src = inspect.getsource(Wh3OrderBackend.sort_token)
    assert "internal_id" in sort_src
    assert "mod_id" not in sort_src
    card_src = (ROOT / "ui" / "mod_card.py").read_text(encoding="utf-8")
    assert "application/x-smm-load-order-token" in card_src
    assert "self._entity_internal_id()" in card_src


def test_registry_unsupported() -> None:
    assert get_order_backend(1623730) is None
    assert get_order_backend(0, "Palworld") is None


def test_ui_has_no_third_bg3_dispatch_branch() -> None:
    view_src = (ROOT / "ui" / "library_view.py").read_text(encoding="utf-8")
    card_src = (ROOT / "ui" / "mod_card.py").read_text(encoding="utf-8")
    from ui.library_view import ModLibraryView

    load_src = inspect.getsource(ModLibraryView._is_load_order_sort_game)
    backend_src = inspect.getsource(ModLibraryView._order_backend)
    move_src = inspect.getsource(ModLibraryView._on_wh3_sort_move)
    drop_src = inspect.getsource(ModLibraryView._on_wh3_sort_drop)
    assert "get_order_backend" in backend_src
    assert "_order_backend" in load_src
    assert "1086940" not in view_src
    assert "1086940" not in card_src
    assert "modsettings.lsx" not in view_src
    assert "modsettings.lsx" not in card_src
    assert "meta.lsx" not in view_src
    assert "meta.lsx" not in card_src
    assert "Baldur" not in view_src
    assert "Baldur" not in card_src
    assert "GustavX" not in view_src
    assert "LSPK" not in view_src
    assert "if not self._is_paradox_current_game" not in move_src
    assert "apply_order_move" in move_src
    assert "apply_card_drop" in drop_src
    assert "is_bg3" not in view_src
    assert "application/x-smm-bg3" not in card_src
    assert "application/x-smm-load-order-token" in card_src
