"""
==============================================================================
backend/agents/isfj_archivist.py
------------------------------------------------------------------------------
ISFJ · 资料考据型 (SJ 阵营) · 唯一拥有 ChromaDB 检索权的 Agent

人格定位:
    辩论的"档案管理员"。情绪稳定 (低 λ),不爱激辩立场。
    它是整个系统中 *唯一* 在发言前会经过 RETRIEVING 状态的 Agent —
    每次开口前都会从 ChromaDB 中检索与当前议题相关的历史发言或归档摘要,
    把检索到的内容作为"可引用证据"注入 Prompt,严禁凭空主张。

工程参数特化 (核心: RETRIEVING 状态路径):
    - λ = 0.04 (1/s)         半衰期 ≈ 17.3s,稳
    - KEYWORD_TRIGGER × 1.6  对关键词高度敏感 (它是检索者)
    - STANCE_CONFLICT × 0.6  不爱激辩立场
    - TOPIC_DRIFT × 0.7      偏题它不敏感 (那是 ISTJ 的事)
    - PASSIVE_LISTEN × 1.0   旁听平均
    - NAMED_BY_OTHER × 1.0
    - temperature 0.6        低温度,严谨规整
    - 检索内部超时 3s        超时即降级为无检索发言

RETRIEVING 路径 (本类核心特化):
    1. 拿到麦克风后,FSM: LISTENING → RETRIEVING
    2. asyncio.to_thread 走 ChromaGateway.query 检索 top-K
    3. 把检索结果注入 _pending_retrieval_context (实例字段)
    4. FSM: RETRIEVING → COMPUTING (然后走基类推理)
    5. prepare_extra_system_prefix 读取 _pending_retrieval_context,
       把证据清单拼入 Prompt
    6. 任何路径失败 → 优雅降级 (跳过检索,走标准发言)

⚠️ 工程铁律:
    - 严禁 ChromaDB 调用阻塞事件循环 (走 ChromaGateway.query 异步包装)
    - 严禁检索失败导致整轮失败 (try/except 优雅降级)
    - 严禁 _pending_retrieval_context 跨发言污染 (finally 清空)
    - 严禁 RETRIEVING 阶段无超时 (内部 3s 兜底)

==============================================================================
"""

from __future__ import annotations

import asyncio
from typing import Final, Optional

from loguru import logger

from backend.agents.base_agent import AgentPersona, BaseAgent
from backend.core.aggro_engine import (
    AggroEngine,
    StimulusEvent,
    StimulusKind,
)
from backend.core.arbitrator import (
    MicrophoneToken,
    RequestKind,
)
from backend.core.context_bus import (
    ContextBus,
    Topic,
    TruncationReason,
    TurnSealedEvent,
)
from backend.core.fsm import (
    FSMState,
    IllegalTransitionError,
)
from backend.core.llm_router import InvocationResult
from backend.memory.chroma_gateway import (
    ChromaGateway,
    QueryHit,
    get_chroma_gateway,
)
from backend.schemas.agent_state import MBTIType


# ──────────────────────────────────────────────────────────────────────────────
# 工程参数 (集中管理)
# ──────────────────────────────────────────────────────────────────────────────

#: Agent 唯一 ID
_AGENT_ID: Final[str] = "ISFJ_archivist"

#: HUD 显示名
_DISPLAY_NAME: Final[str] = "资料考据 ISFJ"

#: 人格原型
_ARCHETYPE: Final[str] = "资料考据型"

#: λ 衰减系数
_DECAY_LAMBDA: Final[float] = 0.04

#: 采样参数
_TEMPERATURE: Final[float] = 0.6
_TOP_P: Final[float] = 0.85
_MAX_TOKENS: Final[int] = 320

#: 检索 top-K
_RETRIEVAL_TOP_K: Final[int] = 5

#: 检索超时 (秒) · 超时即降级为无检索发言
_RETRIEVAL_TIMEOUT_SEC: Final[float] = 3.0

#: 检索结果中单条引用的最大字符数 (防 Prompt 撑爆)
_RETRIEVAL_PER_HIT_MAX_CHARS: Final[int] = 240

#: 注入 Prompt 的检索结果块最大字符数
_RETRIEVAL_BLOCK_MAX_CHARS: Final[int] = 1200

#: 自我职责完成回退
_ISFJ_DUTY_DONE_DELTA: Final[float] = -8.0

#: 强心剂注入文本
_BEHAVIOR_ANCHOR: Final[str] = (
    "保持考据严谨。每次发言必须基于已经发生的内容 (历史发言或归档摘要),"
    "至少引用一条具体的过往陈述,引用时使用'A 在第 N 轮提到……'或"
    "'根据归档摘要…'之类的格式。严禁凭空主张、严禁'据说'、'有人认为'式的模糊化引用,"
    "严禁伪造数据或假设性场景。"
    "如果检索到的证据与你想表达的立场不一致,你应当如实呈现冲突而非掩盖。"
    "语气克制、用词精确、避免情绪化形容。可使用'根据……'、'记录显示……'等中性句式。"
)

#: 个人关键词 (考据向)
_PERSONAL_KEYWORDS: Final[tuple[str, ...]] = (
    "有据可查",
    "记录显示",
    "上一轮",
    "历史发言",
    "之前提到",
    "档案",
    "原话",
    "出处",
)

#: 别名
_ALIASES: Final[tuple[str, ...]] = (
    "ISFJ",
    "archivist",
    "资料考据",
    "ISFJ_archivist",
)

#: 个性化刺激权重 (绝对值)
_AGGRO_WEIGHT_OVERRIDES: Final[dict[StimulusKind, float]] = {
    # base 8.0 × 1.6 = 12.8   关键词高度敏感
    StimulusKind.KEYWORD_TRIGGER: 12.8,
    # base 12.0 × 0.6 = 7.2   立场冲突弱
    StimulusKind.STANCE_CONFLICT: 7.2,
    # base 6.0 × 0.7 = 4.2    偏题不敏感
    StimulusKind.TOPIC_DRIFT: 4.2,
    # base 0.4 × 1.0          旁听平均
    # base 18.0 × 1.0         被点名平均
    # base 5.0 × 0.6 = 3.0    沉默不慌
    StimulusKind.LONG_SILENCE: 3.0,
    # base 4.0 × 1.0          全局沸腾平均
    # base 3.0 × 1.0          阵营压力平均
    # base -6.0 × 1.0         拒绝后正常泄气
}

#: "凭空主张"语调检测词表 — 命中触发额外 KEYWORD_TRIGGER (考据警觉)
_UNSUPPORTED_CLAIM_MARKERS: Final[tuple[str, ...]] = (
    "据说",
    "有人认为",
    "众所周知",
    "历来如此",
    "毫无疑问",
    "肯定是",
    "绝对",
    "从来",
    "永远",
    "所有",
    "每个人都",
    "明显",
)


# ──────────────────────────────────────────────────────────────────────────────
# Agent 主体
# ──────────────────────────────────────────────────────────────────────────────


class IsfjArchivistAgent(BaseAgent):
    """
    ISFJ · 资料考据型 · 唯一拥有 ChromaDB 检索权。

    继承 BaseAgent 模板方法骨架,特化:
        - persona / behavior_anchor / personal_keywords
        - aggro_weight_overrides (KEYWORD_TRIGGER 高敏)
        - 采样参数 (低温度严谨)
        - prepare_extra_system_prefix (读取 _pending_retrieval_context 注入证据)
        - compute_passive_stimuli_on_other_speak (凭空主张语调检测)
        - attempt_speak (插入 RETRIEVING 阶段)
        - on_own_turn_sealed (职责完成释放)
    """

    # ════════════════════════════════════════════════════════════
    # 初始化
    # ════════════════════════════════════════════════════════════

    def __init__(
        self,
        *,
        chroma_gateway: Optional[ChromaGateway] = None,
        **kwargs: object,
    ) -> None:
        self.persona: AgentPersona = AgentPersona(
            agent_id=_AGENT_ID,
            mbti=MBTIType.ISFJ,
            display_name=_DISPLAY_NAME,
            archetype=_ARCHETYPE,
            decay_lambda=_DECAY_LAMBDA,
            has_chroma_access=True,  # ⚠️ 唯一 True
            is_summarizer=False,
        )
        super().__init__(**kwargs)  # type: ignore[arg-type]

        # ChromaGateway 注入 (默认走全局单例)
        self._chroma: ChromaGateway = chroma_gateway or get_chroma_gateway()

        # 跨阶段状态: RETRIEVING 阶段填充,COMPUTING 阶段读取,finally 清空
        self._pending_retrieval_context: Optional[str] = None

        # 互斥锁: 防止 attempt_speak 多次重入污染 _pending_retrieval_context
        self._retrieval_lock: asyncio.Lock = asyncio.Lock()

    # ════════════════════════════════════════════════════════════
    # 抽象接口实现
    # ════════════════════════════════════════════════════════════

    @property
    def behavior_anchor(self) -> str:
        return _BEHAVIOR_ANCHOR

    @property
    def personal_keywords(self) -> tuple[str, ...]:
        return _PERSONAL_KEYWORDS

    @property
    def aliases(self) -> tuple[str, ...]:
        return _ALIASES

    @property
    def aggro_weight_overrides(self) -> dict[StimulusKind, float]:
        return dict(_AGGRO_WEIGHT_OVERRIDES)

    @property
    def temperature(self) -> float:
        return _TEMPERATURE

    @property
    def top_p(self) -> float:
        return _TOP_P

    @property
    def max_tokens(self) -> int:
        return _MAX_TOKENS

    # ════════════════════════════════════════════════════════════
    # 重写: Prompt 前置上下文 (注入检索证据)
    # ════════════════════════════════════════════════════════════

    def prepare_extra_system_prefix(self) -> str:
        """
        在 base 前缀基础上追加:
            1. ISFJ 输出格式硬约束 (强制引用)
            2. 检索到的证据清单 (来自 _pending_retrieval_context)

        若检索失败 / 无结果 → 仅注入硬约束,Prompt 中明示"暂无可引用记录"。
        """
        base = super().prepare_extra_system_prefix() or ""
        format_constraint = (
            "[ISFJ 输出格式硬约束]"
            " 你的发言必须基于下方'可引用证据清单'中的至少一条记录。"
            " 引用格式:'根据 ⟨发言者⟩ 在第 N 轮的陈述: ……, 我认为……'"
            " 或:'结合归档摘要中提到的 X 现象,可以判断……'"
            " 严禁未引用任何记录直接发表主张。"
            " 若证据清单为空,请直说'目前尚无可考据的历史记录,请其他人先就 ⟨议题⟩ 提供具体陈述',"
            "并 *仅* 输出这一句不要展开。"
            " 全文不超过 160 字,语气中性、用词精确,不使用感叹号。"
        )

        evidence_block = self._pending_retrieval_context or (
            "[可引用证据清单] (空) — 暂无可考据的历史记录。"
        )

        return f"{base}\n\n{format_constraint}\n\n{evidence_block}"

    # ════════════════════════════════════════════════════════════
    # 重写: 被动刺激计算 (凭空主张语调检测)
    # ════════════════════════════════════════════════════════════

    def compute_passive_stimuli_on_other_speak(
        self,
        event: TurnSealedEvent,
    ) -> list[StimulusEvent]:
        """
        ISFJ 对他人发言的考据敏感型刺激规则:

            1. 被点名 → NAMED_BY_OTHER
            2. 关键词命中 → KEYWORD_TRIGGER (1.6 倍权重)
            3. 立场冲突 → STANCE_CONFLICT (0.6 倍)
            4. 旁听背景 → PASSIVE_LISTEN
            5. 检测"凭空主张"语调 → 额外 KEYWORD_TRIGGER (考据警觉)

        ⚠️ 严禁调 LLM,纯启发式扫描。
        """
        engine: AggroEngine = self._aggro_engine
        agent_id = self.persona.agent_id
        out: list[StimulusEvent] = []
        speaker_id = event.turn.agent_id
        text = event.turn.text or ""

        # 1) 被点名
        targets = event.named_targets
        if agent_id in targets or "ALL" in targets:
            out.append(
                engine.make_event(
                    agent_id=agent_id,
                    kind=StimulusKind.NAMED_BY_OTHER,
                    scale=1.0,
                    source_agent_id=speaker_id,
                    note=f"ISFJ 被 {speaker_id} 点名",
                )
            )

        # 2) 关键词命中
        kw_list = event.keyword_hits.get(agent_id, [])
        if kw_list:
            scale = min(2.0, 0.6 + 0.4 * len(kw_list))
            out.append(
                engine.make_event(
                    agent_id=agent_id,
                    kind=StimulusKind.KEYWORD_TRIGGER,
                    scale=scale,
                    source_agent_id=speaker_id,
                    note=f"命中关键词: {','.join(kw_list[:3])}",
                )
            )

        # 3) 立场冲突 (跨阵营)
        speaker_faction = self._guess_faction_from_id(speaker_id)
        if (
            speaker_faction is not None
            and speaker_faction != self.persona.faction.value
        ):
            out.append(
                engine.make_event(
                    agent_id=agent_id,
                    kind=StimulusKind.STANCE_CONFLICT,
                    scale=1.0,
                    source_agent_id=speaker_id,
                    note=f"立场冲突: {speaker_faction} vs SJ",
                )
            )

        # 4) 旁听背景
        out.append(
            engine.make_event(
                agent_id=agent_id,
                kind=StimulusKind.PASSIVE_LISTEN,
                scale=1.0,
                source_agent_id=None,
                note="passive_listen",
            )
        )

        # 5) 凭空主张语调检测 → 额外 KEYWORD_TRIGGER
        unsupported_hits = self._scan_markers(
            text, _UNSUPPORTED_CLAIM_MARKERS
        )
        if unsupported_hits:
            scale = min(1.5, 0.5 + 0.25 * len(unsupported_hits))
            out.append(
                engine.make_event(
                    agent_id=agent_id,
                    kind=StimulusKind.KEYWORD_TRIGGER,
                    scale=scale,
                    source_agent_id=speaker_id,
                    note=(
                        f"凭空主张语调命中: {','.join(unsupported_hits[:3])}"
                    ),
                )
            )

        return out

    @staticmethod
    def _scan_markers(text: str, markers: tuple[str, ...]) -> list[str]:
        if not text:
            return []
        seen: set[str] = set()
        out: list[str] = []
        for m in markers:
            if m in text and m not in seen:
                seen.add(m)
                out.append(m)
        return out

    @staticmethod
    def _guess_faction_from_id(agent_id: str) -> str | None:
        if not agent_id or len(agent_id) < 4:
            return None
        prefix = agent_id[:4].upper()
        nt = {"INTJ", "ENTP", "INTP", "ENTJ"}
        nf = {"INFP", "ENFP", "INFJ", "ENFJ"}
        sj = {"ISTJ", "ISFJ", "ESTJ", "ESFJ"}
        sp = {"ESTP", "ISFP", "ESFP", "ISTP"}
        if prefix in nt:
            return "NT"
        if prefix in nf:
            return "NF"
        if prefix in sj:
            return "SJ"
        if prefix in sp:
            return "SP"
        return None

    # ════════════════════════════════════════════════════════════
    # 重写: attempt_speak (插入 RETRIEVING 阶段)
    # ════════════════════════════════════════════════════════════

    async def attempt_speak(
        self,
        *,
        request_kind: RequestKind = RequestKind.AGGRO_THRESHOLD,
        force_token: Optional[MicrophoneToken] = None,
    ) -> Optional[InvocationResult]:
        """
        ISFJ 的 attempt_speak 重写:

        基类流程:
            申请令牌 → 转 COMPUTING → invoke_chat_stream → 释放
        ISFJ 流程:
            申请令牌 → 转 RETRIEVING → 检索 → 注入 context → 转 COMPUTING
            → 走基类剩余路径 (这里通过让基类继续即可,因 transition 已就绪)

        实现策略:
            1. 我们在锁内串行化 _pending_retrieval_context 的写入
            2. 利用基类 attempt_speak 的转换矩阵 (LISTENING → COMPUTING),
               但 ISFJ 的合法路径是 LISTENING → RETRIEVING → COMPUTING
            3. 因此我们在调 super().attempt_speak 之前,先手动:
               - 申请令牌
               - FSM: LISTENING → RETRIEVING
               - 检索
               - FSM: RETRIEVING → COMPUTING ← 这步交给基类做
            4. 但基类 attempt_speak 内部会再次申请令牌 → 冲突
               所以我们改写: 在拿到 token 后,直接调 super 的 force_token 路径

        简化方案:
            为了不分裂模板方法,我们重写 attempt_speak 完整流程,
            但在 RETRIEVING 阶段独立处理,然后调 super().attempt_speak(force_token=...)
            让基类用我们手中已检索好的 _pending_retrieval_context 推理。

        ⚠️ 该方法持 _retrieval_lock,严禁同 Agent 并发调用。
        """
        if not self.is_registered or self._fsm is None:
            return await super().attempt_speak(
                request_kind=request_kind,
                force_token=force_token,
            )

        async with self._retrieval_lock:
            # ── 1. 拿令牌 ──
            token: Optional[MicrophoneToken] = force_token
            if token is None:
                token = await self._acquire_token(request_kind)
                if token is None:
                    return None

            # ── 2. FSM: LISTENING → RETRIEVING ──
            try:
                await self._fsm.transition(
                    FSMState.RETRIEVING,
                    reason=f"isfj_retrieve_start:{request_kind.value}",
                )
            except IllegalTransitionError as exc:
                # 当前不是 LISTENING (可能被其他事件转走) → 释放令牌降级
                logger.warning(
                    f"[ISFJ:{self.persona.agent_id}] FSM 拒绝转 RETRIEVING "
                    f"(state={self._fsm.state.value}) · 释放令牌: {exc}"
                )
                await self._safe_release(token, fsm_target=FSMState.LISTENING)
                return None

            # ── 3. 检索 (带超时 + 失败降级) ──
            retrieval_text: Optional[str] = None
            try:
                retrieval_text = await asyncio.wait_for(
                    self._perform_retrieval(),
                    timeout=_RETRIEVAL_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"[ISFJ:{self.persona.agent_id}] 检索超时 "
                    f"({_RETRIEVAL_TIMEOUT_SEC}s) · 降级为无检索发言"
                )
                retrieval_text = None
            except Exception as exc:
                logger.warning(
                    f"[ISFJ:{self.persona.agent_id}] 检索异常 (降级): {exc!r}"
                )
                retrieval_text = None

            self._pending_retrieval_context = retrieval_text

            # ── 4. FSM: RETRIEVING → COMPUTING ──
            # 转 COMPUTING 是合法路径 (见 fsm.py 转换矩阵)
            try:
                await self._fsm.transition(
                    FSMState.COMPUTING,
                    reason="isfj_retrieve_done",
                )
            except IllegalTransitionError as exc:
                logger.warning(
                    f"[ISFJ:{self.persona.agent_id}] 检索后 RETRIEVING → "
                    f"COMPUTING 失败: {exc} · 释放令牌"
                )
                self._pending_retrieval_context = None
                await self._safe_release(token, fsm_target=FSMState.LISTENING)
                return None

            # ── 5. 调基类 attempt_speak 的 force_token 路径 ──
            # 基类 attempt_speak 会:
            #   - 检测 fsm 不在 LISTENING (因为我们已 COMPUTING) →
            #     基类的 transition(COMPUTING) 是 no-op (同状态自跃迁)
            #   - 然后正常推理 → router.invoke_chat_stream
            #   - prepare_extra_system_prefix 读取 _pending_retrieval_context
            #   - 推理结束自动 release token + 转 LISTENING
            #
            # ⚠️ 注意基类的 transition(LISTENING → COMPUTING) 在我们已是 COMPUTING 时
            # 为 no-op,不会抛错;但若中途状态被外部转走 (不太可能在持锁内),
            # 仍由基类的 IllegalTransition catch 处理
            try:
                result = await super().attempt_speak(
                    request_kind=request_kind,
                    force_token=token,
                )
                return result
            finally:
                # ── 6. 清理 _pending_retrieval_context (杜绝跨发言污染) ──
                self._pending_retrieval_context = None

    # ════════════════════════════════════════════════════════════
    # 检索核心
    # ════════════════════════════════════════════════════════════

    async def _perform_retrieval(self) -> Optional[str]:
        """
        执行 ChromaGateway 检索并构造可读的证据清单字符串。

        Returns:
            注入到 Prompt 的"可引用证据清单"块 (Markdown 友好)
            None - 检索为空 / Gateway 不可用
        """
        if not self._chroma.is_running:
            logger.debug(
                f"[ISFJ:{self.persona.agent_id}] ChromaGateway 未运行,跳过检索"
            )
            return None

        # 构造检索文本: 当前议题标题 + 描述 + 自身关键词
        topic = self._bus.current_topic()
        query_text = self._build_query_text(topic)
        if not query_text:
            return None

        # 仅检索当前议题的条目 (优先减小语义噪声)
        topic_id = topic.topic_id if topic is not None else None

        try:
            hits: list[QueryHit] = await self._chroma.query(
                query_text=query_text,
                n_results=_RETRIEVAL_TOP_K,
                topic_id=topic_id,
            )
        except Exception as exc:
            logger.warning(
                f"[ISFJ:{self.persona.agent_id}] chroma.query 异常: {exc!r}"
            )
            return None

        if not hits:
            # 当前议题为空 → 放宽到全库
            try:
                hits = await self._chroma.query(
                    query_text=query_text,
                    n_results=_RETRIEVAL_TOP_K,
                    topic_id=None,
                )
            except Exception as exc:
                logger.debug(
                    f"[ISFJ:{self.persona.agent_id}] chroma.query (全库) 异常: "
                    f"{exc!r}"
                )
                return None

        if not hits:
            return None

        return self._format_evidence_block(hits)

    def _build_query_text(self, topic: Optional[Topic]) -> str:
        """构造检索文本 · 议题为空时退化为关键词联合。"""
        parts: list[str] = []
        if topic is not None:
            if topic.title:
                parts.append(topic.title)
            if topic.description:
                # 描述长时截断,避免 query 文本过长
                parts.append(topic.description[:200])
        # 加入自身关键词 (帮助召回相关历史)
        parts.extend(self.personal_keywords[:4])
        # 去空 + 拼接
        joined = " ".join(p for p in parts if p)
        return joined.strip()

    def _format_evidence_block(self, hits: list[QueryHit]) -> str:
        """
        把检索结果格式化为 Prompt 可读的"证据清单"块。

        每条:
            [N] 来源 ⟨agent_id, kind=raw_turn|summary⟩,议题片段:"..."

        总长度受 _RETRIEVAL_BLOCK_MAX_CHARS 限制,超出则截断。
        """
        lines: list[str] = ["[可引用证据清单]"]
        total = len(lines[0])
        used = 0

        for i, hit in enumerate(hits, start=1):
            meta = hit.metadata or {}
            kind = str(meta.get("kind", "raw_turn"))
            source_agent = str(meta.get("agent_id", "?"))
            turn_id = str(meta.get("turn_id", ""))[:8]

            # 单条引用文本截断
            content = hit.content or ""
            if len(content) > _RETRIEVAL_PER_HIT_MAX_CHARS:
                content = (
                    content[: _RETRIEVAL_PER_HIT_MAX_CHARS // 2]
                    + "…[省略]…"
                    + content[-(_RETRIEVAL_PER_HIT_MAX_CHARS // 2) :]
                )

            kind_zh = "归档摘要" if kind == "summary" else "原始发言"
            line = (
                f"[{i}] 来源:{source_agent} ({kind_zh}, ref={turn_id}) "
                f"片段:\"{content}\""
            )

            # 总长度检查
            if total + len(line) > _RETRIEVAL_BLOCK_MAX_CHARS and used > 0:
                lines.append(
                    f"…(后续 {len(hits) - used} 条因长度限制省略)"
                )
                break
            lines.append(line)
            total += len(line)
            used += 1

        if used == 0:
            return "[可引用证据清单] (空) — 暂无可考据的历史记录。"

        return "\n".join(lines)

    # ════════════════════════════════════════════════════════════
    # 重写: 自己 Turn 封存后的回调 (职责完成释放)
    # ════════════════════════════════════════════════════════════

    async def on_own_turn_sealed(self, event: TurnSealedEvent) -> None:
        """
        ISFJ 完成考据发言后,获得"档案管理员的安心",Aggro 主动回退 -8。
        被打断时不释放 (因为引用未必呈现完整)。
        """
        await super().on_own_turn_sealed(event)

        if event.turn.truncation != TruncationReason.NORMAL:
            return

        try:
            relief = StimulusEvent(
                kind=StimulusKind.ARBITER_REJECTED,
                delta=_ISFJ_DUTY_DONE_DELTA,
                source_agent_id=None,
                note="ISFJ 考据发言完成释放",
            )
            await self._aggro_engine.apply_stimulus(
                agent_id=self.persona.agent_id,
                event=relief,
            )
            logger.debug(
                f"[ISFJ:{self.persona.agent_id}] 考据职责完成 "
                f"Δ={_ISFJ_DUTY_DONE_DELTA}"
            )
        except Exception as exc:
            logger.debug(
                f"[ISFJ:{self.persona.agent_id}] 释放回退异常 (忽略): {exc!r}"
            )


# ──────────────────────────────────────────────────────────────────────────────
# 工厂函数
# ──────────────────────────────────────────────────────────────────────────────


def build_isfj_archivist(
    *,
    aggro_engine: AggroEngine | None = None,
    context_bus: ContextBus | None = None,
    chroma_gateway: ChromaGateway | None = None,
) -> IsfjArchivistAgent:
    """构造 ISFJ_archivist 实例 (尚未 register_with_runtime)。"""
    return IsfjArchivistAgent(
        aggro_engine=aggro_engine,
        context_bus=context_bus,
        chroma_gateway=chroma_gateway,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 模块级公开 API
# ──────────────────────────────────────────────────────────────────────────────


__all__ = [
    "IsfjArchivistAgent",
    "build_isfj_archivist",
]