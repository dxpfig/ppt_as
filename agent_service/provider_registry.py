"""Read-only provider configuration exposed by the service boundary."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import os
import shutil

from . import agents, knowledge


@dataclass(frozen=True)
class ProviderState:
    kind: str
    provider: str
    configured: bool
    model: str = ''
    detail: str = ''


def model_provider() -> ProviderState:
    provider, token, model = agents.configuration()
    return ProviderState(
        kind='model',
        provider=provider,
        configured=bool(token and model),
        model=model or '',
    )


def vision_provider(model: ProviderState | None = None) -> ProviderState:
    model = model or model_provider()
    provider = os.getenv('PPT_VISION_PROVIDER') or model.provider
    configured = bool(
        os.getenv('PPT_VISION_API_KEY')
        or os.getenv('OPENAI_API_KEY')
        or os.getenv('ANTHROPIC_API_KEY')
        or os.getenv('ANTHROPIC_AUTH_TOKEN')
    )
    return ProviderState(
        kind='vision',
        provider=provider,
        configured=configured,
        model=os.getenv('PPT_VISION_MODEL')
        or (model.model if provider == model.provider else ''),
    )


def knowledge_provider() -> ProviderState:
    ids = [
        value.strip()
        for value in os.getenv('WEKNORA_KNOWLEDGE_BASE_IDS', '').split(',')
        if value.strip()
    ]
    return ProviderState(
        kind='knowledge',
        provider='weknora',
        configured=knowledge.configured(),
        detail=(
            f'{len(ids)} configured knowledge bases'
            if ids
            else 'knowledge bases discovered from the workspace'
        ),
    )


def renderer_provider() -> ProviderState:
    chromium = os.getenv('PPTD_CHROMIUM', '')
    available = bool(shutil.which('node') and (chromium or shutil.which('chromium') or shutil.which('google-chrome')))
    return ProviderState(
        kind='renderer',
        provider='chromium',
        configured=available,
        detail=os.getenv('PPT_RENDERER_IMAGE', 'local runtime'),
    )


def snapshot() -> dict:
    model = model_provider()
    providers = [model, vision_provider(model), knowledge_provider(), renderer_provider()]
    return {
        'providers': [asdict(item) for item in providers],
        'tenantConfigured': bool(os.getenv('WEKNORA_TENANT_ID')),
    }
