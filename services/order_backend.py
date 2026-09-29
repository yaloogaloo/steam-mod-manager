"""Thin load-order backend registry. Not a plugin system.

UI asks ``get_order_backend(app_id, game_name)`` and then talks only in
tokens / move operations. Game files stay inside the backend modules.
"""

from __future__ import annotations

from typing import Any, Protocol

from services.bg3_activation import is_bg3_order_app
from services.paradox_activation import (
    ORDER_MOVE_BOTTOM,
    ORDER_MOVE_DOWN,
    ORDER_MOVE_TOP,
    ORDER_MOVE_UP,
    is_paradox_activation_app,
    is_stellaris_activation_app,
    paradox_game_for_app_id,
    paradox_game_for_name,
)
from services.wh3_activation import (
    canon_internal_id,
    display_numbers,
    is_wh3_activation_app,
)

__all__ = [
    "OrderBackend",
    "get_order_backend",
    "ORDER_MOVE_TOP",
    "ORDER_MOVE_UP",
    "ORDER_MOVE_DOWN",
    "ORDER_MOVE_BOTTOM",
]


def _attr(obj: object, *names: str) -> str:
    for name in names:
        text = str(getattr(obj, name, "") or "").strip()
        if text:
            return text
    return ""


class OrderBackend(Protocol):
    def sort_mode_tooltip(self) -> str: ...

    def search_in_sort_mode(self) -> bool: ...

    def sort_token(self, obj: object) -> str: ...

    def bind_token(self, obj: object) -> str: ...

    def is_sortable_member(self, index: object, **kwargs: Any) -> bool: ...

    def get_sortable_mods(self, **kwargs: Any) -> list[str]: ...

    def get_current_order(self, **kwargs: Any) -> list[str]: ...

    def persist_order(self, tokens: list[str] | None = None, **kwargs: Any) -> list[str]: ...

    def apply_order_move(self, token: str, action: str, **kwargs: Any) -> list[str]: ...

    def apply_card_drop(
        self, source_id: str, target_id: str, **kwargs: Any
    ) -> list[str]: ...

    def sync_projection(self, **kwargs: Any) -> None: ...

    def sort_entries(
        self, entries: list, **kwargs: Any
    ) -> tuple[list, dict[str, int]]: ...


class ParadoxOrderBackend:
    def __init__(self, app_id: int) -> None:
        self.app_id = int(app_id)

    def sort_mode_tooltip(self) -> str:
        if is_stellaris_activation_app(self.app_id):
            return "进入 Stellaris 已启用 Mod 的 Load Order 排序工作区"
        return "进入已部署 Mod 的 Load Order 排序工作区"

    def search_in_sort_mode(self) -> bool:
        return True

    def sort_token(self, obj: object) -> str:
        return canon_internal_id(_attr(obj, "internal_id", "mod_id", "id"))

    def bind_token(self, obj: object) -> str:
        return self.sort_token(obj)

    def is_sortable_member(self, index: object, **kwargs: Any) -> bool:
        if not bool(getattr(index, "deployed", False)):
            return False
        token = self.sort_token(index)
        if not token or self.app_id <= 0:
            return False
        from core.db_manager import get_db
        from services.paradox_activation import (
            ParadoxModRef,
            map_to_launcher_id,
            resolve_paradox_user_dir,
        )

        db = kwargs.get("db") or get_db()
        try:
            user_dir = resolve_paradox_user_dir(db, app_id=self.app_id)
            ref = ParadoxModRef(
                token=token,
                workspace_id=_attr(index, "workspace_id"),
                enabled=True,
                deployed=True,
                last_known_path="",
                platform=_attr(index, "platform"),
                external_id=_attr(index, "external_id"),
            )
            return bool(map_to_launcher_id(ref, user_dir=user_dir).available)
        except Exception:
            return False

    def get_sortable_mods(self, **kwargs: Any) -> list[str]:
        from services.paradox_activation import list_installed_paradox_mods

        return [
            m.token
            for m in list_installed_paradox_mods(kwargs.get("db"), app_id=self.app_id)
        ]

    def get_current_order(self, **kwargs: Any) -> list[str]:
        from services.paradox_activation import resolved_load_order

        return resolved_load_order(kwargs.get("db"), app_id=self.app_id)

    def persist_order(self, tokens: list[str] | None = None, **kwargs: Any) -> list[str]:
        from services.paradox_activation import load_saved_order, persist_load_order

        current = tokens if tokens is not None else load_saved_order(app_id=self.app_id)
        return persist_load_order(current, kwargs.get("db"), app_id=self.app_id)

    def apply_order_move(self, token: str, action: str, **kwargs: Any) -> list[str]:
        from services.paradox_activation import apply_order_move

        return apply_order_move(token, action, kwargs.get("db"), app_id=self.app_id)

    def apply_card_drop(self, source_id: str, target_id: str, **kwargs: Any) -> list[str]:
        from services.paradox_activation import apply_card_drop

        return apply_card_drop(
            source_id, target_id, kwargs.get("db"), app_id=self.app_id
        )

    def sync_projection(self, **kwargs: Any) -> object:
        from services.paradox_activation import sync_paradox_launcher

        return sync_paradox_launcher(kwargs.get("db"), app_id=self.app_id)

    def sort_entries(self, entries: list, **kwargs: Any) -> tuple[list, dict[str, int]]:
        order = self.persist_order(None, **kwargs)
        rank = {mid: i for i, mid in enumerate(order)}
        out = list(entries)
        out.sort(key=lambda pair: rank.get(self.sort_token(pair[0]), 10**9))
        visible = [self.sort_token(pair[0]) for pair in out]
        return out, display_numbers(visible)


class Wh3OrderBackend:
    def sort_mode_tooltip(self) -> str:
        return "进入 WH3 已部署 Mod 的 Load Order 排序工作区"

    def search_in_sort_mode(self) -> bool:
        return False

    def sort_token(self, obj: object) -> str:
        from services.deploy_identity import is_frozen_internal_uuid

        token = str(_attr(obj, "internal_id", "id") or "").strip()
        return token if is_frozen_internal_uuid(token) else ""

    def bind_token(self, obj: object) -> str:
        return self.sort_token(obj)

    def is_sortable_member(self, index: object, **kwargs: Any) -> bool:
        if not bool(getattr(index, "deployed", False)):
            return False
        return bool(self.sort_token(index))

    def get_sortable_mods(self, **kwargs: Any) -> list[str]:
        from services.wh3_activation import list_deployed_wh3_mods

        return [
            m.internal_id
            for m in list_deployed_wh3_mods(
                kwargs.get("db"), library_root=kwargs.get("library_root")
            )
        ]

    def get_current_order(self, **kwargs: Any) -> list[str]:
        from services.wh3_activation import resolved_load_order

        return resolved_load_order(
            kwargs.get("db"), library_root=kwargs.get("library_root")
        )

    def persist_order(self, tokens: list[str] | None = None, **kwargs: Any) -> list[str]:
        from services.wh3_activation import load_saved_order, persist_load_order

        current = tokens if tokens is not None else load_saved_order(kwargs.get("db"))
        return persist_load_order(
            current, kwargs.get("db"), library_root=kwargs.get("library_root")
        )

    def apply_order_move(self, token: str, action: str, **kwargs: Any) -> list[str]:
        from services.wh3_activation import apply_order_move

        return apply_order_move(
            token,
            action,
            kwargs.get("db"),
            library_root=kwargs.get("library_root"),
        )

    def apply_card_drop(self, source_id: str, target_id: str, **kwargs: Any) -> list[str]:
        from services.wh3_activation import apply_card_drop

        return apply_card_drop(
            source_id,
            target_id,
            kwargs.get("db"),
            library_root=kwargs.get("library_root"),
        )

    def sync_projection(self, **kwargs: Any) -> None:
        from services.wh3_activation import sync_used_mods_txt

        sync_used_mods_txt(
            kwargs.get("db"), library_root=kwargs.get("library_root")
        )

    def sort_entries(self, entries: list, **kwargs: Any) -> tuple[list, dict[str, int]]:
        order = self.persist_order(None, **kwargs)
        numbers = display_numbers(order)
        rank = {mid: i for i, mid in enumerate(order)}
        out = list(entries)
        out.sort(key=lambda pair: rank.get(self.sort_token(pair[0]), 10**9))
        return out, numbers


class Bg3OrderBackend:
    def __init__(self) -> None:
        self._sortable_cache: list[str] | None = None

    def sort_mode_tooltip(self) -> str:
        return "进入已部署 Mod 的 Load Order 排序工作区"

    def search_in_sort_mode(self) -> bool:
        return True

    def sort_token(self, obj: object) -> str:
        from services.bg3_activation import sort_token as bg3_sort_token

        return bg3_sort_token(obj)

    def bind_token(self, obj: object) -> str:
        return self.sort_token(obj)

    def get_sortable_mods(self, **kwargs: Any) -> list[str]:
        if self._sortable_cache is None:
            from services.bg3_activation import get_sortable_mods

            self._sortable_cache = get_sortable_mods(
                kwargs.get("db"), library_root=kwargs.get("library_root")
            )
        return list(self._sortable_cache)

    def is_sortable_member(self, index: object, **kwargs: Any) -> bool:
        token = self.sort_token(index)
        if not token:
            return False
        return token in set(self.get_sortable_mods(**kwargs))

    def get_current_order(self, **kwargs: Any) -> list[str]:
        from services.bg3_activation import resolved_load_order

        return resolved_load_order(
            kwargs.get("db"), library_root=kwargs.get("library_root")
        )

    def persist_order(self, tokens: list[str] | None = None, **kwargs: Any) -> list[str]:
        from services.bg3_activation import persist_load_order

        return persist_load_order(
            tokens, kwargs.get("db"), library_root=kwargs.get("library_root")
        )

    def apply_order_move(self, token: str, action: str, **kwargs: Any) -> list[str]:
        from services.bg3_activation import apply_order_move

        return apply_order_move(
            token, action, kwargs.get("db"), library_root=kwargs.get("library_root")
        )

    def apply_card_drop(self, source_id: str, target_id: str, **kwargs: Any) -> list[str]:
        from services.bg3_activation import apply_card_drop

        return apply_card_drop(
            source_id,
            target_id,
            kwargs.get("db"),
            library_root=kwargs.get("library_root"),
        )

    def sync_projection(self, **kwargs: Any) -> None:
        from services.bg3_activation import sync_projection

        sync_projection(kwargs.get("db"), library_root=kwargs.get("library_root"))

    def sort_entries(self, entries: list, **kwargs: Any) -> tuple[list, dict[str, int]]:
        order = self.persist_order(None, **kwargs)
        rank = {mid: i for i, mid in enumerate(order)}
        out = list(entries)
        out.sort(key=lambda pair: rank.get(self.sort_token(pair[0]), 10**9))
        visible = [self.sort_token(pair[0]) for pair in out]
        return out, display_numbers(visible)


def get_order_backend(
    app_id: int | str = 0,
    game_name: str = "",
) -> OrderBackend | None:
    name = str(game_name or "").strip()
    if is_paradox_activation_app(app_id, name):
        game = paradox_game_for_app_id(app_id) or paradox_game_for_name(name)
        aid = int(game.app_id) if game is not None else int(app_id or 0)
        return ParadoxOrderBackend(aid)
    if is_wh3_activation_app(app_id, name):
        return Wh3OrderBackend()
    if is_bg3_order_app(app_id, name):
        return Bg3OrderBackend()
    return None
