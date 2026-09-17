"""题湖题库分类本地服务入口。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import sys
import tempfile
import threading
import traceback
from contextlib import contextmanager
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from collections import defaultdict, deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml

try:
    from .cache import ResultCache
    from .classifier import classify, content_hash, validate_model_decision
    from .taxonomy import Taxonomy, TaxonomyError
    from .providers import OpenAIChatCompletionsProvider
    from .providers.openai_responses import CloudProviderError
    from .batch_jobs import BatchStore
    from .classification_jobs import ClassificationJobStore
    from .proposals import ProposalStore
    from .model_input import SNAPSHOT_VERSION, build_model_input_snapshot
    from .directory_refactor import prepare_refactor_context
    from .directory_exports import write_directory_export
    from .skill_classifications import build_import_plan
    from .taxonomy_sync import synchronize_taxonomy
except ImportError:  # 支持直接运行 python server/main.py。
    from cache import ResultCache
    from classifier import classify, content_hash, validate_model_decision
    from taxonomy import Taxonomy, TaxonomyError
    from providers import OpenAIChatCompletionsProvider
    from providers.openai_responses import CloudProviderError
    from batch_jobs import BatchStore
    from classification_jobs import ClassificationJobStore
    from proposals import ProposalStore
    from model_input import SNAPSHOT_VERSION, build_model_input_snapshot
    from directory_refactor import prepare_refactor_context
    from directory_exports import write_directory_export
    from skill_classifications import build_import_plan
    from taxonomy_sync import synchronize_taxonomy


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAX_INTERACTIVE_CLASSIFICATION_QUESTIONS = 1000
MAX_TOPIC_REROUTES_PER_QUESTION = 2
ROUTING_PIPELINE_VERSION = 2


def public_cloud_error_message(error: Exception | str) -> str:
    """将云端协议诊断与面向日常用户的提示分离。"""
    detail = str(error)
    if "未返回可解析的结构化结果" in detail or "云端分类响应格式异常" in detail:
        return "云端模型本次未返回可用的分类结果，请稍后重试；若持续出现，请切换兼容的模型方案。"
    return detail


def log_cloud_error(context: str, error: Exception | str) -> None:
    """仅在本机服务日志保存脱敏的协议诊断，不回传浏览器。"""
    sys.stderr.write(f"[题湖分类服务] {context}：{error}\n")


CLOUD_PROFILE_FIELDS = (
    "protocol", "model", "routing_model", "reasoning_effort", "routing_reasoning_effort",
    "base_url", "api_key_env", "api_key", "request_compatibility", "extra_headers",
)
PROTECTED_REQUEST_HEADERS = {"authorization", "x-api-key", "content-type", "host", "content-length"}
PIPELINE_FIELDS = (
    "max_concurrent_requests", "timeout_seconds",
)


def load_settings(config_path: Path) -> dict[str, Any]:
    with config_path.open("r", encoding="utf-8") as file:
        settings = yaml.safe_load(file) or {}
    if not isinstance(settings, dict):
        raise ValueError("服务配置必须是 YAML 对象")
    return settings


def save_settings(config_path: Path, settings: dict[str, Any]) -> None:
    """原子替换本机配置，避免写到一半损坏密钥配置。"""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=config_path.parent, delete=False) as file:
        yaml.safe_dump(settings, file, allow_unicode=True, sort_keys=False)
        temporary = Path(file.name)
    temporary.replace(config_path)


def resolve_path(config_path: Path, raw_path: str) -> Path:
    return (config_path.parent / raw_path).resolve()


class ServiceState:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path.resolve()
        self.settings = load_settings(self.config_path)
        self._taxonomy_refresh_lock = threading.Lock()
        self.taxonomy_path = self._taxonomy_snapshot_path()
        sync_result = self._synchronize_taxonomy()
        self.taxonomy_sync_status = sync_result.status
        self.taxonomy = Taxonomy.from_file(self.taxonomy_path)
        rules_path = resolve_path(self.config_path, str(self.settings["rules_path"]))
        with rules_path.open("r", encoding="utf-8") as file:
            self.rules = yaml.safe_load(file) or {}
        self.rule_version = str(self.rules.get("rule_version", ""))
        if not self.rule_version:
            raise ValueError("classification-rules.yaml 缺少 rule_version")
        cache_path = resolve_path(self.config_path, str(self.settings["cache_path"]))
        self.cache = ResultCache(cache_path)
        classifier_settings = self.settings.get("classifier") or {}
        self.rules.setdefault("model", {})["auto_accept_min_confidence"] = float(classifier_settings.get("auto_accept_min_confidence", 0.92))
        # 规则正文及影响审核状态的阈值任一变化，缓存都必须自动失效。
        self.rule_hash = hashlib.sha256(
            json.dumps(self.rules, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        self.cloud: OpenAIChatCompletionsProvider | None = None
        self._refresh_cloud_provider()
        batch_path = resolve_path(self.config_path, str((classifier_settings.get("batch") or {}).get("storage_path", "../.local-data/batches")))
        self.batches = BatchStore(batch_path)
        self.classification_jobs = ClassificationJobStore()
        # 连接测试不写入设置，也不能因浏览器对单次长请求的限制而丢失完成状态。
        self._connection_test_lock = threading.Lock()
        self._connection_tests: dict[str, dict[str, Any]] = {}
        # 所有实时分类作业共用同一组模型请求槽位，避免两个 Focus 各自按 3 路并发时叠加成 6 路。
        self._llm_slot_condition = threading.Condition()
        self._active_llm_requests = 0
        proposal_minimum = int((self.rules.get("rules") or {}).get("proposal_minimum_distinct_questions", 3))
        self.proposals = ProposalStore(cache_path.with_name("directory-proposals.sqlite3"), proposal_minimum)

    def _taxonomy_snapshot_path(self) -> Path:
        """返回本机运行时实际使用的 taxonomy 快照位置。"""
        taxonomy_path = resolve_path(self.config_path, str(self.settings["taxonomy_path"]))
        generated_taxonomy = PROJECT_ROOT / ".local-data" / "taxonomy.yaml"
        example_taxonomy = (PROJECT_ROOT / "config" / "taxonomy.example.yaml").resolve()
        # 本机配置沿用示例路径时，目录快照固定写入 Git 忽略的 .local-data。
        if self.config_path.name in {"settings.example.yaml", "settings.local.yaml"} and taxonomy_path == example_taxonomy:
            return generated_taxonomy
        return taxonomy_path

    def _synchronize_taxonomy(self):
        sync_result = synchronize_taxonomy(
            config_path=self.config_path,
            workbook_settings=self.settings.get("directory_workbook"),
            output_path=self.taxonomy_path,
        )
        if sync_result.workbook_path:
            # 目录重构等后续流程必须使用与当前 taxonomy 同一版本的工作簿。
            self.settings.setdefault("directory_workbook", {})["path"] = str(sync_result.workbook_path)
        return sync_result

    def refresh_taxonomy(self) -> bool:
        """在页面读取目录或提交分类前，原子切换到新发现的目录工作簿快照。"""
        with self._taxonomy_refresh_lock:
            sync_result = self._synchronize_taxonomy()
            self.taxonomy_sync_status = sync_result.status
            if sync_result.status != "updated":
                return False
            refreshed = Taxonomy.from_file(self.taxonomy_path)
            if refreshed.version == self.taxonomy.version:
                return False
            self.taxonomy = refreshed
            return True

    def cache_key(self, question: dict[str, Any]) -> str:
        cloud_settings = self.active_cloud_settings()
        profile = self.active_cloud_profile()
        routing_model = str(cloud_settings.get("routing_model", "")).strip()
        directory_effort = str(cloud_settings.get("reasoning_effort", "high")).strip().lower()
        routing_effort = str(cloud_settings.get("routing_reasoning_effort", "medium")).strip().lower()
        material = "|".join([
            "semantic-cache-v2", str(question.get("exercise_id", "")), content_hash(question), self.taxonomy.version, self.rule_version,
            SNAPSHOT_VERSION,
            self.rule_hash,
            # 模型名相同时，更换 API 协议或提供方也必须重新生成结果。
            # 保持与旧版默认“审核关闭”结果的缓存兼容，避免移除该步骤后无谓重跑全量题目。
            (
                f"cloud:{profile.get('id', '')}:{getattr(self.cloud, 'provider_name', 'cloud')}:{getattr(self.cloud, 'base_url', cloud_settings.get('base_url', ''))}:"
                f"{getattr(self.cloud, 'model', cloud_settings.get('model', ''))}:directory:{directory_effort}:"
                f"route:{routing_model or getattr(self.cloud, 'model', cloud_settings.get('model', ''))}:{routing_effort}:audit:disabled"
            ) if self.cloud else "heuristic-v1",
        ])
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def attach_model_input_snapshot(question: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        """持久化脱敏文本快照，供模型误判时复核，不保存图片 URL 或页面令牌。"""
        result["model_input_snapshot"] = build_model_input_snapshot(question)
        return result

    @staticmethod
    def source_catalogue_id(question: dict[str, Any]) -> str:
        """题湖属性表单的叶子目录 ID，可能是三级或四级目录。"""
        return str(question.get("current_catalogue_id", "")).strip() or "__uncategorized__"

    def _resolved_manual_override(self, exercise_id: str, source_catalogue_id: str) -> dict[str, Any] | None:
        """挑出一条目标目录在当前目录里仍然成立的分类结论。

        taxonomy_version 是整份工作簿的哈希，改一个专题就会让它整体变化；照版本号
        一刀切，目标落在其他专题的人工结论会被无谓作废。所以改看标题路径还解析不
        解析得到：还在就继续生效，被改名、移位或撤销才要求模型重新分类。当前目录
        的记录排在最前，只有它解析不到时才回退到同题的其他候选。
        """
        for candidate in self.cache.get_manual_overrides(exercise_id, source_catalogue_id):
            if self.taxonomy.resolve_published_path(candidate["target_path"]):
                return candidate
        return None

    def cached_result(self, question: dict[str, Any]) -> dict[str, Any] | None:
        exercise_id = str(question["exercise_id"])
        source_catalogue_id = self.source_catalogue_id(question)
        cached = self.cache.get(self.cache_key(question), exercise_id, source_catalogue_id)
        # 旧版二阶段自检发现跨专题问题后只能停在待复核。仅让这类旧模型复核结果重跑，
        # 已通过的建议、人工修正和真实移动记录仍复用原缓存。
        if (
            cached
            and cached.get("status") == "review"
            and cached.get("classification_method") == "model"
            and isinstance(cached.get("routing"), dict)
            and cached.get("routing_pipeline_version") != ROUTING_PIPELINE_VERSION
        ):
            cached = None
        manual = self._resolved_manual_override(exercise_id, source_catalogue_id)
        if not manual:
            if cached:
                move = self.cache.latest_catalogue_moves([exercise_id]).get(exercise_id)
                if move:
                    cached = dict(cached)
                    cached["catalogue_move"] = move
            return cached
        # 人工采纳的路径优先于模型输出。旧记录是否仍然算数，看它的目标目录在当前
        # 目录里还解析不解析得到：还在就继续生效，被改名、移位或撤销才让模型重跑。
        override_source = manual.get("source") or "manual"
        reason = "已采纳人工修正" if override_source == "manual" else "目录整理 Skill 归档"
        result = dict(cached or {
            "exercise_id": exercise_id,
            "status": "suggested",
            "confidence": 1.0,
            "reason": reason,
            "review_reasons": [],
        })
        result.update({
            "exercise_id": exercise_id,
            "status": "suggested",
            "confidence": 1.0,
            "reason": reason,
            "review_reasons": [],
            "target": {"path": manual["target_path"]},
            "manual_override": {
                # 人工采纳与外部 Skill 批量导入共用这张表，靠 source 区分，
                # 前端据此标注“人工修改”还是“Skill 归类”。
                "source": override_source,
                "original_target_path": manual["original_target_path"],
                "accepted_at": manual["accepted_at"],
                "taxonomy_version": manual["taxonomy_version"],
            },
        })
        move = self.cache.latest_catalogue_moves([exercise_id]).get(exercise_id)
        if move:
            result["catalogue_move"] = move
        return result

    def cache_result(self, question: dict[str, Any], result: dict[str, Any]) -> None:
        result["routing_pipeline_version"] = ROUTING_PIPELINE_VERSION
        self.cache.put(
            self.cache_key(question),
            str(question["exercise_id"]),
            self.source_catalogue_id(question),
            result,
            stable_code=str(question.get("stable_code", "")),
        )

    @staticmethod
    def _manual_path(value: Any, field_name: str, *, allow_empty: bool = False) -> list[str]:
        if not isinstance(value, list) or len(value) > 8 or (not value and not allow_empty):
            minimum = "0" if allow_empty else "1"
            raise ValueError(f"{field_name} 必须是 {minimum} 至 8 级目录数组")
        path = [str(item).strip() for item in value]
        if any(not item or len(item) > 160 for item in path):
            raise ValueError(f"{field_name} 含有无效目录名称")
        return path

    @staticmethod
    def _history_path(value: Any, field_name: str, *, allow_empty: bool = False) -> list[str]:
        """工作成果路径最多四层；三级叶子目录不强制补出四级。"""
        if not isinstance(value, list) or len(value) > 4 or (not value and not allow_empty):
            minimum = "0" if allow_empty else "1"
            raise ValueError(f"{field_name} 必须是 {minimum} 至 4 级目录数组")
        path = [str(item).strip() for item in value]
        if any(not item or len(item) > 160 for item in path):
            raise ValueError(f"{field_name} 含有无效目录名称")
        return path

    def save_manual_classification(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存已回读确认的人工分类，不把人工决定混入模型缓存。"""
        exercise_id = str(payload.get("exercise_id", "")).strip()
        if not exercise_id or len(exercise_id) > 120:
            raise ValueError("题目 ID 无效")
        source_catalogue_id = self.source_catalogue_id(payload)
        target_path = self._manual_path(payload.get("target_path"), "target_path")
        original_target_path = self._manual_path(
            payload.get("original_target_path"), "original_target_path", allow_empty=True
        )
        stable_code = str(payload.get("stable_code", "")).strip()
        if len(stable_code) > 160:
            raise ValueError("稳定题号过长")
        override = self.cache.put_manual_override(
            exercise_id=exercise_id,
            source_catalogue_id=source_catalogue_id,
            stable_code=stable_code,
            taxonomy_version=self.taxonomy.version,
            original_target_path=original_target_path,
            target_path=target_path,
        )
        return {
            "exercise_id": exercise_id,
            "source_catalogue_id": source_catalogue_id,
            "target_path": override["target_path"],
            "accepted_at": override["accepted_at"],
            "taxonomy_version": override["taxonomy_version"],
        }

    def save_catalogue_move(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存题湖回读确认后的真实目录移动，供工作成果页查询。"""
        exercise_id = str(payload.get("exercise_id", "")).strip()
        source_catalogue_id = str(payload.get("source_catalogue_id", "")).strip()
        target_catalogue_id = str(payload.get("target_catalogue_id", "")).strip()
        stable_code = str(payload.get("stable_code", "")).strip()
        if not exercise_id or len(exercise_id) > 120:
            raise ValueError("题目 ID 无效")
        if not source_catalogue_id or not target_catalogue_id:
            raise ValueError("原目录和目标目录 ID 不能为空")
        if len(source_catalogue_id) > 160 or len(target_catalogue_id) > 160:
            raise ValueError("目录 ID 过长")
        if len(stable_code) > 160:
            raise ValueError("稳定题号过长")
        # 部分页不会加载旧目录所在的完整树。此时仍要记录真实移动，但如实保留为空路径。
        original_path = self._history_path(payload.get("original_path"), "original_path", allow_empty=True)
        target_path = self._history_path(payload.get("target_path"), "target_path")
        recorded = self.cache.record_catalogue_move(
            exercise_id=exercise_id,
            stable_code=stable_code,
            source_catalogue_id=source_catalogue_id,
            target_catalogue_id=target_catalogue_id,
            original_path=original_path,
            target_path=target_path,
        )
        return {"exercise_id": exercise_id, **recorded}

    def close(self) -> None:
        self.cache.close()
        self.batches.close()
        self.proposals.close()

    def cloud_profiles(self) -> tuple[list[dict[str, Any]], str]:
        """读取命名模型方案；旧版单 cloud 配置以“默认方案”兼容呈现。"""
        classifier = self.settings.get("classifier") or {}
        stored = classifier.get("cloud_profiles") or {}
        profiles = stored.get("profiles") if isinstance(stored, dict) else None
        if isinstance(profiles, list) and profiles:
            normalized = [dict(item) for item in profiles if isinstance(item, dict) and str(item.get("id", "")).strip()]
            active_id = str(stored.get("active_id", "")).strip()
            if normalized and any(str(item["id"]) == active_id for item in normalized):
                return normalized, active_id
            if normalized:
                return normalized, str(normalized[0]["id"])
        legacy = dict(classifier.get("cloud") or {})
        return [{"id": "default", "name": "默认方案", **legacy}], "default"

    def active_cloud_profile(self) -> dict[str, Any]:
        profiles, active_id = self.cloud_profiles()
        return next((item for item in profiles if str(item.get("id")) == active_id), profiles[0])

    def pipeline_settings(self) -> dict[str, Any]:
        classifier = self.settings.get("classifier") or {}
        legacy = classifier.get("cloud") or {}
        configured = classifier.get("pipeline") or {}
        return {field: configured.get(field, legacy.get(field)) for field in PIPELINE_FIELDS if configured.get(field, legacy.get(field)) is not None}

    def active_cloud_settings(self) -> dict[str, Any]:
        profile = self.active_cloud_profile()
        settings = {field: profile[field] for field in CLOUD_PROFILE_FIELDS if field in profile}
        settings.update(self.pipeline_settings())
        return settings

    def cloud_for_batch(self, job: dict[str, Any]) -> OpenAIChatCompletionsProvider:
        """批任务始终使用创建时的模型方案，避免用户切换方案后协议错配。"""
        profile_id = str(job.get("cloud_profile_id", "")).strip()
        if not profile_id:
            return self._require_cloud_provider()
        profiles, _active_id = self.cloud_profiles()
        profile = next((item for item in profiles if str(item.get("id")) == profile_id), None)
        if not profile:
            raise ValueError("批处理所用的模型方案已被删除")
        settings = {field: profile[field] for field in CLOUD_PROFILE_FIELDS if field in profile}
        settings.update(self.pipeline_settings())
        return OpenAIChatCompletionsProvider(settings)

    def _require_cloud_provider(self) -> OpenAIChatCompletionsProvider:
        if not self.cloud:
            raise ValueError("尚未启用 cloud_hybrid 分类器")
        return self.cloud

    def _refresh_cloud_provider(self) -> None:
        classifier = self.settings.get("classifier") or {}
        cloud_settings = self.active_cloud_settings()
        if classifier.get("mode") == "cloud_hybrid" and str(cloud_settings.get("model", "")).strip():
            self.cloud = OpenAIChatCompletionsProvider(cloud_settings)
        else:
            self.cloud = None

    def _ensure_cloud_profile_store(self) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        """将旧配置仅在写入时迁移为方案列表，避免启动时意外改写用户文件。"""
        classifier = self.settings.setdefault("classifier", {})
        stored = classifier.get("cloud_profiles")
        if not isinstance(stored, dict) or not isinstance(stored.get("profiles"), list) or not stored["profiles"]:
            legacy = dict(classifier.get("cloud") or {})
            profile = {"id": "default", "name": "默认方案"}
            profile.update({field: legacy[field] for field in CLOUD_PROFILE_FIELDS if field in legacy})
            legacy_pipeline = {field: legacy[field] for field in PIPELINE_FIELDS if field in legacy}
            stored = {"active_id": "default", "profiles": [profile]}
            classifier["cloud_profiles"] = stored
            classifier.pop("cloud", None)
        else:
            legacy_pipeline = {}
        profiles = stored["profiles"]
        pipeline = classifier.setdefault("pipeline", {})
        # 已迁移用户仍可从旧配置继承一次共享处理设置。
        for field in PIPELINE_FIELDS:
            if field not in pipeline and field in legacy_pipeline:
                pipeline[field] = legacy_pipeline[field]
        return stored, profiles, pipeline

    @staticmethod
    def _profile_id() -> str:
        return f"profile-{secrets.token_hex(4)}"

    @staticmethod
    def _profile_name(value: Any) -> str:
        name = str(value or "").strip()
        if not name or len(name) > 40:
            raise ValueError("方案名称不能为空，且长度不能超过 40")
        return name

    @staticmethod
    def _request_compatibility(value: Any) -> str:
        compatibility = str(value or "standard").strip().lower()
        if compatibility not in {"standard", "go_http"}:
            raise ValueError("请求兼容方式无效")
        return compatibility

    @staticmethod
    def _extra_headers(value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            raise ValueError("自定义请求头格式无效")
        if len(value) > 12:
            raise ValueError("自定义请求头最多 12 项")
        headers: dict[str, str] = {}
        seen: set[str] = set()
        for raw_name, raw_value in value.items():
            name = str(raw_name or "").strip()
            header_value = str(raw_value or "").strip()
            normalized_name = name.lower()
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,80}", name):
                raise ValueError("自定义请求头名称无效")
            if normalized_name in PROTECTED_REQUEST_HEADERS:
                raise ValueError(f"自定义请求头不能覆盖 {name}")
            if normalized_name in seen or not header_value or len(header_value) > 1_000 or "\r" in header_value or "\n" in header_value:
                raise ValueError("自定义请求头内容无效")
            seen.add(normalized_name)
            headers[name] = header_value
        return headers

    def _persist_cloud_settings(self) -> None:
        target_config = self.config_path
        if target_config.name == "settings.example.yaml":
            target_config = target_config.with_name("settings.local.yaml")
        save_settings(target_config, self.settings)
        self.config_path = target_config
        self._refresh_cloud_provider()
        with self._llm_slot_condition:
            self._llm_slot_condition.notify_all()

    def cloud_summary(self) -> dict[str, Any]:
        profile = self.active_cloud_profile()
        pipeline = self.pipeline_settings()
        profiles, active_id = self.cloud_profiles()
        def custom_header_names(item: dict[str, Any]) -> list[str]:
            headers = item.get("extra_headers")
            return sorted(str(name) for name in headers) if isinstance(headers, dict) else []
        profile_summaries = [{
            "id": str(item.get("id", "")), "name": str(item.get("name", "")),
            "protocol": str(item.get("protocol", "responses")), "model": str(item.get("model", "")), "routing_model": str(item.get("routing_model", "")),
            "reasoning_effort": str(item.get("reasoning_effort", "high")),
            "routing_reasoning_effort": str(item.get("routing_reasoning_effort", "medium")),
            "request_compatibility": str(item.get("request_compatibility", "standard")),
            "custom_header_names": custom_header_names(item),
            "base_url": str(item.get("base_url", "https://api.openai.com/v1")),
            "api_key_configured": bool(item.get("api_key") or (str(item.get("id")) == active_id and self.cloud and self.cloud.configured)),
        } for item in profiles]
        return {
            "mode": (self.settings.get("classifier") or {}).get("mode", "heuristic"),
            "active_profile_id": active_id,
            "active_profile_name": profile.get("name", ""),
            "profiles": profile_summaries,
            "protocol": profile.get("protocol", "responses"),
            "model": profile.get("model", ""),
            "routing_model": profile.get("routing_model", ""),
            "reasoning_effort": profile.get("reasoning_effort", "high"),
            "routing_reasoning_effort": profile.get("routing_reasoning_effort", "medium"),
            "request_compatibility": profile.get("request_compatibility", "standard"),
            "custom_header_names": custom_header_names(profile),
            "max_concurrent_requests": self.llm_concurrency(),
            "pipeline": {"max_concurrent_requests": self.llm_concurrency()},
            "base_url": profile.get("base_url", "https://api.openai.com/v1"),
            "api_key_env": profile.get("api_key_env", "OPENAI_API_KEY"),
            "api_key_configured": bool(profile.get("api_key") or (self.cloud and self.cloud.configured)),
        }

    def update_cloud_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action", "save_profile")).strip()
        stored, profiles, pipeline = self._ensure_cloud_profile_store()
        profile_id = str(payload.get("profile_id") or stored.get("active_id") or "").strip()
        profile = next((item for item in profiles if str(item.get("id")) == profile_id), None)
        if action == "create_profile":
            name = self._profile_name(payload.get("name"))
            if any(str(item.get("name", "")).casefold() == name.casefold() for item in profiles):
                raise ValueError("已有同名模型方案")
            profile = {"id": self._profile_id(), "name": name}
            profiles.append(profile)
            stored["active_id"] = profile["id"]
            self._persist_cloud_settings()
            return self.cloud_summary()
        if not profile:
            raise ValueError("未找到所选模型方案")
        if action == "select_profile":
            stored["active_id"] = profile_id
            self._persist_cloud_settings()
            return self.cloud_summary()
        if action == "rename_profile":
            profile_name = self._profile_name(payload.get("name", profile.get("name")))
            if any(
                str(item.get("id")) != profile_id and str(item.get("name", "")).casefold() == profile_name.casefold()
                for item in profiles
            ):
                raise ValueError("已有同名模型方案")
            profile["name"] = profile_name
            self._persist_cloud_settings()
            return self.cloud_summary()
        if action == "delete_profile":
            if len(profiles) == 1:
                raise ValueError("至少保留一套模型方案")
            profiles.remove(profile)
            stored["active_id"] = str(profiles[0]["id"]) if stored.get("active_id") == profile_id else str(stored.get("active_id"))
            self._persist_cloud_settings()
            return self.cloud_summary()
        if action == "reorder_profiles":
            supplied_ids = payload.get("profile_ids")
            if not isinstance(supplied_ids, list):
                raise ValueError("模型方案排序格式无效")
            ordered_ids = [str(item).strip() for item in supplied_ids]
            known_ids = [str(item.get("id", "")) for item in profiles]
            if len(ordered_ids) != len(known_ids) or not all(ordered_ids) or set(ordered_ids) != set(known_ids):
                raise ValueError("模型方案排序与当前方案不一致，请刷新后重试")
            by_id = {str(item["id"]): item for item in profiles}
            profiles[:] = [by_id[item_id] for item_id in ordered_ids]
            self._persist_cloud_settings()
            return self.cloud_summary()
        if action != "save_profile":
            raise ValueError("未知的模型方案操作")

        model = str(payload.get("model", "")).strip()
        protocol = str(payload.get("protocol", profile.get("protocol", "responses"))).strip().lower()
        routing_model = str(payload.get("routing_model", "")).strip()
        request_compatibility = self._request_compatibility(payload.get("request_compatibility", profile.get("request_compatibility")))
        reasoning_effort = str(payload.get("reasoning_effort", "high")).strip().lower()
        routing_reasoning_effort = str(payload.get("routing_reasoning_effort", "medium")).strip().lower()
        try:
            pipeline_input = payload.get("pipeline") if isinstance(payload.get("pipeline"), dict) else payload
            max_concurrent_requests = int(pipeline_input.get("max_concurrent_requests", pipeline.get("max_concurrent_requests", 3)))
        except (TypeError, ValueError) as error:
            raise ValueError("LLM 并发数必须是 1 到 5") from error
        base_url = OpenAIChatCompletionsProvider.normalize_base_url(
            payload.get("base_url", profile.get("base_url", "https://api.openai.com"))
        )
        api_key_env = str(payload.get("api_key_env", profile.get("api_key_env", "OPENAI_API_KEY"))).strip()
        if not model or len(model) > 160: raise ValueError("模型名不能为空，且长度不能超过 160")
        if protocol not in {"responses", "chat_completions", "anthropic_messages"}:
            raise ValueError("协议必须是 Responses、Chat Completions 或 Claude Messages")
        if len(routing_model) > 160: raise ValueError("专题路由模型名长度不能超过 160")
        allowed_efforts = {"none", "low", "medium", "high", "xhigh", "max"}
        if reasoning_effort not in allowed_efforts:
            raise ValueError("目录分类推理强度必须是 none、low、medium、high、xhigh 或 max")
        if routing_reasoning_effort not in allowed_efforts:
            raise ValueError("专题路由推理强度必须是 none、low、medium、high、xhigh 或 max")
        if not 1 <= max_concurrent_requests <= 5:
            raise ValueError("LLM 并发数必须是 1 到 5")
        if not base_url.startswith(("https://", "http://")): raise ValueError("接口地址必须以 http:// 或 https:// 开头")
        if not api_key_env or len(api_key_env) > 120: raise ValueError("密钥环境变量名无效")
        classifier = self.settings.setdefault("classifier", {}); classifier["mode"] = "cloud_hybrid"; profile.update({
            "protocol": protocol, "model": model, "base_url": base_url, "api_key_env": api_key_env,
            "reasoning_effort": reasoning_effort,
            "routing_reasoning_effort": routing_reasoning_effort,
            "request_compatibility": request_compatibility,
        })
        pipeline["max_concurrent_requests"] = max_concurrent_requests
        profile_name = self._profile_name(payload.get("name", profile.get("name")))
        if any(
            str(item.get("id")) != profile_id and str(item.get("name", "")).casefold() == profile_name.casefold()
            for item in profiles
        ):
            raise ValueError("已有同名模型方案")
        profile["name"] = profile_name
        if routing_model:
            profile["routing_model"] = routing_model
        else:
            profile.pop("routing_model", None)
        if "extra_headers" in payload:
            profile["extra_headers"] = self._extra_headers(payload["extra_headers"])
        elif payload.get("clear_extra_headers") is True:
            profile.pop("extra_headers", None)
        # 清理旧版本留下的独立审核配置，后续不会再触发第三次模型调用。
        profile.pop("audit_mode", None)
        if payload.get("clear_api_key") is True: profile.pop("api_key", None)
        elif "api_key" in payload:
            api_key = str(payload.get("api_key") or "").strip()
            if len(api_key) < 8: raise ValueError("API 密钥长度异常；如需清除请使用 clear_api_key")
            profile["api_key"] = api_key
        stored["active_id"] = profile_id
        self._persist_cloud_settings()
        return self.cloud_summary()

    def _connection_test_provider(self, payload: dict[str, Any]) -> OpenAIChatCompletionsProvider:
        """由草稿或已保存方案构造一次性测试客户端，不写入密钥和设置。"""
        profile = dict(self.active_cloud_profile())
        settings = {field: profile[field] for field in CLOUD_PROFILE_FIELDS if field in profile}
        settings.update(self.pipeline_settings())
        for field in ("protocol", "routing_model", "reasoning_effort", "routing_reasoning_effort", "request_compatibility", "base_url", "api_key_env"):
            if field in payload:
                settings[field] = payload[field]
        if "extra_headers" in payload:
            settings["extra_headers"] = self._extra_headers(payload["extra_headers"])
        elif payload.get("clear_extra_headers") is True:
            settings.pop("extra_headers", None)
        # 连接测试只读取 /models；即使新方案尚未选择模型也应允许测试。
        settings["allow_empty_model"] = True
        if str(payload.get("api_key") or "").strip():
            settings["api_key"] = str(payload["api_key"]).strip()
        parsed_url = urlparse(str(settings.get("base_url", "")))
        if parsed_url.hostname in {"chatgpt.com", "chat.openai.com"} and "/backend-api/" in parsed_url.path:
            raise ValueError("chatgpt.com/backend-api 是 ChatGPT/Codex 登录态内部接口，不支持 API 密钥直连；请使用公开 API 地址或兼容网关地址")
        return OpenAIChatCompletionsProvider(settings)

    def test_cloud_connection(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._connection_test_provider(payload).test_connection()

    def start_cloud_connection_test(self, payload: dict[str, Any]) -> dict[str, Any]:
        """立即返回测试编号，实际网络调用在后台执行，供浏览器短轮询读取。"""
        provider = self._connection_test_provider(payload)
        test_id = f"connection-test-{secrets.token_hex(8)}"
        with self._connection_test_lock:
            # 仅保存不含密钥的状态和结果；淘汰已结束的旧记录以控制内存。
            finished = [key for key, item in self._connection_tests.items() if item.get("status") != "running"]
            for key in finished[:-50]:
                self._connection_tests.pop(key, None)
            self._connection_tests[test_id] = {"test_id": test_id, "status": "running"}

        def run() -> None:
            try:
                result = provider.test_connection()
                snapshot: dict[str, Any] = {"test_id": test_id, "status": "succeeded", "result": result}
            except CloudProviderError as error:
                # 详细原因只写入本机服务日志，避免把协议/网关诊断噪音暴露给日常使用者。
                log_cloud_error("云端连接测试失败", error)
                snapshot = {"test_id": test_id, "status": "failed", "message": public_cloud_error_message(error)}
            except ValueError as error:
                snapshot = {"test_id": test_id, "status": "failed", "message": str(error)}
            except Exception as error:
                traceback.print_exc(file=sys.stderr)
                snapshot = {"test_id": test_id, "status": "failed", "message": f"连接测试异常：{type(error).__name__}"}
            with self._connection_test_lock:
                self._connection_tests[test_id] = snapshot

        threading.Thread(target=run, name=test_id, daemon=True).start()
        return {"test_id": test_id, "status": "running"}

    def cloud_connection_test(self, test_id: str) -> dict[str, Any]:
        with self._connection_test_lock:
            snapshot = self._connection_tests.get(test_id)
            if not snapshot:
                raise ValueError("未找到连接测试任务")
            return dict(snapshot)

    def routing_cloud(self) -> OpenAIChatCompletionsProvider | None:
        """专题路由可单独配置模型和推理强度；留空模型时共用目录分类模型。"""
        if not self.cloud:
            return None
        cloud_settings = self.active_cloud_settings()
        routing_model = str(cloud_settings.get("routing_model", "")).strip() or self.cloud.model
        routing_effort = str(cloud_settings.get("routing_reasoning_effort", "medium")).strip().lower()
        directory_effort = str(cloud_settings.get("reasoning_effort", "high")).strip().lower()
        if routing_model == self.cloud.model and routing_effort == getattr(self.cloud, "reasoning_effort", directory_effort):
            return self.cloud
        routing_settings = dict(cloud_settings)
        routing_settings["model"] = routing_model
        routing_settings["reasoning_effort"] = routing_effort
        return OpenAIChatCompletionsProvider(routing_settings)

    def llm_concurrency(self) -> int:
        """限制服务全部实时流水线的总在途请求数，保护网关免受突发并发冲击。"""
        cloud_settings = self.active_cloud_settings()
        try:
            value = int(cloud_settings.get("max_concurrent_requests", 3))
        except (TypeError, ValueError):
            value = 3
        return max(1, min(5, value))

    @contextmanager
    def _llm_request_slot(self):
        """为一次真实云端调用申请服务级并发槽位。"""
        with self._llm_slot_condition:
            while self._active_llm_requests >= self.llm_concurrency():
                self._llm_slot_condition.wait()
            self._active_llm_requests += 1
        try:
            yield
        finally:
            with self._llm_slot_condition:
                self._active_llm_requests -= 1
                self._llm_slot_condition.notify_all()

    def _call_cloud(self, operation: Any, *args: Any) -> Any:
        """所有实时分类阶段都经由共享闸门调用云端。

        兼容网关偶尔会返回与其声明协议不符的对象。该类异常不应让整页
        分类作业中断；转换为 ``CloudProviderError`` 后，会仅把当前分批
        标为待人工复核，并保留其余分批的执行机会。
        """
        with self._llm_request_slot():
            try:
                return operation(*args)
            except CloudProviderError as error:
                log_cloud_error("云端分类调用失败", error)
                raise
            except (AttributeError, KeyError, TypeError) as error:
                raise CloudProviderError(
                    f"云端分类响应格式异常（{type(error).__name__}）"
                ) from error

    def create_classification_job(self, questions: Any) -> dict[str, Any]:
        """提交整页实时分类作业；HTTP 立刻返回，长推理在本机后台继续执行。"""
        if not isinstance(questions, list) or not questions:
            raise ValueError("questions 必须是非空数组")
        if len(questions) > MAX_INTERACTIVE_CLASSIFICATION_QUESTIONS:
            raise ValueError(f"单个分类作业最多处理 {MAX_INTERACTIVE_CLASSIFICATION_QUESTIONS} 道题")
        normalized: list[dict[str, Any]] = []
        exercise_ids: set[str] = set()
        for question in questions:
            if not isinstance(question, dict):
                raise ValueError("题目必须是对象")
            exercise_id = str(question.get("exercise_id", "")).strip()
            if not exercise_id or exercise_id in exercise_ids:
                raise ValueError("题目 ID 不能为空或重复")
            exercise_ids.add(exercise_id)
            normalized.append(dict(question))
        job = self.classification_jobs.create(normalized)
        worker = threading.Thread(
            target=self._run_classification_job, args=(job.job_id,), daemon=True, name=f"wulou-{job.job_id[-8:]}"
        )
        worker.start()
        return self.classification_jobs.snapshot(job.job_id)

    def import_skill_classifications(self, payload: dict[str, Any]) -> dict[str, Any]:
        """导入目录整理 Skill 的逐题归类结果。

        外部 Skill 拿不到本机回环地址，也预知不了本地目录 ID，所以只交回「题号 +
        知识点编号」。这里按当前目录版本反查目录、逐条校验，再写入人工修正表并标记
        来源为 skill；前端原有的缓存读取链路随即就能展示这些分类建议。
        """
        plan = build_import_plan(payload, self.taxonomy)
        report = {
            "schema_version": plan["schema_version"],
            "taxonomy_version": plan["taxonomy_version"],
            "file_taxonomy_version": plan["file_taxonomy_version"],
            "received": plan["received"],
            "resolved": plan["resolved"],
            "written": 0,
            "inserted": 0,
            "updated": 0,
            "skipped_manual_decisions": 0,
            "failed": plan["failed"],
            "warnings": plan["warnings"],
            "preview": plan["preview"],
            "dry_run": bool(payload.get("dry_run")),
        }
        if report["dry_run"]:
            return report
        counts = self.cache.put_skill_classifications(
            plan["rows"], preserve_manual_decisions=bool(payload.get("preserve_manual_decisions"))
        )
        report.update(counts)
        report["written"] = counts["inserted"] + counts["updated"]
        if counts["skipped_manual_decisions"]:
            report["warnings"].append(
                f"{counts['skipped_manual_decisions']} 条已有同一目录版本内的人工修正，已按请求保留，未被覆盖。"
            )
        return report

    def create_directory_export(self, payload: dict[str, Any]) -> dict[str, Any]:
        """保存完整题库交接包，供用户手动启动的 Agent 使用。"""
        context = prepare_refactor_context(payload, self.taxonomy)
        return write_directory_export(
            PROJECT_ROOT / ".local-data" / "directory-exports",
            context=context,
            taxonomy_version=self.taxonomy.version,
            rule_version=self.rule_version,
        )

    @staticmethod
    def _chunks(items: list[Any], size: int) -> list[list[Any]]:
        return [items[index:index + size] for index in range(0, len(items), size)]

    def _put_job_result(self, job_id: str, question: dict[str, Any], result: dict[str, Any], *, failed: bool = False) -> None:
        """结果先落缓存再更新作业状态，页面刷新或长连接中断后仍可复用。"""
        exercise_id = str(question["exercise_id"])
        result["rule_version"] = self.rule_version
        result["cache_hit"] = False
        self.attach_model_input_snapshot(question, result)
        if not failed:
            self.cache_result(question, result)
            self.proposals.record(question, result)
        self.classification_jobs.record_result(job_id, result, failed=failed)

    def _routing_review_result(self, question: dict[str, Any], routing: dict[str, Any]) -> dict[str, Any]:
        """专题路由无法可靠定位时，不允许进入专题内目录分类。"""
        return validate_model_decision(
            question,
            {
                "status": "review",
                "confidence": routing.get("confidence", 0),
                "reason": routing.get("reason") or "无法确定最终归属专题",
                "review_reasons": routing.get("review_reasons") or ["topic_routing_failed"],
                "routing": routing,
                "proposal": {"kind": "none", "title": None, "cluster_key": None, "reason": None},
            },
            self.taxonomy,
            self.rules,
        )

    def _cloud_review_result(self, exercise_id: str, reason: Exception | str) -> dict[str, Any]:
        """把单个云端分批异常安全降级为待人工复核结果。"""
        return {
            "exercise_id": exercise_id,
            "taxonomy_version": self.taxonomy.version,
            "rule_version": self.rule_version,
            "status": "review",
            "needs_review": True,
            "review_reasons": ["cloud_request_failed"],
            "classification_method": "model",
            "confidence": 0.0,
            "reason": public_cloud_error_message(reason),
            "target": None,
            "proposal_required": False,
            "proposal_cluster_id": None,
            "cache_hit": False,
        }

    def _run_classification_job(self, job_id: str) -> None:
        """按总并发上限流水化路由、专题内分类和必要审核，完成即持续写回。"""
        try:
            job = self.classification_jobs.get(job_id)
            questions = job.questions
            uncached: list[dict[str, Any]] = []
            for question in questions:
                cached = self.cached_result(question)
                if cached:
                    cached["cache_hit"] = True
                    self.classification_jobs.record_result(job_id, cached)
                else:
                    uncached.append(question)

            if not uncached:
                self.classification_jobs.complete(job_id)
                return
            cloud = self.cloud
            routing_cloud = self.routing_cloud()
            if not cloud or not routing_cloud or not cloud.configured or not routing_cloud.configured:
                self.classification_jobs.set_running(job_id, "classifying")
                for question in uncached:
                    self._put_job_result(job_id, question, classify(question, self.taxonomy, self.rules))
                self.classification_jobs.complete(job_id)
                return

            self.classification_jobs.set_running(job_id, "routing")
            routed_count = len(questions) - len(uncached)
            self.classification_jobs.set_stage(job_id, "routing", routed_count)
            route_queue = deque(self._chunks(uncached, 10))
            classify_queue: deque[tuple[str, list[dict[str, Any]], dict[str, dict[str, Any]]]] = deque()
            reroute_attempts: defaultdict[str, int] = defaultdict(int)
            total_limit = self.llm_concurrency()

            def finalize(batch: list[dict[str, Any]], decisions: list[dict[str, Any]]) -> None:
                for question, decision in zip(batch, decisions, strict=True):
                    result = validate_model_decision(question, decision, self.taxonomy, self.rules)
                    self._put_job_result(job_id, question, result)

            def corrected_routing(question: dict[str, Any], decision: dict[str, Any], current_topic_id: str) -> tuple[str, dict[str, Any]] | None:
                """仅接受二阶段自检明确给出的跨专题修正，最多重路由两次以避免循环。"""
                self_check = decision.get("self_check") if isinstance(decision.get("self_check"), dict) else {}
                corrected_topic_id = str(self_check.get("reroute_topic_id") or "").strip()
                if decision.get("status") != "review" or self_check.get("passed") is not False:
                    return None
                if not corrected_topic_id or corrected_topic_id == current_topic_id or not self.taxonomy.topic(corrected_topic_id):
                    return None
                if self.taxonomy.is_large_question_scope(question) and not self.taxonomy.large_question_level2_id(corrected_topic_id):
                    decision["review_reasons"] = list(decision.get("review_reasons") or []) + ["target_topic_large_question_directory_missing"]
                    decision["reason"] = "修正专题未配置唯一的【大题】二级目录，无法安全重路由"
                    return None
                exercise_id = str(question["exercise_id"])
                if reroute_attempts[exercise_id] >= MAX_TOPIC_REROUTES_PER_QUESTION:
                    decision["review_reasons"] = list(decision.get("review_reasons") or []) + ["topic_reroute_limit_reached"]
                    decision["reason"] = "已完成最多两次专题重路由，仍无法稳定确定归属，请人工复核"
                    return None
                reroute_attempts[exercise_id] += 1
                routing = dict(decision.get("routing") or {})
                routing["initial_topic_id"] = routing.get("initial_topic_id") or current_topic_id
                routing["rerouted_from_topic_id"] = current_topic_id
                routing["latest_topic_id"] = corrected_topic_id
                routing["reroute_reason"] = str(self_check.get("reason") or decision.get("reason") or "二阶段目录自检要求修正专题")
                routing["reroute_count"] = reroute_attempts[exercise_id]
                return corrected_topic_id, routing

            # 单个执行池对专题路由和专题内分类共享总上限；仍保留一个路由槽位，避免全页栅栏等待。
            with ThreadPoolExecutor(max_workers=total_limit) as executor:
                futures: dict[Any, tuple[str, Any]] = {}

                def schedule() -> None:
                    while len(futures) < total_limit:
                        active_route = any(kind == "routing" for kind, _payload in futures.values())
                        if route_queue and not active_route:
                            batch = route_queue.popleft()
                            future = executor.submit(
                                self._call_cloud, routing_cloud.route_fast_batch, batch, self.taxonomy, self.rules
                            )
                            futures[future] = ("routing", batch)
                        elif classify_queue:
                            topic_id, batch, routing_by_id = classify_queue.popleft()
                            future = executor.submit(
                                self._call_cloud, cloud.classify_topic_batch,
                                batch, topic_id, self.taxonomy, self.rules, routing_by_id,
                            )
                            futures[future] = ("classifying", (topic_id, batch))
                            self.classification_jobs.set_stage(job_id, "classifying", routed_count)
                        elif route_queue:
                            batch = route_queue.popleft()
                            future = executor.submit(
                                self._call_cloud, routing_cloud.route_fast_batch, batch, self.taxonomy, self.rules
                            )
                            futures[future] = ("routing", batch)
                        else:
                            return

                schedule()
                while futures:
                    done, _pending = wait(futures, return_when=FIRST_COMPLETED)
                    for future in done:
                        kind, payload = futures.pop(future)
                        if kind == "routing":
                            batch = payload
                            try:
                                routings = future.result()
                            except CloudProviderError as error:
                                for question in batch:
                                    self._put_job_result(job_id, question, self._cloud_review_result(str(question["exercise_id"]), error), failed=True)
                            else:
                                routed_groups: dict[tuple[str, bool], list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
                                for question, routing in zip(batch, routings, strict=True):
                                    topic_id = str(routing.get("latest_topic_id") or "")
                                    if routing.get("status") != "routed" or not self.taxonomy.topic(topic_id):
                                        self._put_job_result(job_id, question, self._routing_review_result(question, routing))
                                    elif self.taxonomy.is_large_question_scope(question) and not self.taxonomy.large_question_level2_id(topic_id):
                                        self._put_job_result(job_id, question, self._routing_review_result(question, {
                                            **routing,
                                            "status": "review",
                                            "reason": "目标专题未配置唯一的【大题】二级目录，无法安全重路由",
                                            "review_reasons": ["target_topic_large_question_directory_missing"],
                                        }))
                                    else:
                                        routed_groups[(topic_id, self.taxonomy.is_large_question_scope(question))].append((question, routing))
                                for (topic_id, _is_large_question), pairs in routed_groups.items():
                                    batch_questions = [item[0] for item in pairs]
                                    routing_by_id = {str(item[0]["exercise_id"]): item[1] for item in pairs}
                                    classify_queue.append((topic_id, batch_questions, routing_by_id))
                            routed_count += len(batch)
                            self.classification_jobs.set_stage(job_id, "routing", routed_count)
                        elif kind == "classifying":
                            topic_id, batch = payload
                            try:
                                decisions = future.result()
                            except (CloudProviderError, ValueError) as error:
                                for question in batch:
                                    self._put_job_result(job_id, question, self._cloud_review_result(str(question["exercise_id"]), error), failed=True)
                            else:
                                settled_batch: list[dict[str, Any]] = []
                                settled_decisions: list[dict[str, Any]] = []
                                rerouted_groups: dict[tuple[str, bool], list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
                                for question, decision in zip(batch, decisions, strict=True):
                                    rerouted = corrected_routing(question, decision, topic_id)
                                    if rerouted:
                                        corrected_topic_id, routing = rerouted
                                        rerouted_groups[(corrected_topic_id, self.taxonomy.is_large_question_scope(question))].append((question, routing))
                                        continue
                                    settled_batch.append(question)
                                    settled_decisions.append(decision)
                                for (corrected_topic_id, _is_large_question), pairs in rerouted_groups.items():
                                    rerouted_questions = [item[0] for item in pairs]
                                    rerouted_by_id = {str(item[0]["exercise_id"]): item[1] for item in pairs}
                                    classify_queue.append((corrected_topic_id, rerouted_questions, rerouted_by_id))
                                if settled_batch:
                                    finalize(settled_batch, settled_decisions)
                        schedule()
            self.classification_jobs.complete(job_id)
        except Exception as error:
            # 作业状态接口只返回安全摘要；完整堆栈留在本机启动终端。
            traceback.print_exc(file=sys.stderr)
            self.classification_jobs.fail(job_id, f"分类作业异常：{type(error).__name__}")


class RequestHandler(BaseHTTPRequestHandler):
    server: "CurationServer"

    def log_message(self, format: str, *args: Any) -> None:
        # 不记录题干、答案或云端模型密钥。
        sys.stderr.write("[题湖分类服务] %s\n" % (format % args))

    def do_GET(self) -> None:  # noqa: N802
        request_url = urlparse(self.path)
        if request_url.path in {"/health", "/api/v1/taxonomy"}:
            try:
                self.server.state.refresh_taxonomy()
            except (ValueError, TaxonomyError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "taxonomy_sync_failed", "message": str(error)})
                return
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, {
                "status": "ok", "service": "wulou-question-curation-assistant", "host": "127.0.0.1",
                "port": self.server.server_port, "taxonomy_version": self.server.state.taxonomy.version,
                "rule_version": self.server.state.rule_version, "rule_hash": self.server.state.rule_hash,
                "cloud_configured": bool(self.server.state.cloud and self.server.state.cloud.configured),
            })
            return
        if self.path == "/api/v1/taxonomy":
            self._send_json(HTTPStatus.OK, self.server.state.taxonomy.summary())
            return
        if self.path == "/api/v1/settings/cloud":
            self._send_json(HTTPStatus.OK, self.server.state.cloud_summary()); return
        if request_url.path.startswith("/api/v1/settings/cloud/test/"):
            test_id = request_url.path.rsplit("/", 1)[-1]
            try:
                self._send_json(HTTPStatus.OK, self.server.state.cloud_connection_test(test_id))
            except ValueError as error:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "message": str(error)})
            return
        if request_url.path == "/api/v1/history/catalogue-moves":
            query = parse_qs(request_url.query)
            date_values = query.get("date", [])
            start_values = query.get("start_date", [])
            end_values = query.get("end_date", [])
            if any(len(values) > 1 for values in (date_values, start_values, end_values)):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "message": "日期参数只能提供一次"}); return
            try:
                report = self.server.state.cache.catalogue_move_report(
                    selected_date=date_values[0] if date_values else None,
                    start_date=start_values[0] if start_values else None,
                    end_date=end_values[0] if end_values else None,
                )
            except ValueError as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad_request", "message": str(error)}); return
            self._send_json(HTTPStatus.OK, report); return
        if self.path.startswith("/api/v1/classification-jobs/"):
            job_id = self.path.rsplit("/", 1)[-1]
            self._send_json(HTTPStatus.OK, self.server.state.classification_jobs.snapshot(job_id)); return
        if self.path.startswith("/api/v1/batches/"):
            job_id = self.path.rsplit("/", 1)[-1]
            self._send_json(HTTPStatus.OK, self.server.state.batches.get(job_id)); return
        if self.path == "/api/v1/directory-proposals":
            self._send_json(HTTPStatus.OK, {"proposals": self.server.state.proposals.list()}); return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in {"/api/v1/classify", "/api/v1/classify/batch", "/api/v1/classification-jobs", "/api/v1/skill-classifications", "/api/v1/directory-exports", "/api/v1/batches", "/api/v1/batches/submit", "/api/v1/batches/refresh", "/api/v1/batches/import", "/api/v1/settings/cloud", "/api/v1/settings/cloud/test", "/api/v1/settings/cloud/test/start", "/api/v1/cache/classifications/delete", "/api/v1/cache/classifications/lookup", "/api/v1/manual-classifications", "/api/v1/history/catalogue-moves"}:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        try:
            payload = self._read_json(maximum_length=12_000_000 if self.path in {"/api/v1/directory-exports", "/api/v1/classification-jobs", "/api/v1/skill-classifications"} else 2_000_000)
            if self.path in {
                "/api/v1/classify", "/api/v1/classify/batch", "/api/v1/classification-jobs",
                "/api/v1/cache/classifications/lookup", "/api/v1/manual-classifications",
                "/api/v1/skill-classifications", "/api/v1/directory-exports", "/api/v1/batches",
            }:
                self.server.state.refresh_taxonomy()
            if self.path == "/api/v1/settings/cloud":
                self._send_json(HTTPStatus.OK, self.server.state.update_cloud_settings(payload))
            elif self.path == "/api/v1/settings/cloud/test/start":
                self._send_json(HTTPStatus.ACCEPTED, self.server.state.start_cloud_connection_test(payload))
            elif self.path == "/api/v1/settings/cloud/test":
                self._send_json(HTTPStatus.OK, self.server.state.test_cloud_connection(payload))
            elif self.path == "/api/v1/skill-classifications":
                self._send_json(HTTPStatus.OK, self.server.state.import_skill_classifications(payload))
            elif self.path == "/api/v1/directory-exports":
                self._send_json(HTTPStatus.CREATED, self.server.state.create_directory_export(payload))
            elif self.path == "/api/v1/manual-classifications":
                self._send_json(HTTPStatus.CREATED, self.server.state.save_manual_classification(payload))
            elif self.path == "/api/v1/history/catalogue-moves":
                self._send_json(HTTPStatus.CREATED, self.server.state.save_catalogue_move(payload))
            elif self.path == "/api/v1/classification-jobs":
                self._send_json(HTTPStatus.ACCEPTED, self.server.state.create_classification_job(payload.get("questions")))
            elif self.path == "/api/v1/cache/classifications/lookup":
                questions = payload.get("questions")
                if not isinstance(questions, list) or not questions:
                    raise ValueError("questions 必须是非空数组")
                if len(questions) > 200:
                    raise ValueError("单次最多查询 200 道题目的缓存")
                results: list[dict[str, Any]] = []
                missing_exercise_ids: list[str] = []
                seen_ids: set[str] = set()
                for question in questions:
                    if not isinstance(question, dict):
                        raise ValueError("题目必须是对象")
                    exercise_id = str(question.get("exercise_id", "")).strip()
                    if not exercise_id or exercise_id in seen_ids:
                        raise ValueError("批量题目 ID 不能为空或重复")
                    seen_ids.add(exercise_id)
                    cached = self.server.state.cached_result(question)
                    if cached:
                        cached["cache_hit"] = True
                        results.append(cached)
                    else:
                        missing_exercise_ids.append(exercise_id)
                self._send_json(HTTPStatus.OK, {
                    "results": results,
                    "missing_exercise_ids": missing_exercise_ids,
                })
            elif self.path == "/api/v1/cache/classifications/delete":
                exercise_ids = payload.get("exercise_ids")
                if not isinstance(exercise_ids, list) or not exercise_ids:
                    raise ValueError("exercise_ids 必须是非空数组")
                if len(exercise_ids) > 200:
                    raise ValueError("单次最多清除 200 道题目的缓存")
                normalized_ids = [str(item).strip() for item in exercise_ids]
                if any(not item or len(item) > 120 for item in normalized_ids):
                    raise ValueError("题目 ID 无效")
                deleted = self.server.state.cache.delete_by_exercise_ids(normalized_ids)
                self._send_json(HTTPStatus.OK, {"deleted": deleted, "exercise_ids": normalized_ids})
            elif self.path == "/api/v1/batches":
                questions = payload.get("questions")
                if not isinstance(questions, list): raise ValueError("questions 必须是数组")
                if not self.server.state.cloud: raise ValueError("尚未启用 cloud_hybrid 分类器")
                profile = self.server.state.active_cloud_profile()
                metadata = self.server.state.batches.create(
                    self.server.state.cloud.create_batch_jsonl(questions, self.server.state.taxonomy, self.server.state.rules),
                    questions, cloud_profile_id=str(profile.get("id", "")),
                )
                self._send_json(HTTPStatus.CREATED, metadata)
            elif self.path == "/api/v1/batches/submit":
                job_id = str(payload.get("job_id", "")); local_job = self.server.state.batches.get(job_id); cloud = self.server.state.cloud_for_batch(local_job)
                provider_job = cloud.submit_batch(self.server.state.batches.input_bytes(job_id))
                self.server.state.batches.update(job_id, status=str(provider_job.get("status", "submitted")), provider_batch_id=str(provider_job["id"]))
                self._send_json(HTTPStatus.OK, self.server.state.batches.get(job_id))
            elif self.path == "/api/v1/batches/refresh":
                job_id = str(payload.get("job_id", "")); local_job = self.server.state.batches.get(job_id); cloud = self.server.state.cloud_for_batch(local_job)
                provider_job = cloud.get_batch(str(local_job.get("provider_batch_id", "")))
                status = str(provider_job.get("status", "unknown"));
                if status == "completed":
                    self.server.state.batches.save_result(job_id, cloud.get_batch_result_content(provider_job))
                else: self.server.state.batches.update(job_id, status=status)
                self._send_json(HTTPStatus.OK, self.server.state.batches.get(job_id))
            elif self.path == "/api/v1/batches/import":
                job_id = str(payload.get("job_id", "")); job = self.server.state.batches.get(job_id)
                if job.get("status") != "completed" or not job.get("result_file"): raise ValueError("批处理尚未完成，不能导入")
                cloud = self.server.state.cloud_for_batch(job)
                by_id = {str(item["exercise_id"]): item for item in job.get("questions", [])}
                imported, failed = 0, []
                for line in Path(str(job["result_file"])).read_text(encoding="utf-8").splitlines():
                    record = json.loads(line); custom_id, decision = cloud.batch_result_decision(record) if cloud else ("", None)
                    exercise_id = custom_id[len("exercise-"):].rsplit("-", 1)[0] if custom_id.startswith("exercise-") and "-" in custom_id[len("exercise-"):] else ""
                    question = by_id.get(exercise_id)
                    if not question or decision is None:
                        failed.append(exercise_id or custom_id); continue
                    result = validate_model_decision(question, decision, self.server.state.taxonomy, self.server.state.rules)
                    result["rule_version"] = self.server.state.rule_version; result["cache_hit"] = False
                    self.server.state.attach_model_input_snapshot(question, result)
                    self.server.state.cache_result(question, result)
                    self.server.state.proposals.record(question, result); imported += 1
                self._send_json(HTTPStatus.OK, {"job_id": job_id, "imported": imported, "failed_exercise_ids": failed})
            elif self.path.endswith("/batch"):
                questions = payload.get("questions")
                if not isinstance(questions, list):
                    raise ValueError("questions 必须是数组")
                results = self._classify_fast_batch(questions)
                self._send_json(HTTPStatus.OK, {"results": results})
            else:
                self._send_json(HTTPStatus.OK, self._classify_one(payload))
        except CloudProviderError as error:
            log_cloud_error("HTTP 分类请求失败", error)
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request", "message": public_cloud_error_message(error)})
        except (ValueError, TaxonomyError) as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request", "message": str(error)})
        except Exception as error:
            # 未预期异常必须留下完整堆栈，便于定位不同题目触发的边界；不得记录请求正文或密钥。
            self.log_error("分类处理失败：%s", type(error).__name__)
            traceback.print_exc(file=sys.stderr)
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"})

    def _classify_one(self, question: Any) -> dict[str, Any]:
        if not isinstance(question, dict):
            raise ValueError("题目必须是对象")
        cached = self.server.state.cached_result(question)
        if cached:
            cached["cache_hit"] = True
            return cached
        if self.server.state.cloud and self.server.state.cloud.configured:
            decision = self.server.state.cloud.classify(question, self.server.state.taxonomy, self.server.state.rules)
            result = validate_model_decision(question, decision, self.server.state.taxonomy, self.server.state.rules)
        else:
            result = classify(question, self.server.state.taxonomy, self.server.state.rules)
        result["rule_version"] = self.server.state.rule_version
        result["cache_hit"] = False
        self.server.state.attach_model_input_snapshot(question, result)
        self.server.state.cache_result(question, result)
        self.server.state.proposals.record(question, result)
        return result

    def _classify_batch_one(self, question: Any) -> dict[str, Any]:
        """隔离单题云端失败，避免一题限流使整页结果丢失。"""
        try:
            return self._classify_one(question)
        except CloudProviderError as error:
            log_cloud_error("单题云端分类失败", error)
            exercise_id = str(question.get("exercise_id", "")) if isinstance(question, dict) else ""
            return {
                "exercise_id": exercise_id,
                "taxonomy_version": self.server.state.taxonomy.version,
                "rule_version": self.server.state.rule_version,
                "status": "review",
                "needs_review": True,
                "review_reasons": ["cloud_request_failed"],
                "classification_method": "model",
                "confidence": 0.0,
                "reason": public_cloud_error_message(error),
                "target": None,
                "proposal_required": False,
                "proposal_cluster_id": None,
            }

    def _classify_fast_batch(self, questions: list[Any]) -> list[dict[str, Any]]:
        """以一次云端调用处理一个小分块，并逐题执行本地缓存和 Skill 冲突校验。"""
        if not questions or len(questions) > 10:
            raise ValueError("快速批量分类每次必须包含 1-10 道题")
        normalized: list[dict[str, Any]] = []
        results_by_id: dict[str, dict[str, Any]] = {}
        uncached: list[dict[str, Any]] = []
        exercise_ids: set[str] = set()
        for question in questions:
            if not isinstance(question, dict):
                raise ValueError("题目必须是对象")
            exercise_id = str(question.get("exercise_id", "")).strip()
            if not exercise_id or exercise_id in exercise_ids:
                raise ValueError("批量题目 ID 不能为空或重复")
            exercise_ids.add(exercise_id)
            normalized.append(question)
            cached = self.server.state.cached_result(question)
            if cached:
                cached["cache_hit"] = True
                results_by_id[exercise_id] = cached
            else:
                uncached.append(question)

        if uncached and self.server.state.cloud and self.server.state.cloud.configured:
            try:
                decisions = self.server.state.cloud.classify_fast_batch(
                    uncached, self.server.state.taxonomy, self.server.state.rules
                )
            except CloudProviderError as error:
                log_cloud_error("快速批量云端分类失败", error)
                for question in uncached:
                    exercise_id = str(question["exercise_id"])
                    result = self._cloud_review_result(exercise_id, error)
                    self.server.state.attach_model_input_snapshot(question, result)
                    results_by_id[exercise_id] = result
            else:
                for question, decision in zip(uncached, decisions, strict=True):
                    result = validate_model_decision(
                        question, decision, self.server.state.taxonomy, self.server.state.rules
                    )
                    result["rule_version"] = self.server.state.rule_version
                    result["cache_hit"] = False
                    self.server.state.attach_model_input_snapshot(question, result)
                    exercise_id = str(question["exercise_id"])
                    self.server.state.cache_result(question, result)
                    self.server.state.proposals.record(question, result)
                    results_by_id[exercise_id] = result
        elif uncached:
            for question in uncached:
                result = classify(question, self.server.state.taxonomy, self.server.state.rules)
                result["rule_version"] = self.server.state.rule_version
                result["cache_hit"] = False
                self.server.state.attach_model_input_snapshot(question, result)
                exercise_id = str(question["exercise_id"])
                self.server.state.cache_result(question, result)
                results_by_id[exercise_id] = result
        return [results_by_id[str(question["exercise_id"])] for question in normalized]

    def _cloud_review_result(self, exercise_id: str, reason: str) -> dict[str, Any]:
        return self.server.state._cloud_review_result(exercise_id, reason)

    def _require_cloud(self) -> OpenAIChatCompletionsProvider:
        cloud = self.server.state.cloud
        if not cloud or not cloud.configured: raise ValueError("云端模型未配置；请设置 cloud.api_key_env 指向的环境变量")
        return cloud

    def _read_json(self, maximum_length: int = 2_000_000) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if not raw_length:
            raise ValueError("缺少 Content-Length")
        length = int(raw_length)
        if length > maximum_length:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("JSON 根节点必须是对象")
        return parsed

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # 浏览器超时、刷新页面或关闭标签页后，写回结果会失败；无需再尝试发送 500。
            return


class CurationServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], state: ServiceState) -> None:
        self.state = state
        super().__init__(address, RequestHandler)


def main() -> int:
    parser = argparse.ArgumentParser(description="题湖题库分类助手本地服务")
    default_config = PROJECT_ROOT / "config" / "settings.local.yaml"
    if not default_config.exists(): default_config = PROJECT_ROOT / "config" / "settings.example.yaml"
    parser.add_argument("--config", type=Path, default=default_config)
    args = parser.parse_args()
    state = ServiceState(args.config)
    host = str(state.settings.get("host", "127.0.0.1"))
    if host != "127.0.0.1":
        raise ValueError("服务只允许绑定 127.0.0.1")
    port = int(state.settings.get("port", 3232))
    server = CurationServer((host, port), state)
    print(f"题湖分类服务已启动：http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
        state.close()


if __name__ == "__main__":
    raise SystemExit(main())
