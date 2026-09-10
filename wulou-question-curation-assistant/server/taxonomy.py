"""目录读取和结构校验。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class TaxonomyError(ValueError):
    """目录结构无法安全用于分类。"""


@dataclass(frozen=True)
class Target:
    topic_id: str
    topic_title: str
    topic_order: int
    level2_id: str
    level2_title: str
    level3_id: str
    level3_title: str
    level3_knowledge_point_id: str | None
    level4_id: str | None = None
    level4_title: str | None = None
    level4_knowledge_point_id: str | None = None
    include_keywords: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()


class Taxonomy:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        self.version = str(raw.get("taxonomy_version", ""))
        if not self.version:
            raise TaxonomyError("taxonomy_version 不能为空")
        self._targets = self._build_targets(raw)

    @classmethod
    def from_file(cls, path: Path) -> "Taxonomy":
        with path.open("r", encoding="utf-8") as file:
            raw = yaml.safe_load(file) or {}
        if not isinstance(raw, dict):
            raise TaxonomyError("目录文件根节点必须是对象")
        return cls(raw)

    def _build_targets(self, raw: dict[str, Any]) -> list[Target]:
        targets: list[Target] = []
        node_ids: set[str] = set()
        for topic in raw.get("topics", []):
            self._require_node(topic, "专题")
            self._assert_new_id(topic["id"], node_ids)
            for level2 in topic.get("level2", []):
                self._require_node(level2, "二级目录")
                self._assert_new_id(level2["id"], node_ids)
                for level3 in level2.get("level3", []):
                    self._require_node(level3, "三级目录")
                    self._assert_new_id(level3["id"], node_ids)
                    base = dict(
                        topic_id=str(topic["id"]), topic_title=str(topic["title"]), topic_order=int(topic.get("order", 0)),
                        level2_id=str(level2["id"]), level2_title=str(level2["title"]),
                        level3_id=str(level3["id"]), level3_title=str(level3["title"]),
                        level3_knowledge_point_id=self._optional_text(level3.get("knowledge_point_id")),
                    )
                    level4_items = level3.get("level4", [])
                    if level4_items:
                        for level4 in level4_items:
                            self._require_node(level4, "四级目录")
                            self._assert_new_id(level4["id"], node_ids)
                            targets.append(Target(
                                **base,
                                level4_id=str(level4["id"]), level4_title=str(level4["title"]),
                                level4_knowledge_point_id=self._optional_text(level4.get("knowledge_point_id")),
                                include_keywords=tuple(map(str, level4.get("include_keywords", level3.get("include_keywords", [])))),
                                exclude_keywords=tuple(map(str, level4.get("exclude_keywords", level3.get("exclude_keywords", [])))),
                            ))
                    else:
                        targets.append(Target(
                            **base,
                            include_keywords=tuple(map(str, level3.get("include_keywords", []))),
                            exclude_keywords=tuple(map(str, level3.get("exclude_keywords", []))),
                        ))
        if not targets:
            raise TaxonomyError("目录中没有可分类的三级或四级目录")
        return targets

    @staticmethod
    def _require_node(node: Any, label: str) -> None:
        if not isinstance(node, dict) or not node.get("id") or not node.get("title"):
            raise TaxonomyError(f"{label}必须包含 id 和 title")

    @staticmethod
    def _assert_new_id(node_id: Any, node_ids: set[str]) -> None:
        text = str(node_id)
        if text in node_ids:
            raise TaxonomyError(f"目录 ID 重复：{text}")
        node_ids.add(text)

    @staticmethod
    def _optional_text(value: Any) -> str | None:
        return str(value) if value not in (None, "") else None

    def candidates(self, topic_id: str | None, level2_id: str | None) -> list[Target]:
        result = self._targets
        if topic_id:
            result = [target for target in result if target.topic_id == topic_id]
        if level2_id:
            result = [target for target in result if target.level2_id == level2_id]
        return result

    def all_targets(self) -> list[Target]:
        """返回全部可落盘目录；调用方不得修改内部列表。"""
        return list(self._targets)

    def topic_catalog(self) -> list[dict[str, Any]]:
        """生成全局专题路由所需的紧凑目录，避免先按网页位置裁剪候选。"""
        catalog: list[dict[str, Any]] = []
        for topic in self.raw.get("topics", []):
            level2_rows = []
            for level2 in topic.get("level2", []):
                level2_rows.append({
                    "id": str(level2["id"]),
                    "title": str(level2["title"]),
                    "level3_titles": [str(item["title"]) for item in level2.get("level3", [])],
                })
            catalog.append({
                "id": str(topic["id"]),
                "title": str(topic["title"]),
                "order": int(topic.get("order", 0)),
                "level2": level2_rows,
            })
        return catalog

    def classification_catalog(self) -> list[dict[str, Any]]:
        """生成模型分类所需的分层目录，避免为每个末级节点重复专题和父目录字段。"""
        catalog: list[dict[str, Any]] = []
        for topic in self.raw.get("topics", []):
            topic_row = {
                "id": str(topic["id"]),
                "title": str(topic["title"]),
                "order": int(topic.get("order", 0)),
                "level2": [],
            }
            for level2 in topic.get("level2", []):
                level2_row = {"id": str(level2["id"]), "title": str(level2["title"]), "level3": []}
                for level3 in level2.get("level3", []):
                    level3_row: dict[str, Any] = {
                        "id": str(level3["id"]),
                        "title": str(level3["title"]),
                    }
                    if level3.get("knowledge_point_id"):
                        level3_row["knowledge_point_id"] = str(level3["knowledge_point_id"])
                    level4_rows = []
                    for level4 in level3.get("level4", []):
                        level4_row: dict[str, Any] = {
                            "id": str(level4["id"]),
                            "title": str(level4["title"]),
                        }
                        if level4.get("knowledge_point_id"):
                            level4_row["knowledge_point_id"] = str(level4["knowledge_point_id"])
                        include_keywords = level4.get("include_keywords", level3.get("include_keywords", []))
                        exclude_keywords = level4.get("exclude_keywords", level3.get("exclude_keywords", []))
                        if include_keywords:
                            level4_row["include_keywords"] = list(map(str, include_keywords))
                        if exclude_keywords:
                            level4_row["exclude_keywords"] = list(map(str, exclude_keywords))
                        level4_rows.append(level4_row)
                    if level4_rows:
                        level3_row["level4"] = level4_rows
                    else:
                        if level3.get("include_keywords"):
                            level3_row["include_keywords"] = list(map(str, level3["include_keywords"]))
                        if level3.get("exclude_keywords"):
                            level3_row["exclude_keywords"] = list(map(str, level3["exclude_keywords"]))
                    level2_row["level3"].append(level3_row)
                topic_row["level2"].append(level2_row)
            catalog.append(topic_row)
        return catalog

    def classification_catalog_for_topic(self, topic_id: str) -> list[dict[str, Any]]:
        """返回单个专题的完整可分类目录，供第二阶段分类避免重复发送全量目录。"""
        return [item for item in self.classification_catalog() if item["id"] == topic_id]

    def topic(self, topic_id: str | None) -> dict[str, Any] | None:
        for topic in self.topic_catalog():
            if topic["id"] == topic_id:
                return topic
        return None

    def minimum_topic_order_containing(self, text: str) -> int | None:
        orders = [item["order"] for item in self.topic_catalog() if text in item["title"]]
        return min(orders) if orders else None

    def target(self, level3_id: str, level4_id: str | None, topic_id: str | None, level2_id: str | None) -> Target | None:
        for candidate in self.candidates(topic_id, level2_id):
            if candidate.level3_id == level3_id and candidate.level4_id == level4_id:
                return candidate
        return None

    def global_target(self, level3_id: str, level4_id: str | None) -> Target | None:
        return self.target(level3_id, level4_id, None, None)

    def summary(self) -> dict[str, Any]:
        return {"taxonomy_version": self.version, "topics": self.raw.get("topics", [])}
