"""Presentation inputs, independent of database entities and persistence."""
from dataclasses import dataclass


@dataclass(frozen=True)
class GameContext:
    key: str
    name: str
    app_id: str


@dataclass(frozen=True)
class Workspace:
    key: str
    title: str
    tabs: tuple[str, ...]
    help_text: str
    action_label: str


WORKSPACES = (
    Workspace("library", "资产库", ("全部资产", "收藏", "合集管理"),
              "检索与整理资产；合集不是部署集合。", "添加 Mod"),
    Workspace("deploy", "部署工作台", ("当前部署", "加载顺序", "部署快照"),
              "顺序仅适用于支持 backend 的已部署集合。快照不保存顺序，也不支持 Apply。", "保存成员快照"),
    Workspace("acquire", "获取中心", ("新建任务", "任务记录"),
              "按来源校验输入，再执行获取任务。不同来源能力分别保留。", "新建任务"),
    Workspace("health", "健康与维护", ("待处理问题", "诊断能力"),
              "检测结果与用户标记分开呈现，未开放自动修复。", "检查内容状态"),
    Workspace("settings", "设置与游戏配置", ("游戏配置", "库与资源", "网络与来源", "高级设置"),
              "游戏配置与全局偏好分开；配置变更不自动执行部署。", "新建游戏配置"),
)
