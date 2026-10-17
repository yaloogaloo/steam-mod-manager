"""Dependency list display: name + Workspace ID on the same item."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from ui.dependency_item_widget import (
    DEPENDENCY_ITEM_HEIGHT_PX,
    DependencyDisplayRow,
    DependencyItem,
    DependencyListHost,
    project_dependency_items,
)
from ui.mod_detail_panel import ModDetailPanel


@pytest.fixture()
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_name_and_workspace_id_same_item() -> None:
    items = project_dependency_items(
        [{"id": 1, "title": "传奇人物包", "mod_id": "9000000000000001"}],
        ["330504052"],
    )
    assert len(items) == 1
    assert items[0].name == "传奇人物包"
    assert items[0].workspace_id == "330504052"
    assert items[0].relation_id == 1
    assert items[0].deploy_status == "not_deployed"


def test_project_uses_mods_deploy_status_by_internal_id() -> None:
    items = project_dependency_items(
        [
            {"id": 1, "title": "传奇人物包", "mod_id": "11"},
            {"id": 2, "title": "传奇角色资源包", "mod_id": "22"},
            {"id": 3, "title": "未部署包", "mod_id": "33"},
        ],
        ["330504052", "330504660", "330504999"],
        deploy_status_by_internal_id={
            "11": "deployed",
            "22": "failed",
            "33": "not_deployed",
        },
    )
    assert [row.deploy_status for row in items] == [
        "deployed",
        "failed",
        "not_deployed",
    ]


def test_dependency_item_shows_status_dot_not_text(qapp: QApplication) -> None:
    deployed = DependencyItem(
        DependencyDisplayRow(name="A", workspace_id="1", deploy_status="deployed")
    )
    failed = DependencyItem(
        DependencyDisplayRow(name="B", workspace_id="2", deploy_status="failed")
    )
    pending = DependencyItem(
        DependencyDisplayRow(name="C", workspace_id="3", deploy_status="not_deployed")
    )
    assert deployed.deploy_status_text() == "已部署"
    assert failed.deploy_status_text() == "部署失败"
    assert pending.deploy_status_text() == "未部署"
    assert deployed.deploy_status_tone() == "deployed"
    assert failed.deploy_status_tone() == "failed"
    assert pending.deploy_status_tone() == "not_deployed"
    assert deployed._status_dot.toolTip() == "已部署"
    assert failed._status_dot.toolTip() == "部署失败"
    assert pending._status_dot.toolTip() == "未部署"
    assert "已部署" not in deployed.name_text()
    assert deployed._status_dot.text() == ""
    assert failed._status_dot.text() == ""
    assert pending._status_dot.text() == ""


def test_dependency_items_have_uniform_fixed_height(qapp: QApplication) -> None:
    host = DependencyListHost()
    host.set_items(
        [
            DependencyDisplayRow(
                name="传奇人物包",
                workspace_id="330504052",
                deploy_status="deployed",
            ),
            DependencyDisplayRow(
                name="B",
                workspace_id="2",
                deploy_status="failed",
            ),
            DependencyDisplayRow(
                name="C",
                workspace_id="",
                deploy_status="not_deployed",
            ),
        ]
    )
    widgets = host.findChildren(DependencyItem)
    assert len(widgets) == 3
    heights = {w.height() for w in widgets}
    assert heights == {DEPENDENCY_ITEM_HEIGHT_PX}
    assert all(w.minimumHeight() == w.maximumHeight() == DEPENDENCY_ITEM_HEIGHT_PX for w in widgets)


def test_remove_button_emits_relation_id(qapp: QApplication) -> None:
    received: list[int] = []
    widget = DependencyItem(
        DependencyDisplayRow(
            name="传奇人物包",
            workspace_id="330504052",
            relation_id=42,
        )
    )
    widget.remove_requested.connect(received.append)
    assert not widget._remove_btn.isHidden()
    widget._remove_btn.click()
    assert received == [42]


def test_multiple_dependencies_are_independent_items() -> None:
    items = project_dependency_items(
        [
            {"id": 1, "title": "传奇人物包"},
            {"id": 2, "title": "传奇角色资源包"},
        ],
        ["330504052", "330504660"],
    )
    assert len(items) == 2
    assert items[0].name == "传奇人物包"
    assert items[0].workspace_id == "330504052"
    assert items[1].name == "传奇角色资源包"
    assert items[1].workspace_id == "330504660"
    names = {row.name for row in items}
    wids = {row.workspace_id for row in items}
    assert names.isdisjoint(wids)


def test_missing_name_falls_back_to_workspace_id() -> None:
    items = project_dependency_items([], ["330504052"])
    assert len(items) == 1
    assert items[0].name == "330504052"
    assert items[0].workspace_id == "330504052"

    empty_title = project_dependency_items(
        [{"id": 3, "title": ""}],
        ["330504660"],
    )
    assert len(empty_title) == 1
    assert empty_title[0].name == "330504660"
    assert empty_title[0].workspace_id == "330504660"


def test_internal_id_is_not_used_as_workspace_id() -> None:
    items = project_dependency_items(
        [{"id": 1, "title": "传奇人物包", "mod_id": "9000000000000001"}],
        [],
    )
    assert len(items) == 1
    assert items[0].name == "传奇人物包"
    assert items[0].workspace_id == ""


def test_dependency_item_widget_shows_name_and_id(qapp: QApplication) -> None:
    row = DependencyDisplayRow(
        name="传奇人物包",
        workspace_id="330504052",
        relation_id=7,
    )
    widget = DependencyItem(row)
    assert widget.name_text() == "传奇人物包"
    assert "Workspace ID: 330504052" == widget.workspace_id_text()
    assert not widget._remove_btn.isHidden()


def test_list_host_refresh_replaces_items(qapp: QApplication) -> None:
    host = DependencyListHost()
    host.set_items(
        project_dependency_items(
            [{"id": 1, "title": "传奇人物包"}],
            ["330504052"],
        )
    )
    first = host.findChildren(DependencyItem)
    assert len(first) == 1
    assert first[0].name_text() == "传奇人物包"
    assert "330504052" in first[0].workspace_id_text()

    host.set_items(
        project_dependency_items(
            [
                {"id": 1, "title": "传奇人物包"},
                {"id": 2, "title": "传奇角色资源包"},
            ],
            ["330504052", "330504660"],
        )
    )
    refreshed = host.findChildren(DependencyItem)
    assert len(refreshed) == 2
    assert [w.name_text() for w in refreshed] == ["传奇人物包", "传奇角色资源包"]
    assert [w.workspace_id_text() for w in refreshed] == [
        "Workspace ID: 330504052",
        "Workspace ID: 330504660",
    ]
    assert host.displayed_rows()[1].workspace_id == "330504660"


def test_panel_refresh_uses_updated_projection(qapp: QApplication) -> None:
    panel = ModDetailPanel()
    panel._resolved = SimpleNamespace(dependencies=["330504052"])
    panel._refresh_dependency_pill(
        {"dependencies": [{"id": 1, "title": "传奇人物包"}]}
    )
    first = panel.dep_list_host.findChildren(DependencyItem)
    assert len(first) == 1
    assert first[0].name_text() == "传奇人物包"
    assert "330504052" in first[0].workspace_id_text()
    assert panel.dep_summary_label.isHidden()

    panel._resolved = SimpleNamespace(dependencies=["330504052", "330504660"])
    panel._refresh_dependency_pill(
        {
            "dependencies": [
                {"id": 1, "title": "传奇人物包"},
                {"id": 2, "title": "传奇角色资源包"},
            ]
        }
    )
    refreshed = panel.dep_list_host.findChildren(DependencyItem)
    assert len(refreshed) == 2
    assert refreshed[0].name_text() == "传奇人物包"
    assert "330504052" in refreshed[0].workspace_id_text()
    assert refreshed[1].name_text() == "传奇角色资源包"
    assert "330504660" in refreshed[1].workspace_id_text()

    panel._resolved = SimpleNamespace(dependencies=[])
    panel._refresh_dependency_pill({"dependencies": []})
    assert panel.dep_list_host.findChildren(DependencyItem) == []
    assert not panel.dep_summary_label.isHidden()
    assert panel.dep_summary_label.text() == "依赖于 —"
    panel.close()
    panel.deleteLater()
