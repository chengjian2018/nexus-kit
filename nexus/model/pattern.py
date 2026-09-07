from typing import Optional, Any


class Pattern:
    def __init__(self,
                 code,
                 name: str,
                 description: str,
                 entry_module_code,
                 modules: Optional[list[Any]] = None,
                 stages: Optional[list[Any]] = None,
                 generate: Optional[Any] = None,
                 pre_recall: Optional[Any] = None,
                 query: Optional[Any] = None,
                 post_recall: Optional[Any] = None,
                 agent_hooks: Optional[dict] = None,
                 messages_builder: Optional[Any] = None,
                 **kwargs):
        self.code = code
        self.name = name
        self.description = description
        self.modules = modules
        self.stages = stages
        self.entry_module_code = entry_module_code

        # Agent loop hooks (pattern-level declaration; the module-level
        # agent_hooks can wholesale-replace it; form {point: [hook,...]},
        # consumer: chat/agent_hooks.py)
        self.agent_hooks = agent_hooks

        # AGENT all-in-one messages builder (pattern-level declaration; the
        # module-level messages_builder can override it; signature
        # (module, cxt, extra_blocks) -> messages, including system-row
        # assembly; consumer: chat/messages.py)
        self.messages_builder = messages_builder

        # Pipeline slot three-layer defaults (the pattern layer of node > module > pattern)
        self.generate = generate
        self.pre_recall = pre_recall
        self.query = query
        self.post_recall = post_recall

        self.node_map = dict()
        self.module_map = dict()

        # ------------------------------------------------------------------
        # Module topology registration + registration-time fail fast
        # (dangling / self-loop / unauthorized config, spec §2.4)
        # ------------------------------------------------------------------
        self.max_hops = int(kwargs.pop("max_hops", 2))

        if self.modules is not None:
            for module in self.modules:
                self.module_map[module.module_code] = module
                # AgentModule has no node_code; only FSM/Route modules do
                if hasattr(module, "node_code") and module.node_code:
                    self.node_map[module.node_code] = module

                for node in module.module_nodes:
                    self.node_map[node.node_code] = node

            for module in self.modules:
                # 1) sub_modules adjacency-edge validation (transfer tools / lent-tool config)
                for link in module.sub_modules:
                    if link.target not in self.module_map:
                        raise ValueError(
                            f"悬空转移边: {module.module_code} → {link.target}"
                            f"（目标不在 module_map 中）"
                        )
                    if link.target == module.module_code:
                        raise ValueError(
                            f"自环转移边: {module.module_code} → {link.target}"
                        )
                    target = self.module_map[link.target]
                    unauthorized = set(link.lend_tools) - set(target.use_tools or [])
                    if unauthorized:
                        raise ValueError(
                            f"越权借出: {module.module_code} 借出配置无效: "
                            f"{sorted(unauthorized)} 不在 {link.target}.use_tools 中"
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


