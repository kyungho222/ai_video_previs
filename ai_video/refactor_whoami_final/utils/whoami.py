"""Shared model configuration and model APIs for independent services.

Model definitions live in models.json/policies.json; keys stay in the environment.
Runtime snapshots load locally; background checks fetch changed DB versions.
When runtime.json is absent, create it from the configured model-registry DB.
Set AI_CONFIG_DIR for the runtime directory and MODEL_REGISTRY_REFRESH_SECONDS
for the check interval (30 seconds by default, 0 disables background checks).
"""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import partial, wraps
from importlib import import_module
import json
import logging
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from threading import RLock, Thread
from time import monotonic
from urllib.parse import urlsplit


WHOAMI_VERSION = "2026-09-24.1"
logger = logging.getLogger(__name__)
_PROJECT_CONFIG_DIR = Path(__file__).resolve().parent
if __package__ or _PROJECT_CONFIG_DIR.name == "utils":
    _PROJECT_CONFIG_DIR = _PROJECT_CONFIG_DIR.parent
_PROJECT_CONFIG_DIR /= "json_config"
# External API providers and the LangChain SDK used for each one.
_SDK_CLASSES = {
    "ChatOpenAI": ("langchain_openai", "ChatOpenAI"),
    "ChatAnthropic": ("langchain_anthropic", "ChatAnthropic"),
    "ChatDeepSeek": ("langchain_deepseek", "ChatDeepSeek"),
    "ChatGoogleGenerativeAI": ("langchain_google_genai", "ChatGoogleGenerativeAI"),
    "ChatXAI": ("langchain_xai", "ChatXAI"),
    "ChatMistralAI": ("langchain_mistralai", "ChatMistralAI"),
    "ChatClovaX": ("langchain_naver", "ChatClovaX"),
    "ChatQwen": ("langchain_qwq", "ChatQwen"),
    "ChatUpstage": ("langchain_upstage", "ChatUpstage"),
}
_PROVIDER_EXPORTS = frozenset({
    "ChatKimiK2", "ChatKimiK3", "ChatExaone", "ChatDoubaoSeed", "ChatDoubaoPro",
    "ChatDoubaoSeed20", "ChatGLM", "ChatLlama", "ChatOpenRouter",
})
ADAPTERS = frozenset(_SDK_CLASSES) | _PROVIDER_EXPORTS | {"LocalModel", "SLLMModel"}
_CREDENTIAL_ENV = {
    "openai_default": ("OPENAI_API_KEY",),
    "openai_asadal": ("OPENAI_ASADAL_API_KEY",),
    "openai_second": ("OPENAI_SECOND_API_KEY",),
    "openai_asadal_media": ("OPENAI_CHATTY_API_KEY", "OPENAI_ASADAL_API_KEY"),
    "moonshot": ("MOONSHOT_API_KEY",),
    "friendli": ("FRIENDLIAI_API_TOKEN",),
    "doubao": ("DOUBAO_API_KEY",),
    "glm": ("GLM_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "fireworks": ("FIREWORKS_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
}
_SECRET_FIELDS = {
    "api_key", "apikey", "authorization", "password", "secret",
    "access_token", "api_token", "credentials", "token", "private_key",
}
# Routing belongs to the model entry, not the SDK request options.
_ROUTING_OPTIONS = {
    "model", "model_name", "base_url", "openai_api_base",
    "endpoint", "endpoint_env", "api_url",
    "anthropic_api_url", "client_options",
}


def get_credential(profile):
    """Resolve only registered environment variables; never cache their values."""
    names = _CREDENTIAL_ENV[profile]
    return next((os.environ[name] for name in names if os.environ.get(name)), None)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate AI configuration key: {key}")
        result[key] = value
    return result


def _read_json(path, *, check_schema=True):
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid AI configuration JSON: {path.name} line={exc.lineno}") from None
    if check_schema and (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 2
    ):
        raise ValueError(f"Unsupported AI configuration schema: {path.name}")
    _validate_json_values(value)
    return value


def _require(condition, message):
    if not condition:
        raise ValueError(f"Invalid AI configuration: {message}")


def _validate_json_values(value):
    if isinstance(value, dict):
        for key, item in value.items():
            field = key.lower().replace("-", "_")
            _require(
                field not in _SECRET_FIELDS and not field.endswith("_api_key"),
                "credentials must be stored in environment variables",
            )
            _validate_json_values(item)
    elif isinstance(value, list):
        for item in value:
            _validate_json_values(item)
    elif isinstance(value, float):
        _require(math.isfinite(value), "non-finite numeric value")


def _validate_url(value, label):
    try:
        parsed = urlsplit(value) if isinstance(value, str) else None
        valid = (
            parsed
            and parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        valid = False
    _require(valid, f"invalid URL: {label} (HTTP(S), no embedded credentials)")


def _valid_selection_screen_id(value):
    return (isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value)
            and value not in {"all", "selected"})


def selection_screens(section):
    """Read screen data only from configuration; missing or empty means no screens."""
    screens = section.get("selection_screens", {})
    _require(isinstance(screens, dict) and all(
        _valid_selection_screen_id(key) and isinstance(label, str)
        and bool(label.strip()) and len(label) <= 80
        for key, label in screens.items()
    ), "invalid image selection screens")
    return deepcopy(screens)


def presentation_html_models(section):
    """Validate presentation HTML catalog shape; admission resolves live chat settings."""
    catalog = section.get("presentation_html_models", {})
    _require(isinstance(catalog, dict) and not set(catalog) - {"models", "default_model"},
             "invalid presentation HTML models")
    models = catalog.get("models", {})
    _require(isinstance(models, dict) and all(
        isinstance(key, str) and bool(key.strip()) and len(key) <= 255
        and isinstance(label, str) and bool(label.strip()) and len(label) <= 255
        for key, label in models.items()
    ), "invalid presentation HTML models")
    default = catalog.get("default_model")
    _require(default is None or isinstance(default, str) and default in models,
             "invalid presentation HTML default model")
    return {"models": deepcopy(models), "default_model": default}


def _validate_settings(settings):
    """Protect credentials/routing; leave ordinary options to their consumer."""
    if "selection_groups" in settings:
        groups = settings["selection_groups"]
        _require(isinstance(groups, list) and all(
            (group == "selected" or _valid_selection_screen_id(group)) for group in groups
        ), "invalid image selection groups")
    for field in ("credential_env", "endpoint_env"):
        if field in settings:
            name = settings[field]
            _require(isinstance(name, str) and re.fullmatch(r"[A-Z][A-Z0-9_]*", name),
                     f"invalid {field} environment name")
    for key in ("credential_profile", "profile"):
        if key in settings:
            _require(settings[key] in _CREDENTIAL_ENV, "unknown credential profile")
    for key in ("endpoint", "base_url", "api_url", "health_base_url", "openai_base_url"):
        if key in settings:
            _validate_url(settings[key], key)
    options = settings.get("options", {})
    _require(not _ROUTING_OPTIONS.intersection(options), "routing fields belong outside options")


def _merge_settings(*layers):
    """Merge JSON dictionaries; lists/scalars replace, and callers own the copy."""
    result = {}
    for layer in layers:
        for key, value in layer.items():
            previous = result.get(key)
            if isinstance(value, dict):
                result[key] = _merge_settings(
                    previous if isinstance(previous, dict) else {}, value,
                )
            else:
                result[key] = deepcopy(value)
    return result


def _read_config_documents(directory):
    directory = Path(directory).resolve()
    models = _read_json(directory / "models.json")
    policies = _read_json(directory / "policies.json")
    version = _check_documents({"models": models, "policies": policies})
    return directory, version, models, policies


def _check_documents(documents):
    models, policies = documents["models"], documents["policies"]
    for document in (models, policies):
        _require(isinstance(document, dict) and type(document.get("schema_version")) is int
                 and document["schema_version"] == 2,
                 "unsupported document schema")
        _validate_json_values(document)
    selection_screens(models.get("imagegen", {}))
    presentation_html_models(models.get("imagegen", {}))
    version = models["config_version"]
    _require(
        version and version == policies["config_version"],
        "models/policies config_version mismatch",
    )
    return version


def _model_settings(section, name, *, allow_disabled=False):
    row = section["models"][name]
    settings = _merge_settings(
        section.get("defaults", {}),
        section.get("providers", {}).get(row["provider"], {}),
        {"model": name},
        row,
    )
    _require(allow_disabled or settings.get("enabled", True), f"disabled model: {name}")
    return settings


def _validate_tenant_groups(groups, names):
    for name in names:
        values = groups.get(name, [])
        _require(
            isinstance(values, list)
            and all(isinstance(item, str) and item for item in values),
            "invalid tenant group",
        )


class ModelConfig:
    """One model type: copied defaults + provider options + model overrides."""

    def __init__(self, directory, kind, *, _documents=None):
        self.directory, self.version, models, policies = _documents or _read_config_documents(directory)
        self._section = models[kind]
        if kind == "imagegen":
            selection_screens(self._section)
            presentation_html_models(self._section)
        self._models = self._section["models"]
        self._aliases = self._section.get("aliases", {})
        self._policies = policies
        for name in self._models:
            row = self.model(name, allow_disabled=True)
            _require(type(row.get("enabled", True)) is bool, f"invalid enabled flag: {name}")
            _validate_settings(row)
            if row.get("cloud_config"):
                _validate_settings(row["cloud_config"])

    def model(self, name, *, allow_disabled=False):
        return _model_settings(self._section, name, allow_disabled=allow_disabled)

    def policy(self, name):
        return deepcopy(self._policies[name])

    def setting(self, name, default=None):
        """Read type-owned defaults without exposing the cached document."""
        return deepcopy(self._section.get(name, default))

    def resolve_model(self, name, *, allow_default=True, allow_null=False):
        if not isinstance(name, str):
            raise ValueError("Model name must be a string")
        if name in self._models:
            self.model(name)  # A disabled entry cannot escape through an alias.
        target = self._aliases.get(name)
        rule = ({"model": target} if isinstance(target, str) else target) if target is not None else None
        if rule is None and name in self._models:
            rule = {"model": name}
        if rule is None:
            key = next((key for key, row in self._models.items()
                        if row.get("model", key) == name), None)
            if key is not None:
                rule = {"model": key}
        if rule is None:
            for pattern, target in self._aliases.items():
                # Legacy prefix aliases also name a selector (e.g. gpt-5.4).
                # They no longer authorize arbitrary, unregistered suffixes.
                matches = name == pattern.rstrip("*")
                if matches:
                    rule = {"model": target} if isinstance(target, str) else target
                    break
        if rule is not None:
            row = self.model(rule["model"])
            # A compatibility alias may select a registered model, but never
            # turn the input into an arbitrary provider model ID.
            row["_resolved_name"] = name
            if allow_null:
                row["_model_key"] = rule["model"]
            api_model = row["model"]
            if not api_model and not (allow_null and api_model is None):
                normalized = name.replace("qwen-3", "qwen3") if rule.get("normalizer") == "qwen3" else name
                if normalized not in self._models:
                    raise ValueError(f"No registered model matches: {name}")
                row = self.model(normalized)
                row["_resolved_name"] = name
                api_model = row["model"]
                if not api_model:
                    raise ValueError(f"No registered model matches: {name}")
            returned = rule.get("return_name", row.get("return_name", "$model"))
            result_name = {"$input": name, "$model": api_model}.get(returned, returned)
            return row, api_model, result_name
        raise ValueError(f"No registered model matches: {name}")



class ChatConfig(ModelConfig):
    """Chat models, aliases, local routing, and text-model policies."""

    def __init__(self, directory, *, _documents=None):
        super().__init__(directory, "chat", _documents=_documents)
        policies = self._policies
        self._aliases = self._section["aliases"]
        for name in ("tenant_groups", "apply_user_ids", "providers", "tasks"):
            policies.setdefault(name, {})
        policies.setdefault("setting_aliases", [])
        chat_policy_names = (
            "defaults", "setting_aliases", "tenant_groups", "apply_user_ids",
            "local", "providers", "tasks",
        )
        self._policies = {
            name: policies[name] for name in chat_policy_names if name in policies
        }
        self._policies["tenant_groups"] = {
            name: values for name, values in policies["tenant_groups"].items()
            if name in {"asadal", "second", "factory_second"}
        }

        _require(self._models, "models must not be empty")
        for name in self._models:
            row = self.model(name, allow_disabled=True)
            _require(row["provider"] in ADAPTERS, f"unsupported provider: {name}")
            _require(
                row["provider"] != "LocalModel" or "base_url" in row,
                f"local base_url: {name}",
            )

        for pattern, target in self._aliases.items():
            _require(pattern and "*" not in pattern[:-1], "invalid alias pattern")
            alias_model = target if isinstance(target, str) else target["model"]
            _require(alias_model in self._models, "unknown alias target")

        for name in ("setting_model", "factory_model"):
            self.resolve_model(policies["defaults"][name], allow_default=False)
        _validate_tenant_groups(
            policies["tenant_groups"], ("asadal", "second", "factory_second"),
        )
        for row in policies["providers"].values():
            _validate_settings(row)
        _validate_settings(policies.get("local", {}))

    def _models_for(self, adapter):
        for name, raw in self._models.items():
            if raw["provider"] == adapter:
                row = self.model(name, allow_disabled=True)
                if row.get("enabled", True):
                    yield name, row

    def local_models(self):
        return {
            name: {"model": row["model"], "base_url": row["base_url"]}
            for name, row in self._models_for("LocalModel")
        }

    def runpod_models(self):
        return {name: row["model"] for name, row in self._models_for("SLLMModel")}

    def setting_model(self, name):
        return next(
            (row["model"] for row in self._policies["setting_aliases"] if row["value"] == name),
            name,
        )

    def key_profile(self, db_name, scope="helper"):
        groups = self._policies["tenant_groups"]
        if scope == "factory":
            name, second = db_name, "factory_second"
        elif scope == "helper":
            name, second = (db_name or "").strip(), "second"
        else:
            raise ValueError(f"Unknown credential selection scope: {scope}")
        if name in groups.get("asadal", []):
            return "openai_asadal"
        return "openai_second" if name in groups.get(second, []) else "openai_default"


class ImageConfig(ModelConfig):
    """Image model settings and image-specific credential selection."""

    def __init__(self, directory, *, _documents=None):
        super().__init__(directory, "image", _documents=_documents)
        policies = self._policies
        policies.setdefault("tenant_groups", {})
        policies.setdefault("image_credentials", {})
        self._policies = {
            "image_credentials": policies["image_credentials"],
            "tenant_groups": {
                name: values for name, values in policies["tenant_groups"].items()
                if name in {"image_asadal", "image_second"}
            },
        }

        _validate_tenant_groups(
            policies["tenant_groups"], ("image_asadal", "image_second"),
        )
        for row in policies["image_credentials"].values():
            _validate_settings(row)

    def credential_override(self, model_name):
        return deepcopy(self._policies["image_credentials"].get(model_name))

    def key_profile(self, db_name):
        name = (db_name or "").strip().lower()
        groups = self._policies["tenant_groups"]
        if name in groups.get("image_asadal", []):
            return "openai_asadal_media"
        return "openai_second" if name in groups.get("image_second", []) else "openai_default"


# Existing chat callers can migrate from these compatibility names gradually.
AIConfig = ChatConfig


_CONFIG_CACHE = {}
_CONFIG_LOCK = RLock()
_PUBLICATION_LOCK = RLock()
_REQUEST_CONFIG = ContextVar("model_config_snapshot", default=None)
_DB_REFRESH_NEXT_CHECK = {}
_DB_REFRESH_PENDING = set()


def _reset_model_config_after_fork():
    """Child workers must not inherit a vanished refresh thread or its locks."""
    global _CONFIG_LOCK, _PUBLICATION_LOCK
    _CONFIG_LOCK = RLock()
    _PUBLICATION_LOCK = RLock()
    _DB_REFRESH_NEXT_CHECK.clear()
    _DB_REFRESH_PENDING.clear()
    _REQUEST_CONFIG.set(None)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_model_config_after_fork)


# Registry conversion is shared here so consumers only need whoami and whoami_db.
MODEL_TABLE = "chatbot_model_config"

COMMON_TABLE = "chatbot_model_config_common"

MODEL_KINDS = {
    "chat": "채팅", "image": "이미지 편집", "imagegen": "이미지 생성",
    "video": "영상 생성", "music": "음악 생성",
    "stt": "음성 인식", "tts": "음성 합성", "realtime": "실시간 음성", "ocr": "OCR",
}

POLICY_SCOPES = {"tasks": "task", "providers": "provider_policy",
                 "tenant_groups": "tenant_group", "image_credentials": "image_credential"}

def common_identity(row):
    return row["kind"], row["scope"], row["config_key"]

def common_path(row):
    """Translate explicit type-owned settings to the existing runtime format."""
    kind, scope, key = common_identity(row)
    if (not isinstance(kind, str) or not isinstance(scope, str) or not isinstance(key, str)
            or len(key) > 255 or any(ord(c) < 32 for c in key)):
        raise ValueError("Invalid common configuration identity")
    if scope == "metadata" and kind == "global" and key in {"models", "policies"}:
        return key, []
    if scope == "extra" and kind == "global" and key and key not in {*MODEL_KINDS, "schema_version", "config_version"}:
        return "models", [key]
    if kind not in MODEL_KINDS:
        raise ValueError("Invalid common configuration type")
    if scope in {"defaults", "aliases"} and key == "":
        return "models", [kind, scope]
    if scope == "provider" and key:
        return "models", [kind, "providers", key]
    if scope == "setting" and key and key not in {"models", "defaults", "aliases"}:
        return "models", [kind] if key == "__root__" else [kind, key]
    if scope == "policy" and key and key not in {"schema_version", "config_version"}:
        expected_kind = "image" if key == "image_credentials" else "chat"
        if kind == expected_kind:
            return "policies", [key]
    for group, group_scope in POLICY_SCOPES.items():
        if scope != group_scope or not key:
            continue
        expected_kind = "image" if group == "image_credentials" or (group == "tenant_groups" and key.startswith("image_")) else "chat"
        if kind == expected_kind:
            return "policies", [group, key]
    raise ValueError("Invalid common configuration scope or ownership")

def assemble_sections(rows):
    """Reconstruct the caller-compatible runtime snapshot from typed records."""
    documents = {"models": {}, "policies": {}}
    paths = {key: [] for key in documents}
    metadata = set()
    ordered = []
    for row in rows:
        document_type, path = common_path(row)
        if type(row["order_ai"]) is not int or row["order_ai"] < 0 or type(row["revision"]) is not int or row["revision"] < 1:
            raise ValueError("Invalid common configuration version/order")
        ordered.append((document_type, row["order_ai"], common_identity(row), path, row))
    for document_type, _, _, path, row in sorted(ordered, key=lambda entry: entry[:3]):
        content = deepcopy(row["content"])
        _validate_json_values(content)
        if row["scope"] == "metadata":
            if document_type in metadata or not isinstance(content, dict) or set(content) != {"schema_version", "config_version"}:
                raise ValueError("Invalid configuration metadata")
            metadata.add(document_type)
            documents[document_type].update(content)
            continue
        for previous in paths[document_type]:
            size = min(len(path), len(previous))
            if path[:size] == previous[:size]:
                raise ValueError("Duplicate or overlapping common settings")
        paths[document_type].append(path)
        target = documents[document_type]
        for part in path[:-1]:
            target = target.setdefault(part, {})
        target[path[-1]] = content
    if metadata != {"models", "policies"}:
        raise ValueError("Configuration metadata is missing")
    return documents

def json_text(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

def validate_policy_sections(policies, chat_config):
    """Validate references exposed by the policy editor using the runtime resolver."""
    groups = policies.get("tenant_groups", {})
    if not isinstance(groups, dict) or any(
        not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values)
        for values in groups.values()
    ):
        raise ValueError("Invalid tenant group")
    aliases = policies.get("setting_aliases", [])
    if not isinstance(aliases, list):
        raise ValueError("Setting aliases must be a list")
    for alias in aliases:
        if not isinstance(alias, dict) or not isinstance(alias.get("value"), str) or not alias["value"]:
            raise ValueError("Invalid setting alias")
        chat_config.resolve_model(alias.get("model"), allow_default=False)
    tasks = policies.get("tasks", {})
    if not isinstance(tasks, dict):
        raise ValueError("Tasks must be an object")
    for task in tasks.values():
        if not isinstance(task, dict):
            raise ValueError("Task settings must be an object")
        _validate_settings(task)
        for field in ("cloud_model", "fallback_model"):
            if field in task:
                chat_config.resolve_model(task[field], allow_default=False)
        if "fallback_plan" in task:
            plan = task["fallback_plan"]
            if not isinstance(plan, list) or not plan:
                raise ValueError("Fallback plan must be a nonempty list")
            for step in plan:
                if not isinstance(step, list) or len(step) != 3 or type(step[1]) is not int or step[1] < 1 or type(step[2]) not in (int, float) or step[2] <= 0:
                    raise ValueError("Invalid fallback step")
                chat_config.resolve_model(step[0], allow_default=False)

def validate_documents(models, policies):
    """Reuse the runtime's validation without importing the app or any SDK."""
    _validate_json_values(models)
    _validate_json_values(policies)
    selection_screens(models.get("imagegen", {}))
    presentation_html_models(models.get("imagegen", {}))
    for kind in MODEL_KINDS:
        section = models.get(kind, {})
        if not isinstance(section, dict) or not isinstance(section.get("providers", {}), dict):
            raise ValueError(f"Invalid common configuration: {kind}")
        for layer in (section.get("defaults", {}), *section.get("providers", {}).values()):
            if not isinstance(layer, dict):
                raise ValueError(f"Invalid common settings: {kind}")
            if "enabled" in layer:
                raise ValueError(f"enabled belongs to the model, not common settings: {kind}")
    with tempfile.TemporaryDirectory(prefix="chatty-registry-validation-") as directory:
        for name, value in (("models", models), ("policies", policies)):
            Path(directory, name + ".json").write_text(json_text(value), encoding="utf-8")
        chat_config = ChatConfig(directory)
        ImageConfig(directory)
    validate_policy_sections(policies, chat_config)
    for kind, section in models.items():
        if isinstance(section, dict) and "models" in section and kind not in MODEL_KINDS:
            raise ValueError(f"Unsupported model kind: {kind}")
    for kind in MODEL_KINDS:
        if kind not in models or kind in {"chat", "image"}:
            continue
        section = models[kind]
        if not isinstance(section, dict) or not isinstance(section.get("models"), dict):
            raise ValueError(f"Invalid model section: {kind}")
        providers = section.get("providers", {})
        if not isinstance(providers, dict):
            raise ValueError(f"Invalid providers: {kind}")
        for layer in (section.get("defaults", {}), *providers.values()):
            if not isinstance(layer, dict):
                raise ValueError(f"Invalid common settings: {kind}")
            if "enabled" in layer:
                raise ValueError(f"enabled belongs to the model, not common settings: {kind}")
            if "options" in layer and not isinstance(layer["options"], dict):
                raise ValueError(f"Invalid common options: {kind}")
            _validate_settings(layer)
        for key, raw in section["models"].items():
            if not isinstance(raw, dict) or raw.get("provider") not in providers:
                raise ValueError(f"Unsupported {kind} provider: {key}")
            _validate_settings(raw)
            if "options" in raw and not isinstance(raw["options"], dict):
                raise ValueError(f"Invalid options: {kind}/{key}")
            if type(raw.get("enabled", True)) is not bool:
                raise ValueError(f"Invalid enabled flag: {kind}/{key}")
        aliases = section.get("aliases", {})
        if not isinstance(aliases, dict) or any(
            not isinstance(alias, str) or not alias or any(c in alias for c in "*?[]")
            or not isinstance(target, str) or target not in section["models"]
            for alias, target in aliases.items()
        ):
            raise ValueError(f"Invalid aliases: {kind}")
        tasks = section.get("task_defaults", {})
        if not isinstance(tasks, dict) or any(not isinstance(v, dict) for v in tasks.values()):
            raise ValueError(f"Invalid task defaults: {kind}")
        for task in tasks.values():
            if "options" in task and not isinstance(task["options"], dict):
                raise ValueError(f"Invalid task options: {kind}")
            _validate_settings(task)
        defaults = [task.get("model") for task in tasks.values()]
        if "default_model" in section:
            defaults.append(section["default_model"])
        for key in defaults:
            if not isinstance(key, str) or key not in section["models"] or not section["models"][key].get("enabled", True):
                raise ValueError(f"Missing or disabled default model: {kind}/{key}")

def validate_record(row):
    if not isinstance(row, dict) or row.get("kind") not in MODEL_KINDS:
        raise ValueError("Invalid model kind")
    for key, limit in (("model_key", 255), ("name", 255), ("provider", 64)):
        value = row.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
            raise ValueError(f"Invalid model {key}")
    api_model = row.get("api_model")
    if api_model is not None and (
        not isinstance(api_model, str) or len(api_model) > 255 or "\x00" in api_model
    ):
        raise ValueError("Invalid API model")
    if type(row.get("enabled")) is not bool:
        raise ValueError("enabled must be a boolean")
    if type(row.get("order_ai")) is not int or not -(2**31) <= row["order_ai"] < 2**31:
        raise ValueError("Invalid sort order")
    if type(row.get("revision")) is not int or row["revision"] < 1:
        raise ValueError("Invalid model revision")
    settings = row.get("settings")
    if not isinstance(settings, dict):
        raise ValueError("Model settings must be an object")
    if "provider" in settings or "enabled" in settings:
        raise ValueError("Provider and enabled belong to their dedicated columns")
    if "model" in settings and (settings["model"] is not None or api_model is not None):
        raise ValueError("API model belongs to its dedicated column")
    if "options" in settings and not isinstance(settings["options"], dict):
        raise ValueError("options must be an object")
    _validate_json_values(settings)
    _validate_settings(settings)
    if settings.get("selection_groups") and (
        row["kind"] != "imagegen" or row["provider"] not in {"openai_image", "google_image"}
    ):
        raise ValueError("Image selection groups require an OpenAI or Google image generation model")
    json_text(settings)
    if row["kind"] == "chat" and row["provider"] not in ADAPTERS:
        raise ValueError("Unsupported chat provider")

def export_documents(rows, documents):
    exported = deepcopy(documents)
    for kind in MODEL_KINDS:
        if kind in exported["models"]:
            exported["models"][kind]["models"] = {}
    seen = set()
    for row in sorted(rows, key=lambda r: (r["kind"], r["order_ai"], r["model_key"])):
        validate_record(row)
        if row["kind"] not in exported["models"]:
            raise ValueError(f"Missing configuration section: {row['kind']}")
        identity = row["kind"], row["model_key"]
        if identity in seen:
            raise ValueError("Duplicate registry model")
        seen.add(identity)
        config = {"provider": row["provider"], **deepcopy(row["settings"])}
        if row["api_model"] is not None:
            config["model"] = row["api_model"]
        if not row["enabled"]:
            config["enabled"] = False
        exported["models"][row["kind"]]["models"][row["model_key"]] = config
    validate_documents(exported["models"], exported["policies"])
    return exported


def _model_registry_connection():
    """Resolve the same registry connection for bootstrap and version checks."""
    import pymysql

    names = {"host": "MARIA_DB_HOST", "port": "MARIA_DB_PORT",
             "user": "MARIA_DB_USER", "password": "MARIA_DB_PASSWORD"}
    options = {key: os.environ.get(name) for key, name in names.items()}
    if any(value is None for value in options.values()):
        try:
            config = import_module("config").Config
        except (ModuleNotFoundError, AttributeError):
            raise ValueError("Missing MariaDB configuration; set MARIA_DB_* environment variables") from None
        for key, name in names.items():
            if options[key] is None:
                options[key] = getattr(config, name, None)
    if any(value is None for value in options.values()):
        raise ValueError("Missing MariaDB configuration; set MARIA_DB_* environment variables")
    options["port"] = int(options["port"])
    options.update(database=os.environ.get("MODEL_REGISTRY_DB", "chatty"), charset="utf8mb4",
                   connect_timeout=5, read_timeout=15, write_timeout=15, autocommit=False)
    return pymysql.connect(**options)


def _load_model_config_version_from_db():
    """Read only the two metadata rows that the admin API versions together."""
    import pymysql

    with _model_registry_connection() as connection:
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            try:
                cursor.execute(f"SELECT config_key,content_json FROM {COMMON_TABLE} WHERE kind='global' AND scope='metadata' AND config_key IN ('models','policies')")
                rows = cursor.fetchall()
            finally:
                connection.rollback()
    documents = {row["config_key"]: json.loads(row["content_json"], object_pairs_hook=_unique_object)
                 for row in rows}
    _require(len(rows) == 2 and set(documents) == {"models", "policies"}, "registry metadata is missing")
    version = _check_documents(documents)
    _require(isinstance(version, str) and version, "invalid registry configuration version")
    return version


def _load_model_config_from_db():
    """Read one registry snapshot without modifying tables or requiring seed JSON."""
    import pymysql

    with _model_registry_connection() as connection:
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            connection.begin()
            try:
                cursor.execute(f"SELECT kind,scope,config_key,content_json,order_ai,revision FROM {COMMON_TABLE}")
                sections = cursor.fetchall()
                cursor.execute(f"SELECT kind,model_key,name,provider,api_model,enabled,settings_json,order_ai,revision FROM {MODEL_TABLE} ORDER BY kind,order_ai,model_key")
                rows = cursor.fetchall()
            finally:
                connection.rollback()
    for section in sections:
        section["content"] = json.loads(section.pop("content_json"))
    for row in rows:
        row["settings"] = json.loads(row.pop("settings_json"))
        if row["enabled"] not in (0, 1):
            raise ValueError("Invalid registry enabled flag")
        row["enabled"] = bool(row["enabled"])
    return export_documents(rows, assemble_sections(sections))


def model_config_directory():
    return Path(os.environ.get("AI_CONFIG_DIR") or _PROJECT_CONFIG_DIR).resolve()


def _typed_configs(directory, documents):
    version = _check_documents(documents)
    loaded = (directory, version, documents["models"], documents["policies"])
    configs = {}
    for kind, section in documents["models"].items():
        if not isinstance(section, dict) or "models" not in section:
            continue
        cls = {"chat": ChatConfig, "image": ImageConfig}.get(kind)
        configs[kind] = (cls(directory, _documents=loaded) if cls else
                         ModelConfig(directory, kind, _documents=loaded))
    _require({"chat", "image"} <= configs.keys(), "missing chat/image settings")
    return configs


def _source_stamp(directory):
    active = directory / "runtime.json"
    paths = [active] if active.exists() else [directory / "models.json", directory / "policies.json"]
    return tuple((str(path), stat.st_ino, stat.st_mtime_ns, stat.st_size)
                 for path in paths for stat in [path.stat()])


def _refresh_model_config(directory, expected_version, interval):
    """Fetch outside the request lock; never replace an advanced local snapshot."""
    try:
        if _load_model_config_version_from_db() != expected_version:
            documents = _load_model_config_from_db()
            with model_publication_lock(directory):
                active = _read_json(directory / "runtime.json", check_schema=False)
                if _check_documents(active) == expected_version:
                    version = _publish_model_config(documents, directory)
                    logger.info("[whoami] registry refresh applied directory=%s version=%s", directory, version)
    except Exception as error:
        logger.warning("[whoami] registry refresh failed; keeping runtime directory=%s error=%s",
                       directory, type(error).__name__)
    finally:
        with _CONFIG_LOCK:
            _DB_REFRESH_PENDING.discard(directory)
            _DB_REFRESH_NEXT_CHECK[directory] = monotonic() + interval


def _schedule_model_config_refresh(directory, version):
    """Called under _CONFIG_LOCK: at most one nonblocking check per directory."""
    interval = float(os.environ.get("MODEL_REGISTRY_REFRESH_SECONDS", "30"))
    _require(math.isfinite(interval) and interval >= 0, "invalid MODEL_REGISTRY_REFRESH_SECONDS")
    if interval == 0:
        return
    now = monotonic()
    if directory not in _DB_REFRESH_NEXT_CHECK:
        _DB_REFRESH_NEXT_CHECK[directory] = now + interval
    if now < _DB_REFRESH_NEXT_CHECK[directory] or directory in _DB_REFRESH_PENDING:
        return
    _DB_REFRESH_NEXT_CHECK[directory] = now + interval
    _DB_REFRESH_PENDING.add(directory)
    try:
        Thread(target=_refresh_model_config, args=(directory, version, interval), daemon=True).start()
    except Exception as error:
        _DB_REFRESH_PENDING.discard(directory)
        logger.warning("[whoami] registry refresh could not start directory=%s error=%s",
                       directory, type(error).__name__)


def _current_configs(directory):
    with _CONFIG_LOCK:
        active = directory / "runtime.json"
        if not active.exists():
            directory.mkdir(parents=True, exist_ok=True)
            with model_publication_lock(directory):
                if not active.exists():
                    _publish_model_config(_load_model_config_from_db(), directory)
        previous = _CONFIG_CACHE.get(directory)
        stamp = None
        try:
            stamp = _source_stamp(directory)
            if previous and stamp in (previous[0], previous[2]):
                _schedule_model_config_refresh(directory, previous[1]["chat"].version)
                return previous[1]
            documents = _read_json(active, check_schema=False)
            configs = _typed_configs(directory, documents)
            _require(stamp == _source_stamp(directory), "settings changed during loading")
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError) as error:
            if previous is None:
                raise
            if stamp != previous[2]:
                logger.warning("[whoami] keeping last valid settings directory=%s error=%s",
                               directory, type(error).__name__)
            _CONFIG_CACHE[directory] = (previous[0], previous[1], stamp)
            _schedule_model_config_refresh(directory, previous[1]["chat"].version)
            return previous[1]
        _CONFIG_CACHE[directory] = (stamp, configs, None)
        logger.info("[whoami] applied settings directory=%s version=%s", directory, configs["chat"].version)
        _schedule_model_config_refresh(directory, configs["chat"].version)
        return configs


def get_model_config(kind="chat", directory=None):
    """Get settings by type; new requests observe validated file publications."""
    directory = Path(directory).resolve() if directory is not None else model_config_directory()
    active = _REQUEST_CONFIG.get()
    configs = active[1] if active and active[0] == directory else _current_configs(directory)
    return configs[kind]


def get_media_model_config(kind, name=None, *, task=None, default_model=None, provider=None):
    """Resolve copied media settings; SDK operations may deliberately omit an API model.

    Selection: explicit model, task model, type default, compatibility default.
    Credentials remain environment names; adapters own transport-specific auth.
    """
    with model_config_snapshot():
        config = get_model_config(kind)
        task_settings = config.setting("task_defaults", {}).get(task, {}) if task else {}
        selector = name or task_settings.get("model") or config.setting("default_model") or default_model
        _require(isinstance(selector, str) and bool(selector), f"no {kind} model configured")
        settings, _, _ = config.resolve_model(selector, allow_null=True)
        if provider is not None:
            allowed = (provider,) if isinstance(provider, str) else provider
            _require(settings["provider"] in allowed, f"unsupported {kind} provider: {settings['provider']}")
        settings["options"] = _merge_settings(settings.get("options", {}), task_settings.get("options", {}))
        return settings


def get_http_model_config(kind, name=None, *, provider, default_timeout=60, task=None, default_model=None):
    """Resolve one HTTP model; returned headers must remain request-local."""
    settings = get_media_model_config(kind, name, task=task, default_model=default_model, provider=provider)
    endpoint = settings.get("endpoint")
    _validate_url(endpoint, "model endpoint")
    parsed = urlsplit(endpoint)
    _require(not parsed.query and not parsed.fragment, "model endpoint must not contain query or fragment")
    credential_env = settings.get("credential_env")
    api_key = os.getenv(credential_env) if credential_env else None
    _require(bool(api_key), f"credential environment unavailable: {credential_env}")
    options = settings.get("options", {})
    timeout = options.pop("request_timeout", options.pop("timeout", default_timeout))
    _require(type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0,
             "request timeout must be a positive finite number")
    options.pop("max_retries", None)  # Generation POSTs must not be retried implicitly.
    return {**settings, "options": options, "request_timeout": timeout,
            "headers": {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}}


@contextmanager
def model_config_snapshot(directory=None):
    """Pin one version through a request, including its awaits and fallbacks."""
    directory = Path(directory).resolve() if directory is not None else model_config_directory()
    active = _REQUEST_CONFIG.get()
    if active and active[0] == directory:
        yield active[1]
        return
    token = _REQUEST_CONFIG.set((directory, _current_configs(directory)))
    try:
        yield _REQUEST_CONFIG.get()[1]
    finally:
        _REQUEST_CONFIG.reset(token)


class ModelConfigMiddleware:
    """Optional ASGI integration: keep one version through an HTTP stream."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # Registry initialization must remain available before its first snapshot.
        if scope['type'] == 'http' and not scope.get('path', '').startswith('/admin/model-registry/'):
            with model_config_snapshot():
                await self.app(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def model_config_operation(function):
    """Keep standalone model construction on one snapshot, like HTTP requests."""
    if asyncio.iscoroutinefunction(function):
        @wraps(function)
        async def call(*args, **kwargs):
            with model_config_snapshot():
                return await function(*args, **kwargs)
    else:
        @wraps(function)
        def call(*args, **kwargs):
            with model_config_snapshot():
                return function(*args, **kwargs)
    return call


@contextmanager
def model_publication_lock(directory):
    """Serialize administrator commit/publication across processes on this mount."""
    import fcntl
    with _PUBLICATION_LOCK, (Path(directory) / ".runtime.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _publish_model_config(documents, directory):
    """Caller holds model_publication_lock; a single replace publishes the pair."""
    directory = Path(directory).resolve()
    configs = _typed_configs(directory, deepcopy(documents))
    serialized = json.dumps(documents, ensure_ascii=False, allow_nan=False, indent=2)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                         prefix=".runtime-", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            reference = directory / "runtime.json"
            if not reference.exists():
                reference = directory / "models.json"
            mode = stat.S_IMODE(reference.stat().st_mode) if reference.exists() else 0o640
            os.fchmod(output.fileno(), mode)
            output.write(serialized + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, directory / "runtime.json")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return configs["chat"].version


def publish_model_config(documents, directory=None):
    directory = Path(directory).resolve() if directory is not None else model_config_directory()
    with model_publication_lock(directory):
        return _publish_model_config(documents, directory)


def load_config(directory):
    """Compatibility loader for a validated chat configuration snapshot."""
    return get_model_config("chat", directory)


def get_chat_config():
    """Return the project's chat configuration snapshot."""
    return get_model_config("chat")


def get_image_config():
    """Return the project's image configuration snapshot."""
    return get_model_config("image")


def get_ai_config():
    """Compatibility name for the project's chat configuration."""
    return get_chat_config()


def main(config_loader=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Validate AI JSON files without loading the app or calling providers.",
    )
    parser.add_argument("directory", type=Path)
    parser.add_argument("--apply", action="store_true", help="Publish validated files without restarting workers.")
    args = parser.parse_args()
    try:
        checked = (config_loader or ChatConfig)(args.directory)
        ImageConfig(args.directory)
        if args.apply:
            _, _, models, policies = _read_config_documents(args.directory)
            publish_model_config({"models": models, "policies": policies}, args.directory)
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        parser.error(str(exc))
    print(json.dumps({
        "directory": str(checked.directory),
        "version": checked.version,
        "models": len(checked._models),
    }))


def extract_text_content(content):
    """Return display text without mutating or stringifying native payloads."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") in ("text", "output_text"):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


class TextOnlyStreamMixin:
    """Normalize Anthropic/Mistral message blocks for text-only consumers."""

    def stream(self, *args, **kwargs):
        for chunk in super().stream(*args, **kwargs):
            chunk.content = extract_text_content(chunk.content)
            yield chunk

    async def astream(self, *args, **kwargs):
        async for chunk in super().astream(*args, **kwargs):
            chunk.content = extract_text_content(chunk.content)
            yield chunk

    def invoke(self, *args, **kwargs):
        result = super().invoke(*args, **kwargs)
        result.content = extract_text_content(result.content)
        return result

    async def ainvoke(self, *args, **kwargs):
        result = await super().ainvoke(*args, **kwargs)
        result.content = extract_text_content(result.content)
        return result


def sdk_class(adapter, *, text_only=True):
    module, name = _SDK_CLASSES[adapter]
    cls = getattr(import_module(module), name)
    if adapter in ("ChatAnthropic", "ChatMistralAI") and text_only:
        return type(f"TextOnly{name}", (TextOnlyStreamMixin, cls), {})
    return cls


def openai_chat_kwargs(
    model_name, temperature=0, api_key=None, db_name=None, chat_bot_id=None,
    *, use_prompt_cache=True,
):
    result = {"model_name": model_name, "api_key": api_key, "temperature": temperature}
    if use_prompt_cache:
        scope = (db_name or "default").strip() or "default"
        result["model_kwargs"] = {"prompt_cache_key": f"{scope}:{chat_bot_id}:{model_name}"}
    return result


def anthropic_chat_kwargs(model_name, temperature=0):
    result = {"model_name": model_name, "stream_usage": True}
    if temperature is not None:
        result["temperature"] = temperature
    return result


_API_ACCOUNT_PROFILES = {}


def _api_account_profile(db_name):
    """Cache account routing for one minute; never cache credential values."""
    name = (db_name or "").strip()
    if not name:
        return None
    cached = _API_ACCOUNT_PROFILES.get(name)
    if cached is not None and monotonic() < cached[0]:
        return cached[1]
    profile = None
    try:
        with _model_registry_connection() as connection:
            with connection.cursor() as cursor:
                try:
                    cursor.execute(
                        "SELECT api_account_server FROM chatty.chatty_apply_list "
                        "WHERE user_id = %s LIMIT 1", (name,),
                    )
                    row = cursor.fetchone()
                finally:
                    connection.rollback()
        if row:
            profile = {1: "openai_default", 2: "openai_second", 3: "openai_asadal"}.get(row[0])
            if row[0] is not None and profile is None:
                logger.warning("[API ACCOUNT] invalid server db_name=%s server=%s", name, row[0])
    except Exception as error:
        logger.warning("[API ACCOUNT] lookup failed; using tenant policy db_name=%s error=%s",
                       name, type(error).__name__)
    _API_ACCOUNT_PROFILES[name] = (monotonic() + 60, profile)
    return profile


def get_openai_api_key_for_db(db_name, log_prefix="[OPENAI API KEY]", *, scope="helper"):
    account_profile = _api_account_profile(db_name)
    profile = account_profile or get_chat_config().key_profile(db_name, scope)
    key = get_credential(profile)
    if account_profile and not key:
        raise ValueError(f"Missing API account credential: {profile}")
    logger.info("%s profile=%s db_name=%s configured=%s", log_prefix, profile, db_name, bool(key))
    return key


def _request_options(settings, temperature, request_options):
    return {
        "temperature": temperature,
        **deepcopy(request_options),
        **deepcopy(settings.get("options", {})),
    }


def _apply_omissions(settings, kwargs):
    for field in set(settings.get("omit", [])) - _ROUTING_OPTIONS - _SECRET_FIELDS:
        kwargs.pop(field, None)
    return kwargs


def _external_chat_kwargs(
    settings, api_model, temperature, db_name, chat_bot_id,
    *, key_scope="factory", log_prefix="[OPENAI API KEY]",
    use_prompt_cache=True, **request_options,
):
    """Use the selected external SDK's model, key, and request options."""
    kwargs = _request_options(settings, temperature, request_options)
    provider = settings["provider"]

    if provider == "ChatAnthropic" and kwargs.get("temperature") is None:
        kwargs.pop("temperature", None)

    if provider == "ChatOpenAI":
        api_key = None
        if not settings.get("credential_profile") and not settings.get("credential_env"):
            api_key = get_openai_api_key_for_db(db_name, log_prefix, scope=key_scope)
        routing = openai_chat_kwargs(
            api_model, None, api_key, db_name, chat_bot_id,
            use_prompt_cache=bool(settings.get("prompt_cache")) and use_prompt_cache,
        )
        routing.pop("temperature")
        kwargs.update(routing)
    elif provider in {"ChatAnthropic", "ChatDeepSeek"}:
        kwargs["model_name"] = api_model
    else:
        kwargs["model"] = api_model

    if settings.get("base_url"):
        kwargs["base_url"] = settings["base_url"]
    if settings.get("credential_profile"):
        kwargs["api_key"] = get_credential(settings["credential_profile"])
    if settings.get("credential_env"):
        name = settings["credential_env"]
        key = os.getenv(name)
        if not key:
            raise ValueError(f"Missing credential environment: {name}")
        kwargs["api_key"] = key
    return _apply_omissions(settings, kwargs)


@model_config_operation
def _create_openai_compatible_model(adapter, model=None, **kwargs):
    config = get_chat_config()
    provider = config.policy("providers").get(adapter, {})
    name = model or provider.get("model")
    if not name:
        raise ValueError(f"Missing registered model for provider: {adapter}")
    settings, api_model, _ = config.resolve_model(name)
    if settings["provider"] != adapter:
        raise ValueError(f"Not a {adapter} model: {name}")
    if not provider:
        # A registered model may own its connection through provider common options.
        kwargs = _external_chat_kwargs(
            settings, api_model, kwargs.pop("temperature", 0), None, None,
            use_prompt_cache=False, **kwargs,
        )
        if not kwargs.get("base_url") or not kwargs.get("api_key"):
            raise ValueError(
                f"Missing provider connection settings: {adapter}; "
                "configure base_url and credential_env or credential_profile"
            )
        return sdk_class("ChatOpenAI")(**kwargs)
    key = get_credential(provider["credential_profile"])
    if not key:
        raise ValueError(f"Missing credential profile: {provider['credential_profile']}")
    kwargs["api_key"] = key
    kwargs.setdefault("base_url", provider["base_url"])
    return sdk_class("ChatOpenAI")(model=api_model, **kwargs)


def _local_settings(config, model_name):
    settings, api_model, _ = config.resolve_model(model_name, allow_default=False)
    if settings["provider"] != "LocalModel":
        raise ValueError(f"Not a local model: {model_name}")
    return settings, api_model


async def _get_json(url, timeout):
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
            response.raise_for_status()
            return await response.json()


async def _probe_health_check(config, timeout):
    try:
        url = config.policy("local")["health_base_url"].rstrip("/")
        data = await _get_json(f"{url}/health", timeout)
        return data.get("status") == "ok"
    except Exception:
        return False


async def _probe_first_model(config, timeout, model_name=None):
    try:
        url = (
            _local_settings(config, model_name)[0]["base_url"]
            if model_name else config.policy("local")["openai_base_url"]
        )
        data = await _get_json(f"{url.rstrip('/')}/models", timeout)
        models = data.get("data", [])
        if model_name:
            expected = _local_settings(config, model_name)[1]
            return next((row["id"] for row in models if row.get("id") == expected), None)
        return models[0].get("id") if models else None
    except Exception:
        return None


async def _probe_local_llm(config, timeout, model_name=None):
    async def probe():
        if model_name or await _probe_health_check(config, timeout):
            return await _probe_first_model(config, timeout, model_name)
        return None
    try:
        return await asyncio.wait_for(probe(), timeout=timeout)
    except Exception:
        return None


def _create_chat_model(
    config, settings, api_model, temperature=0, db_name=None, chat_bot_id=None,
    *, use_prompt_cache=True, key_scope="factory", log_prefix="[OPENAI API KEY]",
):
    """Construct the selected model through one provider dispatch."""
    provider = settings["provider"]
    if provider == "LocalModel":
        return LocalModel(model_name=settings["_resolved_name"], temperature=temperature)
    if provider == "SLLMModel":
        return SLLMModel(model_name=settings["_resolved_name"], temperature=temperature)
    kwargs = _external_chat_kwargs(
        settings, api_model, temperature, db_name, chat_bot_id,
        use_prompt_cache=use_prompt_cache, key_scope=key_scope, log_prefix=log_prefix,
    )
    if provider in config.policy("providers") or provider in _PROVIDER_EXPORTS:
        return _create_openai_compatible_model(provider, **kwargs)
    if provider in _SDK_CLASSES:
        return sdk_class(provider)(**kwargs)
    raise ValueError(f"Unsupported chat provider: {provider}")


# Keep the existing public call and tuple result.
@model_config_operation
async def get_model_by_name(
    model_name, temperature=0, db_name=None, chat_bot_id=None,
    *, use_prompt_cache=True,
):
    config = get_chat_config()
    settings, api_model, returned_name = config.resolve_model(model_name)
    return _create_chat_model(
        config, settings, api_model, temperature, db_name, chat_bot_id,
        use_prompt_cache=use_prompt_cache,
    ), returned_name


@model_config_operation
def build_openai_chat_model(
    model_name,
    temperature=0,
    db_name=None,
    chat_bot_id=None,
    log_prefix="[OPENAI API KEY]",
    *,
    use_prompt_cache=True,
):
    config = get_chat_config()
    settings, api_model, _ = config.resolve_model(model_name)
    if settings["provider"] != "ChatOpenAI":
        raise ValueError(f"Not an OpenAI model: {model_name}")
    return _create_chat_model(
        config, settings, api_model, temperature, db_name, chat_bot_id,
        key_scope="helper", log_prefix=log_prefix, use_prompt_cache=use_prompt_cache,
    )


@model_config_operation
def build_anthropic_chat_model(model_name, temperature=0):
    config = get_chat_config()
    settings, api_model, _ = config.resolve_model(model_name)
    if settings["provider"] != "ChatAnthropic":
        raise ValueError(f"Not an Anthropic model: {model_name}")
    return _create_chat_model(config, settings, api_model, temperature)


@model_config_operation
def _registered_sdk_model(adapter, *, model=None, model_name=None, **kwargs):
    """Keep legacy constructor imports while validating each new client."""
    if model is not None and model_name is not None and model != model_name:
        raise ValueError("Conflicting model and model_name")
    settings, api_model, _ = get_chat_config().resolve_model(model_name or model)
    if settings["provider"] != adapter:
        raise ValueError(f"Not a {adapter} model: {model_name or model}")
    options = _external_chat_kwargs(settings, api_model, kwargs.pop("temperature", 0),
                                    None, None, use_prompt_cache=False, **kwargs)
    if "api_key" in kwargs and not (settings.get("credential_profile") or settings.get("credential_env")):
        options["api_key"] = kwargs["api_key"]
    return sdk_class(adapter, text_only=False)(**options)


@model_config_operation
def _registered_ollama_model(*, model, **kwargs):
    _, api_model = _local_settings(get_chat_config(), model)
    return getattr(import_module("langchain_ollama"), "OllamaLLM")(model=api_model, **kwargs)


@model_config_operation
def LocalModel(model_name, is_reasoning=None, **kwargs):
    settings, api_model = _local_settings(get_chat_config(), model_name)
    options = settings.setdefault("options", {})
    options["stream_usage"] = kwargs.pop("stream_usage", options.get("stream_usage", True))
    if is_reasoning is not None:
        extra_body = options.setdefault("extra_body", deepcopy(kwargs.get("extra_body", {})))
        extra_body.setdefault("chat_template_kwargs", {})["enable_thinking"] = is_reasoning
    model_kwargs = _request_options(settings, kwargs.pop("temperature", 0), kwargs)
    model_kwargs["model"] = api_model
    model_kwargs["base_url"] = settings["base_url"]
    model_kwargs["api_key"] = os.getenv("LOCAL_SLLM_API_KEY") or "LOCAL_EMPTY"
    model_kwargs = _apply_omissions(settings, model_kwargs)
    model_kwargs["metadata"] = {**(model_kwargs.get("metadata") or {}), "usage_provider": "local"}
    return sdk_class("ChatOpenAI")(**model_kwargs)


@model_config_operation
def SLLMModel(model_name, **kwargs):
    config = get_chat_config()
    settings, api_model, _ = config.resolve_model(model_name, allow_default=False)
    if settings["provider"] != "SLLMModel":
        raise ValueError(f"Not a RunPod model: {model_name}")
    model_kwargs = _request_options(settings, kwargs.pop("temperature", None), kwargs)
    url = get_runpod_url(api_model)
    if not url:
        raise ValueError(f"Runpod URL not found for model: {api_model}")
    model_kwargs["model"] = api_model
    model_kwargs["base_url"] = f"{url.rstrip('/')}/v1"
    model_kwargs["api_key"] = os.getenv("RUNPOD_API_KEY") or "EMPTY"
    model_kwargs = _apply_omissions(settings, model_kwargs)
    model_kwargs["metadata"] = {**(model_kwargs.get("metadata") or {}), "usage_provider": "local"}
    return sdk_class("ChatOpenAI")(**model_kwargs)


def is_local_sllm_model(model_name):
    try:
        _local_settings(get_chat_config(), model_name)
        return True
    except (KeyError, ValueError):
        return False


def resolve_local_sllm_model_id(model_name: str) -> str:
    config = get_chat_config()
    if not is_local_sllm_model(model_name):
        model_name = config.policy("local")["default_model"]
    return _local_settings(config, model_name)[1]


async def sllm_local_health_check(timeout=3.0):
    return await _probe_health_check(get_chat_config(), timeout)


async def sllm_local_get_first_model(timeout=3.0, model_name=None):
    return await _probe_first_model(get_chat_config(), timeout, model_name)


async def get_local_llm_or_none(timeout=3.0, model_name=None):
    return await _probe_local_llm(get_chat_config(), timeout, model_name)


def get_runpod_url(model_name):
    from runpod_manager.regist_runpod_id import get_url_sync
    return get_url_sync(model_name) or None


_LOCAL_EXPORTS = {
    "SLLM_LOCAL_BASE_URL": "health_base_url",
    "SLLM_LOCAL_OPENAI_BASE_URL": "openai_base_url",
    "LOCAL_LLM_REQUEST_TIMEOUT_SECONDS": "timeout",
    "IMAGE_DESCRIPTION_SLLM_TIMEOUT_SECONDS": "image_timeout",
    "LOCAL_LLM_MAX_RETRIES": "max_retries",
}


def __getattr__(name):
    """Forward legacy imports in one place without eager DB or SDK loading."""
    if name in {
        "get_chat_id_from_db", "get_upload_dir_from_db", "get_or_create_upload_dir",
        "get_or_create_upload_pdf_dir", "get_or_create_prompt_dir", "check_duplicate_contents",
        "get_model_name", "get_or_create_web_content_dir", "get_db_name_by_chatbot_id",
        "check_chatbot_id_valid", "CHATBOT_DB_CACHE_KEY_PREFIX", "CHATBOT_DB_CACHE_TTL",
    }:
        module = f"{__package__}.whoami_db" if __package__ else "whoami_db"
        return getattr(import_module(module), name)
    if name in _SDK_CLASSES:
        return partial(_registered_sdk_model, name)
    if name == "OllamaLLM":
        return _registered_ollama_model
    if name in _PROVIDER_EXPORTS:
        return partial(_create_openai_compatible_model, name)
    if name == "SLLM_LOCAL_API_KEY":
        return os.getenv("LOCAL_SLLM_API_KEY") or "LOCAL_EMPTY"
    if name in _LOCAL_EXPORTS:
        return get_chat_config().policy("local")[_LOCAL_EXPORTS[name]]
    if name in {
        "LOCAL_TEXT_MODEL_NAMES",
        "LOCAL_TEXT_PRIMARY_MODEL_NAME",
        "LOCAL_TEXT_FALLBACK_MODEL_NAME",
    }:
        names = get_chat_config().policy("local")["text_models"]
        if name == "LOCAL_TEXT_MODEL_NAMES":
            return tuple(names)
        if name == "LOCAL_TEXT_PRIMARY_MODEL_NAME":
            return names[0]
        return names[1]
    if name in {"LOCAL_MODEL_MAP", "SLLM_MODEL_MAP"}:
        config = get_chat_config()
        if name == "LOCAL_MODEL_MAP":
            return config.local_models()
        return config.runpod_models()
    if name in {"ASADAL_DB_NAME_LIST", "SECOND_DB_NAME_LIST"}:
        group = "asadal" if name == "ASADAL_DB_NAME_LIST" else "second"
        return get_chat_config().policy("tenant_groups").get(group, [])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    main()
