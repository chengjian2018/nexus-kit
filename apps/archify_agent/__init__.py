"""archify_agent — translates the archify diagram skill into a nexus-kit
AGENT graph recipe.

route.py declares the nine-node topology / tool grants / semantic contracts
and binds the executors (plugins={"loop": node code}); executor.py carries
the nine-station NodeExecutor implementations (the route/author/repair/
perceptual review stations drive the LLM, while the probe/gate/deliver/
browser-check/report five stations execute deterministically); prompts.py
holds the prompt assets. See the route.py module docstring for graph
topology and per-station discipline.
"""
