from typing import Any, Dict, Optional

from nexus.model.plugins_field import normalize_plugins
from nexus.pipeline import normalize_skeleton


class Pattern:
    def __init__(self,
                 code,
                 name: str,
                 description: str,
                 entry_module_code,
                 modules: Optional[list[Any]] = None,
                 stages: Optional[list[Any]] = None,
                 plugins: Optional[Dict[str, Any]] = None,
                 agent_hooks: Optional[str] = None,
                 messages_builder: Optional[str] = None,
                 executor_loop: Optional[str] = None,
                 executor_fsm: Optional[str] = None,
                 executor_route: Optional[str] = None,
                 **kwargs):
        self.code = code
        self.name = name
        self.description = description
        self.entry_module_code = entry_module_code

        # Pipeline skeleton: ordered list of single-key dicts
        # ({slot_name: code-or-None}); normalized fail-fast at construction
        # (empty/None → the kernel default six-slot skeleton)
        self.stages = normalize_skeleton(stages)

        # Unified plugin declarations (stages-style dict, see
        # nexus/model/plugins_field.py): executor family (loop/fsm/route —
        # the merged form of the old executor_<family> fields) +
        # messages_builder + agent_hooks. Legacy scalar params fold in (the
        # dict value wins on conflict); read-side compat via the
        # executor_* / messages_builder / agent_hooks properties below.
        self.plugins = normalize_plugins(
            plugins,
            legacy={
                "loop": executor_loop,
                "fsm": executor_fsm,
                "route": executor_route,
                "messages_builder": messages_builder,
                "agent_hooks": agent_hooks,
            },
        )

        self.node_map = dict()
        self.module_map = dict()

        # ------------------------------------------------------------------
        # Module topology registration + registration-time fail fast
        # (dangling / self-loop / unauthorized config, spec §2.4)
        # ------------------------------------------------------------------
        self.max_hops = int(kwargs.pop("max_hops", 2))

        self.modules = modules
        if self.modules is not None:
            for module in self.modules:
                self.module_map[module.module_code] = module
                # AgentModule has no node_code; only FSM/Route modules do
                if hasattr(module, "node_code") and module.node_code:
                    self.node_map[module.node_code] = module

                for node in module.module_nodes:
                    self.node_map[node.node_code] = node

            for module in self.modules:
                # 1) sub_modules adjacency-edge validation (transfer tools /
                #    lent-tool config)
                for link in module.sub_modules:
                    if link["target"] not in self.module_map:
                        raise ValueError(
                            f"悬空转移边: {module.module_code} → {link['target']}"
                            f"（目标不在 module_map 中）"
                        )
                    if link["target"] == module.module_code:
                        raise ValueError(
                            f"自环转移边: {module.module_code} → {link['target']}"
                        )
                    target = self.module_map[link["target"]]
                    unauthorized = (set(link["lend_tools"])
                                    - set(target.use_tools or []))
                    if unauthorized:
                        raise ValueError(
                            f"越权借出: {module.module_code} 借出配置无效: "
                            f"{sorted(unauthorized)} 不在 {link['target']}.use_tools 中"
                        )
                # 2) Node jump_module config validation (fail fast on jump targets;
                #    consumed at runtime by the chat layer's _detect_jump_after_stage,
                #    which consults no adjacency graph)
                for node in module.module_nodes:
                    jump_target = getattr(node, "jump_module", None)
                    if jump_target:
                        if jump_target not in self.module_map:
                            raise ValueError(
                                f"悬空转移边: 节点 {node.node_code}.jump_module "
                                f"→ {jump_target} 不存在"
                            )
                        if jump_target == module.module_code:
                            raise ValueError(
                                f"自环转移边: 节点 {node.node_code}.jump_module "
                                f"→ {jump_target}（模块自环）"
                            )

        for key, value in kwargs.items():
            setattr(self, key, value)

    # ------------------------------------------------------------------
    # Read/write compat for the pre-merge scalar fields: they live on as
    # properties over the plugins dict (consumers' getattr reads, yml
    # old-shape loads, and post-construction assignments — the legacy
    # inline dict/callable forms included — stay untouched; serialization
    # emits the canonical plugins form)
    # ------------------------------------------------------------------

    @property
    def executor_loop(self) -> Optional[str]:
        return self.plugins.get("loop")

    @executor_loop.setter
    def executor_loop(self, value) -> None:
        self.plugins["loop"] = value

    @property
    def executor_fsm(self) -> Optional[str]:
        return self.plugins.get("fsm")

    @executor_fsm.setter
    def executor_fsm(self, value) -> None:
        self.plugins["fsm"] = value

    @property
    def executor_route(self) -> Optional[str]:
        return self.plugins.get("route")

    @executor_route.setter
    def executor_route(self, value) -> None:
        self.plugins["route"] = value

    @property
    def messages_builder(self):
        return self.plugins.get("messages_builder")

    @messages_builder.setter
    def messages_builder(self, value) -> None:
        self.plugins["messages_builder"] = value

    @property
    def agent_hooks(self):
        return self.plugins.get("agent_hooks")

    @agent_hooks.setter
    def agent_hooks(self, value) -> None:
        self.plugins["agent_hooks"] = value
