"""Worker adapter registry (GATE-D1-07 A3/A5).

The harness asks the registry for *an adapter*, never for a named vendor. The
vendor-neutral LiteLLM adapter is registered unconditionally and is always
preferred; the optional Claude adapter is imported **lazily** and only when
``EFAH_ENABLE_CLAUDE_ADAPTER`` is set, so that:

* the default build has no import edge to a vendor SDK at all, and
* deleting ``workers/adapters/claude_code.py`` outright leaves this module and
  every caller working (A5).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from models.errors import AdapterUnavailableError
from models.gateway import LiteLLMGateway
from models.policy import ModelPolicy
from workers.adapters.base import WorkerAdapter
from workers.adapters.litellm_worker import LiteLLMWorkerAdapter

#: Preference order. The vendor-neutral adapter is first, always.
PREFERRED_ORDER = ("litellm", "claude_code")


@dataclass
class WorkerAdapterRegistry:
    adapters: dict[str, WorkerAdapter] = field(default_factory=dict)

    def register(self, adapter: WorkerAdapter) -> WorkerAdapter:
        self.adapters[adapter.name] = adapter
        return adapter

    def unregister(self, name: str) -> None:
        self.adapters.pop(name, None)

    def get(self, name: str) -> WorkerAdapter:
        try:
            return self.adapters[name]
        except KeyError:
            raise AdapterUnavailableError(f"no worker adapter named {name!r} is registered") from None

    def names(self) -> list[str]:
        return sorted(self.adapters)

    def available(self) -> list[str]:
        return [name for name in self.names() if self.adapters[name].is_available()]

    def default(self) -> WorkerAdapter:
        """Return the first *available* adapter in preference order.

        GATE-D1-07 A5: with the Claude adapter disabled or absent this still
        returns a working adapter.
        """
        ordered = [n for n in PREFERRED_ORDER if n in self.adapters]
        ordered += [n for n in self.names() if n not in ordered]
        for name in ordered:
            adapter = self.adapters[name]
            if adapter.is_available():
                return adapter
        raise AdapterUnavailableError(
            f"no worker adapter is available (registered: {self.names()})"
        )


def claude_adapter_enabled() -> bool:
    return os.environ.get("EFAH_ENABLE_CLAUDE_ADAPTER", "").lower() in {"1", "true", "yes"}


def build_registry(
    gateway: LiteLLMGateway,
    *,
    policy: ModelPolicy | None = None,
    include_optional_vendor_adapters: bool = True,
) -> WorkerAdapterRegistry:
    """Build the registry the harness runs on."""
    registry = WorkerAdapterRegistry()
    registry.register(LiteLLMWorkerAdapter(gateway, policy=policy))

    if include_optional_vendor_adapters and claude_adapter_enabled():
        try:
            from workers.adapters.claude_code import ClaudeCodeWorkerAdapter
        except ImportError:
            return registry
        registry.register(ClaudeCodeWorkerAdapter(policy=policy))
    return registry
