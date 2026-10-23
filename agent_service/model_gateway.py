"""Model and provider boundary shared by all generation modes."""
from __future__ import annotations

from . import agents


def configuration():
    return agents.configuration()


def make_model(*args, **kwargs):
    return agents.make_model(*args, **kwargs)


def reasoning_settings(*args, **kwargs):
    return agents.reasoning_settings(*args, **kwargs)


def make_agent(*args, **kwargs):
    return agents.make_agent(*args, **kwargs)


def health():
    return agents.health()


async def reference_budget():
    return await agents.reference_budget()


def timeout_setting(name, default):
    return agents.timeout_setting(name, default)


async def generate(request, progress):
    return await agents.generate(request, progress)
