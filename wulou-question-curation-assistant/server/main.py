"""题湖题库分类本地服务入口。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import threading
import traceback
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from collections import defaultdict, deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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


PROJECT_ROOT = Path(__file__).resolve().parent.parent


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
        taxonomy_path = resolve_path(self.config_path, str(self.settings["taxonomy_path"]))
        generated_taxonomy = PROJECT_ROOT / ".local-data" / "taxonomy.yaml"
        example_taxonomy = (PROJECT_ROOT / "config" / "taxonomy.example.yaml").resolve()
        # 首次保存云端设置会生成 settings.local.yaml 并沿用示例路径；此时应自动切换到已导出的真实目录。
        if self.config_path.name in {"settings.example.yaml", "settings.local.yaml"} and taxonomy_path == example_taxonomy and generated_taxonomy.exists():
            taxonomy_path = generated_taxonomy
        self.taxonomy = Taxonomy.from_file(taxonomy_path)
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
        if classifier_settings.get("mode") == "cloud_hybrid":
            self.cloud = OpenAIChatCompletionsProvider(classifier_settings.get("cloud") or {})
        batch_path = resolve_path(self.config_path, str((classifier_settings.get("batch") or {}).get("storage_path", "../.local-data/batches")))
        self.batches = BatchStore(batch_path)
        self.classification_jobs = ClassificationJobStore()
        proposal_minimum = int((self.rules.get("rules") or {}).get("proposal_minimum_distinct_questions", 3))
        self.proposals = ProposalStore(cache_path.with_name("directory-proposals.sqlite3"), proposal_minimum)

    def cache_key(self, question: dict[str, Any]) -> str:
        cloud_settings = (self.settings.get("classifier") or {}).get("cloud") or {}
        routing_model = str(cloud_settings.get("routing_model", "")).strip()
        directory_effort = str(cloud_settings.get("reasoning_effort", "high")).strip().lower()
        routing_effort = str(cloud_settings.get("routing_reasoning_effort", "medium")).strip().lower()
        audit_mode = self.audit_mode()
        source_catalogue_id = self.source_catalogue_id(question)
        material = "|".join([
            str(question.get("exercise_id", "")), source_catalogue_id, content_hash(question), self.taxonomy.version, self.rule_version,
            SNAPSHOT_VERSION,
            self.rule_hash,
            # 模型名相同时，更换 API 协议或提供方也必须重新生成结果。
            f"cloud:{self.cloud.provider_name}:{self.cloud.model}:directory:{directory_effort}:route:{routing_model or self.cloud.model}:{routing_effort}:audit:{audit_mode}" if self.cloud else "heuristic-v1",
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

    def cached_result(self, question: dict[str, Any]) -> dict[str, Any] | None:
        exercise_id = str(question["exercise_id"])
        source_catalogue_id = self.source_catalogue_id(question)
        cached = self.cache.get(self.cache_key(question), exercise_id, source_catalogue_id)
        manual = self.cache.get_manual_override(exercise_id, source_catalogue_id)
        if not manual:
            return cached
        # 人工采纳的路径优先于模型输出；即使模型缓存因规则更新失效，人工结果仍可恢复。
        result = dict(cached or {
            "exercise_id": exercise_id,
            "status": "suggested",
            "confidence": 1.0,
            "reason": "已采纳人工修正",
            "review_reasons": [],
        })
        result.update({
            "exercise_id": exercise_id,
            "status": "suggested",
            "confidence": 1.0,
            "reason": "已采纳人工修正",
            "review_reasons": [],
            "target": {"path": manual["target_path"]},
            "manual_override": {
                "source": "manual",
                "original_target_path": manual["original_target_path"],
                "accepted_at": manual["accepted_at"],
            },
        })
        return result

    def cache_result(self, question: dict[str, Any], result: dict[str, Any]) -> None:
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
            original_target_path=original_target_path,
            target_path=target_path,
        )
        return {
            "exercise_id": exercise_id,
            "source_catalogue_id": source_catalogue_id,
            "target_path": override["target_path"],
            "accepted_at": override["accepted_at"],
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

    def cloud_summary(self) -> dict[str, Any]:
        cloud_settings = (self.settings.get("classifier") or {}).get("cloud") or {}
        return {
            "mode": (self.settings.get("classifier") or {}).get("mode", "heuristic"),
            "model": cloud_settings.get("model", ""),
            "routing_model": cloud_settings.get("routing_model", ""),
            "reasoning_effort": cloud_settings.get("reasoning_effort", "high"),
            "routing_reasoning_effort": cloud_settings.get("routing_reasoning_effort", "medium"),
            "audit_mode": self.audit_mode(),
            "max_concurrent_requests": self.llm_concurrency(),
            "base_url": cloud_settings.get("base_url", "https://api.openai.com/v1"),
            "api_key_env": cloud_settings.get("api_key_env", "OPENAI_API_KEY"),
            "api_key_configured": bool(cloud_settings.get("api_key") or (self.cloud and self.cloud.configured)),
        }

    def update_cloud_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        existing_cloud = (self.settings.get("classifier") or {}).get("cloud") or {}
        model = str(payload.get("model", "")).strip()
        routing_model = str(payload.get("routing_model", "")).strip()
        reasoning_effort = str(payload.get("reasoning_effort", "high")).strip().lower()
        routing_reasoning_effort = str(payload.get("routing_reasoning_effort", "medium")).strip().lower()
        audit_mode = str(payload.get("audit_mode", "disabled")).strip().lower()
        try:
            max_concurrent_requests = int(payload.get("max_concurrent_requests", existing_cloud.get("max_concurrent_requests", 3)))
        except (TypeError, ValueError) as error:
            raise ValueError("LLM 并发数必须是 1 到 5") from error
        base_url = str(payload.get("base_url", "https://api.openai.com/v1")).strip().rstrip("/")
        api_key_env = str(payload.get("api_key_env", "OPENAI_API_KEY")).strip()
        if not model or len(model) > 160: raise ValueError("模型名不能为空，且长度不能超过 160")
        if len(routing_model) > 160: raise ValueError("专题路由模型名长度不能超过 160")
        allowed_efforts = {"none", "low", "medium", "high", "xhigh", "max"}
        if reasoning_effort not in allowed_efforts:
            raise ValueError("目录分类推理强度必须是 none、low、medium、high、xhigh 或 max")
        if routing_reasoning_effort not in allowed_efforts:
            raise ValueError("专题路由推理强度必须是 none、low、medium、high、xhigh 或 max")
        if audit_mode not in {"disabled", "conditional", "always"}:
            raise ValueError("审核策略必须是 disabled、conditional 或 always")
        if not 1 <= max_concurrent_requests <= 5:
            raise ValueError("LLM 并发数必须是 1 到 5")
        if not base_url.startswith(("https://", "http://")): raise ValueError("接口地址必须以 http:// 或 https:// 开头")
        if not api_key_env or len(api_key_env) > 120: raise ValueError("密钥环境变量名无效")
        classifier = self.settings.setdefault("classifier", {}); cloud = classifier.setdefault("cloud", {})
        classifier["mode"] = "cloud_hybrid"; cloud.update({
            "model": model, "base_url": base_url, "api_key_env": api_key_env,
            "reasoning_effort": reasoning_effort,
            "routing_reasoning_effort": routing_reasoning_effort,
            "audit_mode": audit_mode,
            "max_concurrent_requests": max_concurrent_requests,
        })
        if routing_model:
            cloud["routing_model"] = routing_model
        else:
            cloud.pop("routing_model", None)
        if payload.get("clear_api_key") is True: cloud.pop("api_key", None)
        elif "api_key" in payload:
            api_key = str(payload.get("api_key") or "").strip()
            if len(api_key) < 8: raise ValueError("API 密钥长度异常；如需清除请使用 clear_api_key")
            cloud["api_key"] = api_key
        target_config = self.config_path
        if target_config.name == "settings.example.yaml":
            target_config = target_config.with_name("settings.local.yaml")
        save_settings(target_config, self.settings)
        self.config_path = target_config
        self.cloud = OpenAIChatCompletionsProvider(cloud)
        return self.cloud_summary()

    def routing_cloud(self) -> OpenAIChatCompletionsProvider | None:
        """专题路由可单独配置模型和推理强度；留空模型时共用目录分类模型。"""
        if not self.cloud:
            return None
        cloud_settings = (self.settings.get("classifier") or {}).get("cloud") or {}
        routing_model = str(cloud_settings.get("routing_model", "")).strip() or self.cloud.model
        routing_effort = str(cloud_settings.get("routing_reasoning_effort", "medium")).strip().lower()
        directory_effort = str(cloud_settings.get("reasoning_effort", "high")).strip().lower()
        if routing_model == self.cloud.model and routing_effort == getattr(self.cloud, "reasoning_effort", directory_effort):
            return self.cloud
        routing_settings = dict(cloud_settings)
        routing_settings["model"] = routing_model
        routing_settings["reasoning_effort"] = routing_effort
        return OpenAIChatCompletionsProvider(routing_settings)

    def audit_mode(self) -> str:
        """默认只执行前两阶段；旧配置缺失时同样不额外产生第三次模型请求。"""
        cloud_settings = (self.settings.get("classifier") or {}).get("cloud") or {}
        mode = str(cloud_settings.get("audit_mode", "disabled")).strip().lower()
        return mode if mode in {"disabled", "conditional", "always"} else "disabled"

    def llm_concurrency(self) -> int:
        """限制整条实时流水线的总在途请求数，保护网关免受突发并发冲击。"""
        cloud_settings = (self.settings.get("classifier") or {}).get("cloud") or {}
        try:
            value = int(cloud_settings.get("max_concurrent_requests", 3))
        except (TypeError, ValueError):
            value = 3
        return max(1, min(5, value))

    def _requires_independent_audit(
        self, question: dict[str, Any], decision: dict[str, Any], target: Any
    ) -> bool:
        """只以 Skill 定义的高风险结构和 LLM 自检结果决定是否追加独立审核。"""
        if self.audit_mode() == "disabled":
            return False
        if self.audit_mode() == "always":
            return True
        self_check = decision.get("self_check") if isinstance(decision.get("self_check"), dict) else None
        if not self_check or self_check.get("passed") is not True:
            return True
        # 答案是辅助证据，单独缺失不必增加一轮审核；其余排版或题干风险交给独立审核判断。
        warnings = set(build_model_input_snapshot(question).get("warnings") or [])
        if warnings - {"answer_text_missing"}:
            return True
        core_start = int((self.rules.get("rules") or {}).get("large_question_core_topic_start_order", 10))
        # Skill 要求：前置专题检查最晚必备知识；后续专题【大题】检查核心/辅助关系。
        return target.topic_order < core_start or (
            target.topic_order >= core_start and str(target.level2_title) == "【大题】"
        )

    @staticmethod
    def _self_check_audit(decision: dict[str, Any]) -> dict[str, Any]:
        """未触发第三轮时保留第二轮自检证据，便于结果追溯。"""
        self_check = decision.get("self_check") if isinstance(decision.get("self_check"), dict) else {}
        return {
            "mode": "second_stage_self_check",
            "passed": self_check.get("passed") is True,
            "violations": list(self_check.get("violations") or []),
            "reason": str(self_check.get("reason") or "第二阶段自检通过，未触发独立审核"),
            "confidence": self_check.get("confidence"),
        }

    @staticmethod
    def _disabled_audit() -> dict[str, Any]:
        """显式标记用户关闭了第三阶段，避免将其误读成一次审核通过。"""
        return {
            "mode": "disabled",
            "passed": None,
            "violations": [],
            "reason": "独立审核已关闭；当前仅执行专题路由与目录分类自检",
            "confidence": None,
        }

    def create_classification_job(self, questions: Any) -> dict[str, Any]:
        """提交整页实时分类作业；HTTP 立刻返回，长推理在本机后台继续执行。"""
        if not isinstance(questions, list) or not questions:
            raise ValueError("questions 必须是非空数组")
        if len(questions) > 100:
            raise ValueError("单个分类作业最多处理 100 道题")
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
            audit_queue: deque[tuple[list[dict[str, Any]], list[dict[str, Any]], list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], Any]]]] = deque()
            total_limit = self.llm_concurrency()

            def finalize(batch: list[dict[str, Any]], decisions: list[dict[str, Any]]) -> None:
                for question, decision in zip(batch, decisions, strict=True):
                    result = validate_model_decision(question, decision, self.taxonomy, self.rules)
                    self._put_job_result(job_id, question, result)

            # 单个执行池对三阶段共享总上限。调度器在仍有路由任务时保留至少一个路由槽位，
            # 其余空闲槽优先给已经产生的分类与审核任务，避免三阶段的全页栅栏等待。
            with ThreadPoolExecutor(max_workers=total_limit) as executor:
                futures: dict[Any, tuple[str, Any]] = {}

                def schedule() -> None:
                    while len(futures) < total_limit:
                        active_route = any(kind == "routing" for kind, _payload in futures.values())
                        if route_queue and not active_route:
                            batch = route_queue.popleft()
                            future = executor.submit(routing_cloud.route_fast_batch, batch, self.taxonomy, self.rules)
                            futures[future] = ("routing", batch)
                        elif audit_queue:
                            batch, decisions, audit_items = audit_queue.popleft()
                            future = executor.submit(cloud.audit_batch, audit_items, self.taxonomy, self.rules)
                            futures[future] = ("auditing", (batch, decisions, audit_items))
                            self.classification_jobs.set_stage(job_id, "auditing", routed_count)
                        elif classify_queue:
                            topic_id, batch, routing_by_id = classify_queue.popleft()
                            future = executor.submit(
                                cloud.classify_topic_batch, batch, topic_id, self.taxonomy, self.rules, routing_by_id
                            )
                            futures[future] = ("classifying", (topic_id, batch))
                            self.classification_jobs.set_stage(job_id, "classifying", routed_count)
                        elif route_queue:
                            batch = route_queue.popleft()
                            future = executor.submit(routing_cloud.route_fast_batch, batch, self.taxonomy, self.rules)
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
                                    self._put_job_result(job_id, question, self._cloud_review_result(str(question["exercise_id"]), str(error)), failed=True)
                            else:
                                routed_groups: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
                                for question, routing in zip(batch, routings, strict=True):
                                    topic_id = str(routing.get("latest_topic_id") or "")
                                    if routing.get("status") != "routed" or not self.taxonomy.topic(topic_id):
                                        self._put_job_result(job_id, question, self._routing_review_result(question, routing))
                                    else:
                                        routed_groups[topic_id].append((question, routing))
                                for topic_id, pairs in routed_groups.items():
                                    batch_questions = [item[0] for item in pairs]
                                    routing_by_id = {str(item[0]["exercise_id"]): item[1] for item in pairs}
                                    classify_queue.append((topic_id, batch_questions, routing_by_id))
                            routed_count += len(batch)
                            self.classification_jobs.set_stage(job_id, "routing", routed_count)
                        elif kind == "classifying":
                            topic_id, batch = payload
                            try:
                                decisions = future.result()
                            except CloudProviderError as error:
                                for question in batch:
                                    self._put_job_result(job_id, question, self._cloud_review_result(str(question["exercise_id"]), str(error)), failed=True)
                            else:
                                audit_items: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], Any]] = []
                                for question, decision in zip(batch, decisions, strict=True):
                                    target_level3_id = decision.get("target_level3_id")
                                    target = self.taxonomy.global_target(
                                        str(target_level3_id), decision.get("target_level4_id")
                                    ) if target_level3_id else None
                                    if decision.get("status") == "suggested" and target and target.topic_id == topic_id:
                                        if self._requires_independent_audit(question, decision, target):
                                            routing = decision.get("routing") if isinstance(decision.get("routing"), dict) else {}
                                            audit_items.append((question, routing, decision, target))
                                        else:
                                            decision["audit"] = (
                                                self._disabled_audit() if self.audit_mode() == "disabled"
                                                else self._self_check_audit(decision)
                                            )
                                if audit_items:
                                    audit_queue.append((batch, decisions, audit_items))
                                else:
                                    finalize(batch, decisions)
                        else:
                            batch, decisions, audit_items = payload
                            try:
                                audits = future.result()
                            except CloudProviderError as error:
                                for _question, _routing, decision, _target in audit_items:
                                    decision["status"] = "review"
                                    decision["review_reasons"] = list(decision.get("review_reasons") or []) + ["audit_request_failed"]
                                    decision["reason"] = f"已生成候选，但独立审核未完成：{error}"
                                    decision["audit"] = None
                            else:
                                audit_by_id = {str(audit["exercise_id"]): audit for audit in audits}
                                for question, _routing, decision, _target in audit_items:
                                    decision["audit"] = audit_by_id[str(question["exercise_id"])]
                            finalize(batch, decisions)
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
        if self.path == "/api/v1/history/catalogue-moves":
            self._send_json(HTTPStatus.OK, self.server.state.cache.catalogue_move_report()); return
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
        if self.path not in {"/api/v1/classify", "/api/v1/classify/batch", "/api/v1/classification-jobs", "/api/v1/batches", "/api/v1/batches/submit", "/api/v1/batches/refresh", "/api/v1/batches/import", "/api/v1/settings/cloud", "/api/v1/cache/classifications/delete", "/api/v1/cache/classifications/lookup", "/api/v1/manual-classifications", "/api/v1/history/catalogue-moves"}:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        try:
            payload = self._read_json()
            if self.path == "/api/v1/settings/cloud":
                self._send_json(HTTPStatus.OK, self.server.state.update_cloud_settings(payload))
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
                metadata = self.server.state.batches.create(self.server.state.cloud.create_batch_jsonl(questions, self.server.state.taxonomy, self.server.state.rules), questions)
                self._send_json(HTTPStatus.CREATED, metadata)
            elif self.path == "/api/v1/batches/submit":
                job_id = str(payload.get("job_id", "")); cloud = self._require_cloud()
                provider_job = cloud.submit_batch(self.server.state.batches.input_bytes(job_id))
                self.server.state.batches.update(job_id, status=str(provider_job.get("status", "submitted")), provider_batch_id=str(provider_job["id"]))
                self._send_json(HTTPStatus.OK, self.server.state.batches.get(job_id))
            elif self.path == "/api/v1/batches/refresh":
                job_id = str(payload.get("job_id", "")); cloud = self._require_cloud(); local_job = self.server.state.batches.get(job_id)
                provider_job = cloud.get_batch(str(local_job.get("provider_batch_id", "")))
                status = str(provider_job.get("status", "unknown"));
                if status == "completed" and provider_job.get("output_file_id"):
                    self.server.state.batches.save_result(job_id, cloud.get_file_content(str(provider_job["output_file_id"])))
                else: self.server.state.batches.update(job_id, status=status)
                self._send_json(HTTPStatus.OK, self.server.state.batches.get(job_id))
            elif self.path == "/api/v1/batches/import":
                job_id = str(payload.get("job_id", "")); job = self.server.state.batches.get(job_id)
                if job.get("status") != "completed" or not job.get("result_file"): raise ValueError("批处理尚未完成，不能导入")
                by_id = {str(item["exercise_id"]): item for item in job.get("questions", [])}
                imported, failed = 0, []
                for line in Path(str(job["result_file"])).read_text(encoding="utf-8").splitlines():
                    record = json.loads(line); custom_id = str(record.get("custom_id", "")); exercise_id = custom_id.split("-", 2)[1] if custom_id.startswith("exercise-") else ""
                    question = by_id.get(exercise_id); body = ((record.get("response") or {}).get("body") or {})
                    if not question or int((record.get("response") or {}).get("status_code", 0)) >= 300:
                        failed.append(exercise_id or custom_id); continue
                    decision = self.server.state.cloud._decode(body) if self.server.state.cloud else None
                    result = validate_model_decision(question, decision or {}, self.server.state.taxonomy, self.server.state.rules)
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
        except (ValueError, TaxonomyError, CloudProviderError) as error:
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
            decision = self.server.state.cloud.classify(
                question, self.server.state.taxonomy, self.server.state.rules, self.server.state.audit_mode()
            )
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
                "reason": str(error),
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
                for question in uncached:
                    exercise_id = str(question["exercise_id"])
                    result = self._cloud_review_result(exercise_id, str(error))
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
        return {
            "exercise_id": exercise_id,
            "taxonomy_version": self.server.state.taxonomy.version,
            "rule_version": self.server.state.rule_version,
            "status": "review",
            "needs_review": True,
            "review_reasons": ["cloud_request_failed"],
            "classification_method": "model",
            "confidence": 0.0,
            "reason": reason,
            "target": None,
            "proposal_required": False,
            "proposal_cluster_id": None,
            "cache_hit": False,
        }

    def _require_cloud(self) -> OpenAIChatCompletionsProvider:
        cloud = self.server.state.cloud
        if not cloud or not cloud.configured: raise ValueError("云端模型未配置；请设置 cloud.api_key_env 指向的环境变量")
        return cloud

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if not raw_length:
            raise ValueError("缺少 Content-Length")
        length = int(raw_length)
        if length > 2_000_000:
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
