"""Read-only, offline checks for the model configuration used by generation."""
from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlparse

from application.ai.embedding_config_service import EmbeddingConfigService
from application.ai.llm_control_service import LLMControlService
from infrastructure.ai.embedding_environment import EmbeddingEnvironmentSettings
from infrastructure.ai.local_model_validation import local_model_is_complete, resolve_local_model_path
from infrastructure.ai.local_packages import get_local_packages_status


def _value(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _valid_url(value) -> bool:
    try:
        url = urlparse((value or "").strip())
        _ = url.port
        return (url.scheme in ("http", "https") and bool(url.hostname) and bool(url.netloc)
                and not any(char.isspace() for char in url.netloc))
    except (ValueError, AttributeError):
        return False


def _valid_key(value) -> bool:
    key = (value or "").strip()
    return bool(key) and key != "********" and not key.startswith("ppcred:")


class ModelReadinessService:
    def __init__(self, *, llm_control_service=None, embedding_config_service=None,
                 local_dependencies_status: Callable = get_local_packages_status,
                 local_model_validator: Callable = local_model_is_complete):
        self.llm = llm_control_service or LLMControlService()
        self.embedding = embedding_config_service or EmbeddingConfigService()
        self.local_dependencies_status = local_dependencies_status
        self.local_model_validator = local_model_validator

    def _profile_reasons(self, profile, label):
        if profile is None:
            return [f"{label}未配置"]
        profile = self.llm.resolve_profile(profile)
        reasons = []
        if not profile.model.strip():
            reasons.append(f"{label}缺少模型 ID")
        if not _valid_url(profile.base_url):
            reasons.append(f"{label}缺少有效服务地址")
        if profile.auth_mode != "none" and not _valid_key(profile.api_key):
            reasons.append(f"{label}缺少可用 API Key")
        try:
            self.llm._validate_profile_capabilities(profile)
        except ValueError:
            reasons.append(f"{label}的模型参数不受支持")
        return reasons

    def _llm_reasons(self):
        try:
            config = self.llm.get_config_readonly()
            active = self.llm.get_active_profile(config)
            reasons = self._profile_reasons(active, "当前激活模型")
            runtime = self.llm.get_runtime_summary(config)
            if (_value(runtime, "using_mock", False) or _value(runtime, "source") != "profile" or
                    _value(runtime, "status") != "configured"):
                reasons.append("当前模型运行时不可用")
            if config.endpoint_mode == "independent":
                for role in ("经济模型", "知识图谱模型"):
                    selected = next((item for item in config.profiles if item.name == role), None)
                    reasons.extend(self._profile_reasons(selected, role))
            return reasons
        except Exception:
            # Exceptions may include a provider URL, protected-reference or key.
            return ["无法读取有效模型配置，请检查模型设置与凭据"]

    def _embedding_result(self):
        mode = "local"
        try:
            config = self.embedding.get_config_readonly()
            mode = config.mode if config.mode in ("openai", "local") else "local"
            reasons = []
            if config.mode == "openai":
                env = EmbeddingEnvironmentSettings.from_env()
                if not config.model.strip():
                    reasons.append("云端嵌入缺少模型 ID")
                if not _valid_url(config.base_url or env.base_url):
                    reasons.append("云端嵌入缺少有效服务地址")
                if not _valid_key(config.api_key or env.api_key_with_openai_fallback):
                    reasons.append("云端嵌入缺少可用 API Key")
            elif config.mode == "local":
                dependencies = self.local_dependencies_status()
                if not dependencies.get("all_installed", False):
                    reasons.append("本地嵌入依赖正在验证，请稍候" if dependencies.get("checking")
                                   else "本地嵌入依赖尚未安装完整或无法加载")
                if not self.local_model_validator(resolve_local_model_path(config.model_path)):
                    reasons.append("本地嵌入模型未下载完整或路径无效")
            else:
                reasons.append("嵌入模式无效，请重新选择")
        except Exception:
            reasons = ["无法读取有效嵌入配置，请检查嵌入设置与凭据"]
        return {"ready": not reasons, "reasons": reasons, "mode": mode}

    def get_readiness(self):
        reasons = self._llm_reasons()
        llm = {"ready": not reasons, "reasons": reasons}
        embedding = self._embedding_result()
        return {"ready": llm["ready"] and embedding["ready"], "llm": llm, "embedding": embedding}
