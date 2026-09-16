"""ppt_generator_agent app-local stage — the deterministic guards on top of
the builtin FSM unified stage.

``PPTUnifiedNLU`` (stage code ``ppt_unified``) subclasses ``FSMUnifiedNLU``
(the install_booking pattern: one LLM call writes reply + next_node + slots,
then deterministic zero-LLM guards post-process the decision). The same-turn
rewrites MUST ride this stage — FSM node transitions fire end-of-turn, so a
node-level NLG would resolve one turn late and clobber the reply (the
documented timing trap).

Guards, in execution order (all zero-LLM; real data only — the model can
never fabricate a tpl_id or a ppt_url):

1. ``_resolve_theme_pick`` — on a transition into ppt_generate that carries
   a user pick (tpl_id / style_name slots): match the pick against the REAL
   cached theme list (fetched once per conversation, cached on the state
   board). Resolved → the real style_id/tpl_id ride the transition slots;
   unresolvable → deterministic stay with an honest re-ask listing the
   valid options. From the theme-list node a pick is REQUIRED: generation
   without a resolved theme never proceeds from there (the auto path only
   exists from the ask node).
2. ``_apply_generate_guard`` — on ANY transition into ppt_generate: run the
   real Baidu generation (blocking 2-5 min, via asyncio.to_thread) and
   overwrite the hand-off reply with the outcome:
     - success → the final reply carries the real title + ppt_url;
     - failure → deterministic stay on the current node with an honest
       failure reason, and the template is logged into
       ``failed_tpl_ids`` (tried-and-failed memory: the same template is
       refused after MAX_ATTEMPTS_PER_TEMPLATE failures — the reply then
       lists what was tried and suggests switching templates);
     - no pick → auto path: suggest_category(topic) (keyword table) +
       pick_theme_for_category — the ported smart selection, code not
       prompt;
     - BAIDU_API_KEY unset → honest config-error reply, stay (not counted
       as a template attempt — retrying cannot help until the env is set).
3. ``_apply_themes_rewrite`` — on EVERY transition into ppt_show_themes:
   fetch (or read from cache) the real theme list and overwrite the
   hand-off reply with it. The user always picks from data the API
   actually returned — never from a model re-roll.

State board: one namespaced key ``ppt_gen_state`` —
    {topic, themes, themes_fetched, selected, last_result, failed_tpl_ids}
``themes`` is the loop-inheritance cache (re-picking after 换模板 never
re-fetches); ``failed_tpl_ids`` is the anti-replay memory for the
generate ⇄ retry/switch cycles; ``selected``/``last_result`` carry what the
delivery turn produced.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from atoms.stages.unified import FSMUnifiedNLU
from nexus.context import DialogueContext
from nexus.registry.plugins import registry as plugin_registry

from apps.ppt_generator_agent import tools

logger = logging.getLogger(__name__)


class PPTUnifiedNLU(FSMUnifiedNLU):
    """FSM unified stage + theme-pick resolution + generation guard +
    theme-list rewrite (see module docstring).

    Scenario variants subclass and rebind the node-code class attributes
    and the wording pieces — the guard machinery is node-graph agnostic.
    """

    stage_name = "ppt_unified"

    # Node-code wiring (rebind in scenario variants)
    GENERATE_NODE = "ppt_generate"
    THEMES_NODE = "ppt_show_themes"

    # State-board key (one namespaced key per app)
    STATE_KEY = "ppt_gen_state"

    # Anti-replay: the same template is refused after this many failures
    MAX_ATTEMPTS_PER_TEMPLATE = 2

    # Wording pieces for the deterministic (zero-LLM) replies
    THEMES_LEAD = "当前可用的模板风格如下："
    THEMES_TAIL = "\n回复模板编号或风格名称即可选用；也可以返回上一歩，由我按主题智能匹配。"
    THEMES_FETCH_FAILED = "模板列表暂时拉取失败（{reason}），请稍后再试，或回复「自动匹配」由我直接生成。"
    PICK_UNRESOLVED = "没有找到编号/名称为「{pick}」的模板，请从下面的列表中重新选择：\n{options}"
    PICK_REQUIRED = "请先从上面的列表中选一个模板（回复编号或风格名称）；或者返回上一歩由我自动匹配。"
    GENERATING_HANDOFF = "收到，开始为你生成《{topic}》相关的 PPT，大约需要 2-5 分钟，请稍候……"
    NO_TOPIC = "请先告诉我这份 PPT 的主题，我再开始生成。"
    NO_API_KEY = "尚未配置 BAIDU_API_KEY 环境变量，无法调用生成服务。请配置后再试。"
    ATTEMPTS_EXHAUSTED = (
        "模板 {tpl} 已连续失败 {n} 次（试过：{tried}），先不再重试这一个了。"
        "你可以换一个模板再试，或者稍后再来。"
    )
    GENERATION_FAILED = (
        "抱歉，PPT 生成失败了（{reason}）。这是第 {n} 次尝试该模板，"
        "你可以再说一次「继续生成」让我重试，或换个模板。"
    )
    GENERATION_OK = (
        "✅ 你的 PPT《{title}》已生成完成！\n"
        "下载链接：{url}\n"
        "如需更换模板重新生成，随时告诉我。"
    )

    async def execute(self, ctx: DialogueContext) -> DialogueContext:
        await super().execute(ctx)  # the builtin single call (reply/next_node/slots)
        self._resolve_theme_pick(ctx)
        await self._apply_generate_guard(ctx)
        self._apply_themes_rewrite(ctx)
        return ctx

    # ------------------------------------------------------------------
    # State board helpers
    # ------------------------------------------------------------------

    def _state(self, ctx: DialogueContext) -> Dict[str, Any]:
        state = ctx.graph_state.setdefault(self.STATE_KEY, {})
        state.setdefault("failed_tpl_ids", {})
        return state

    def _cached_themes(self, ctx: DialogueContext,
                       state: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        """The cached theme list (None when absent — never half-trust the
        cache: a fetch failure leaves it unset)."""
        themes = state.get("themes")
        return themes if themes else None

    def _fetch_themes(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Fetch the real theme list via the API client and cache it on the
        state board. Raises on failure (caller owns the honest path)."""
        api_key = tools.get_api_key()
        if not api_key:
            raise RuntimeError("BAIDU_API_KEY 未配置")
        themes = tools.fetch_ppt_themes(api_key)
        state["themes"] = themes
        state["themes_fetched"] = True
        return themes

    # ------------------------------------------------------------------
    # Guard 1 — theme pick resolution (deterministic, anti-fabrication)
    # ------------------------------------------------------------------

    def _slots_in(self, ctx: DialogueContext) -> Dict[str, Any]:
        return dict((ctx.nlu_result or {}).get("slots") or {})

    def _write_back(self, ctx: DialogueContext, slots: Dict[str, Any],
                    next_node: Optional[str] = None) -> None:
        """Rewrite the unified output (slots, and next_node when forced)."""
        nlu_result = dict(ctx.nlu_result or {})
        nlu_result["slots"] = slots
        if next_node is not None:
            nlu_result["next_node"] = next_node
        ctx.nlu_result = nlu_result

    def _stay_with(self, ctx: DialogueContext, reply: str,
                   slots: Dict[str, Any]) -> None:
        """Force a deterministic stay on the current node with an honest
        (guard-authored) reply."""
        self._write_back(ctx, slots, next_node="")
        ctx.nlg_result = {"content": reply, "deterministic": True}
        meta = dict(ctx.metadata.get("unified") or {})
        meta["ppt_guard"] = {"stayed": True, "reply": reply}
        ctx.metadata["unified"] = meta

    def _resolve_theme_pick(self, ctx: DialogueContext) -> None:
        """Match a user pick (tpl_id / style_name) against the real theme
        list; unresolvable picks deterministically stay."""
        nlu_result = ctx.nlu_result or {}
        if nlu_result.get("next_node") != self.GENERATE_NODE:
            return
        slots = self._slots_in(ctx)

        pick_tpl = str(slots.get("tpl_id") or "").strip()
        pick_name = str(slots.get("style_name")
                        or slots.get("theme_name") or "").strip()
        if slots.get("resolved_theme"):
            return  # already carries a resolved theme (self-loop re-runs)
        if not pick_tpl and not pick_name:
            if ctx.current_node_code == self.THEMES_NODE:
                # From the list node a pick is REQUIRED — no silent auto
                self._stay_with(ctx, self.PICK_REQUIRED, slots)
            return  # from other nodes the auto path may proceed

        state = self._state(ctx)
        themes = self._cached_themes(ctx, state)
        if themes is None:
            try:
                themes = self._fetch_themes(state)
            except Exception as e:  # noqa: BLE001 — honest stay, reason shown
                logger.warning("[ppt_gen] 模板列表拉取失败: %s", e)
                self._stay_with(
                    ctx,
                    self.THEMES_FETCH_FAILED.format(reason=str(e)[:120]),
                    slots)
                return

        theme = self._match_theme(themes, pick_tpl, pick_name)
        if theme is None:
            pick = pick_tpl or pick_name
            self._stay_with(
                ctx,
                self.PICK_UNRESOLVED.format(
                    pick=pick, options=self._format_options(themes)),
                slots)
            return

        slots["tpl_id"] = str(theme["tpl_id"])
        slots["style_id"] = str(theme.get("style_id", 0))
        slots["style_name"] = (theme.get("style_name_list") or ["默认"])[0]
        slots["resolved_theme"] = "true"
        self._write_back(ctx, slots)
        logger.info("[ppt_gen] 模板选定: tpl_id=%s (%s)",
                    theme["tpl_id"], slots["style_name"])

    @staticmethod
    def _match_theme(themes: List[Dict[str, Any]],
                     pick_tpl: str, pick_name: str) -> Optional[Dict[str, Any]]:
        """Exact tpl_id match first, then style-name containment (both
        directions) — deterministic against the REAL list only."""
        if pick_tpl:
            for theme in themes:
                if str(theme.get("tpl_id")) == pick_tpl:
                    return theme
        if pick_name:
            for theme in themes:  # exact style-name hit
                if pick_name in (theme.get("style_name_list") or []):
                    return theme
            lowered = pick_name.lower()
            for theme in themes:  # containment hit
                for name in theme.get("style_name_list") or []:
                    if lowered in name.lower() or name.lower() in lowered:
                        return theme
        return None

    def _format_options(self, themes: List[Dict[str, Any]],
                        limit: int = 10) -> str:
        """Compact deterministic option list (primary style name + tpl_id)."""
        lines, seen = [], set()
        for theme in themes:
            name = (theme.get("style_name_list") or ["默认"])[0]
            if name in seen:
                continue
            seen.add(name)
            lines.append(f"- {name}（编号 {theme.get('tpl_id')}）")
            if len(lines) >= limit:
                break
        if len(seen) < len(themes):
            lines.append(f"……等共 {len(themes)} 个模板")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Guard 2 — generation on transition into the generate node
    # ------------------------------------------------------------------

    async def _apply_generate_guard(self, ctx: DialogueContext) -> None:
        nlu_result = ctx.nlu_result or {}
        if nlu_result.get("next_node") != self.GENERATE_NODE:
            return

        state = self._state(ctx)
        slots = self._slots_in(ctx)

        # Topic inheritance: filled at the start node, kept on the board so
        # later regenerate turns never lose it
        topic = (ctx.filled_slots or {}).get("ppt_topic") \
            or slots.get("ppt_topic") or state.get("topic") or ""
        topic = str(topic).strip()
        if not topic:
            self._stay_with(ctx, self.NO_TOPIC, slots)
            return
        state["topic"] = topic

        api_key = tools.get_api_key()
        if not api_key:
            self._stay_with(ctx, self.NO_API_KEY, slots)
            return

        # Theme resolution: resolved pick, else the auto path (code, not
        # prompt — the keyword table + weighted pick, degrading to the
        # API-side random template when the list is unavailable)
        if slots.get("resolved_theme"):
            tpl_id: Optional[int] = int(slots["tpl_id"])
            style_id = int(slots.get("style_id", 0))
            style_name = slots.get("style_name", "")
            category = ""
        else:
            category = tools.suggest_category(topic)
            tpl_id, style_id, style_name = None, 0, ""
            themes = self._cached_themes(ctx, state)
            if themes is None:
                try:
                    themes = self._fetch_themes(state)
                except Exception as e:  # noqa: BLE001 — degrade to random
                    logger.warning("[ppt_gen] 自动匹配时模板列表不可用，"
                                   "退化为 API 随机模板: %s", e)
                    themes = []
            if themes:
                theme = tools.pick_theme_for_category(themes, category)
                if theme is not None:
                    tpl_id = int(theme["tpl_id"])
                    style_id = int(theme.get("style_id", 0))
                    style_name = (theme.get("style_name_list")
                                  or ["默认"])[0]
        slots.update({
            "tpl_id": "" if tpl_id is None else str(tpl_id),
            "style_id": str(style_id),
            "style_name": style_name,
            "auto_category": category,
            "generation_status": "running",
        })

        # Anti-replay: the tried-and-failed memory refuses the same
        # template past the attempt cap (honest exit, suggest switching)
        attempt_key = "auto" if tpl_id is None else str(tpl_id)
        tried = dict(state.get("failed_tpl_ids") or {})
        if tried.get(attempt_key, 0) >= self.MAX_ATTEMPTS_PER_TEMPLATE:
            tried_desc = "、".join(
                f"模板 {key}×{n} 次" for key, n in tried.items()) or attempt_key
            self._stay_with(
                ctx,
                self.ATTEMPTS_EXHAUSTED.format(
                    tpl=style_name or attempt_key,
                    n=tried.get(attempt_key, 0),
                    tried=tried_desc),
                slots)
            return

        self._write_back(ctx, slots)
        # The model's hand-off line only stands until the call returns;
        # surface it in logs so long waits are traceable
        logger.info("[ppt_gen] 开始生成: topic=%r, tpl_id=%s, category=%r",
                    topic, tpl_id, category or "(已选定)")

        try:
            final = await asyncio.to_thread(
                self._run_generation, api_key, topic, style_id, tpl_id)
        except Exception as e:  # noqa: BLE001 — honest failure, counted
            tried[attempt_key] = tried.get(attempt_key, 0) + 1
            state["failed_tpl_ids"] = tried
            state["last_result"] = {
                "status": "failed", "tpl_id": tpl_id,
                "reason": str(e)[:200],
            }
            slots["generation_status"] = "failed"
            self._write_back(ctx, slots, next_node="")  # deterministic stay
            ctx.nlg_result = {
                "content": self.GENERATION_FAILED.format(
                    reason=str(e)[:120],
                    n=tried[attempt_key]),
                "deterministic": True,
            }
            logger.warning("[ppt_gen] 生成失败（模板 %s 第 %s 次）: %s",
                           attempt_key, tried[attempt_key], e)
            return

        title = str(final.get("title") or topic)
        url = str((final.get("data") or {}).get("ppt_url") or "")
        if not url:
            tried[attempt_key] = tried.get(attempt_key, 0) + 1
            state["failed_tpl_ids"] = tried
            state["last_result"] = {
                "status": "failed", "tpl_id": tpl_id,
                "reason": "生成流结束但未返回 ppt_url",
            }
            slots["generation_status"] = "failed"
            self._write_back(ctx, slots, next_node="")
            ctx.nlg_result = {
                "content": self.GENERATION_FAILED.format(
                    reason="服务未返回下载链接", n=tried[attempt_key]),
                "deterministic": True,
            }
            return

        state["selected"] = {
            "tpl_id": tpl_id, "style_id": style_id, "style_name": style_name,
            "category": category,
        }
        state["last_result"] = {
            "status": "success", "title": title, "ppt_url": url,
            "tpl_id": tpl_id,
        }
        slots["generation_status"] = "success"
        self._write_back(ctx, slots)
        ctx.nlg_result = {
            "content": self.GENERATION_OK.format(title=title, url=url),
            "deterministic": True,
        }
        logger.info("[ppt_gen] 生成完成: title=%r, url=%s", title, url)

    @staticmethod
    def _run_generation(api_key: str, topic: str, style_id: int,
                        tpl_id: Optional[int]) -> Dict[str, Any]:
        """The blocking 2-5 minute Baidu call — off the event loop thread."""
        return tools.generate_ppt_blocking(
            api_key, topic, style_id=style_id, tpl_id=tpl_id)

    # ------------------------------------------------------------------
    # Guard 3 — real theme-list rewrite on entering the themes node
    # ------------------------------------------------------------------

    def _apply_themes_rewrite(self, ctx: DialogueContext) -> None:
        nlu_result = ctx.nlu_result or {}
        if nlu_result.get("next_node") != self.THEMES_NODE:
            return

        state = self._state(ctx)
        slots = self._slots_in(ctx)
        themes = self._cached_themes(ctx, state)
        if themes is None:
            try:
                themes = self._fetch_themes(state)
            except Exception as e:  # noqa: BLE001 — honest stay, reason shown
                logger.warning("[ppt_gen] 模板列表拉取失败: %s", e)
                self._stay_with(
                    ctx,
                    self.THEMES_FETCH_FAILED.format(reason=str(e)[:120]),
                    slots)
                return

        ctx.nlg_result = {
            "content": (
                f"{self.THEMES_LEAD}\n{self._format_options(themes)}"
                f"{self.THEMES_TAIL}"),
            "deterministic": True,
        }
        logger.info("[ppt_gen] 模板列表改写（%d 个模板，缓存=%s）",
                    len(themes), bool(self._cached_themes(ctx, state)))


# ============================================================================
# Plugin registration (kind="stage") — the pattern stages skeleton in
# route.py references this code; route.py's bottom import registers it
# ============================================================================

plugin_registry.register("stage", "ppt_unified", PPTUnifiedNLU)
