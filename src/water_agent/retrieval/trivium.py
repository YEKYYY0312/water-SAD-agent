"""TriviumDB 适配器：子块检索 + parent_id 回填父块上下文。

P0 阶段不要求真实向量库已就绪：当 ``db_path`` 不存在或打不开时自动回落到
fixture 检索，保证无真实数据也能完整跑通 RAG / Harness / CLI。
真实库就绪后会走 ``TriviumDB.search_hybrid`` 路径；一旦真实库检索链路失败，
不得静默退回 fixture，而应抛出结构化错误，由上层图/CLI 记录失败。
真实库 0 命中是合法结果（返回空列表，不回落、不抛错）。

文本索引有两条来源，``runtime_status.text_index_source`` 标明实际走了哪条：

- ``persisted``：库带 ``.text`` 侧文件（入库时 ``build_text_index`` + ``flush`` 产出）。
  此时以 ``access_mode="read_only"`` + ``load_text_index=True`` 打开，只读文件系统上
  也能检索，且不必重灌。
- ``rebuilt``：库没有侧文件，退回 read_write 打开并由 ``_rebuild_text_index`` 用 payload
  重灌（只读模式下 ``index_text`` 会被拒，所以这条路必须可写）。

两条都不成立时 ``search_hybrid`` 会退化为纯向量，因此 ``retrieve`` 在
``text_index_ready`` 为假时直接抛错，不静默降级。

P1 父子块语义
--------------
- 入库：仅子块（article/clause/item/table/paragraph/text_window）进向量+文本索引
- 检索：命中子块后按 ``parent_id`` 回填父块（chapter/section/subsection）上下文
- 返回文本：``子块.text + 父块上下文``，引用绑定子块 ``article``（如 GB50014-2021 4.4.2）
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from water_agent.domain.models import Chunk, RetrievedClause
from water_agent.retrieval.base import Retriever
from water_agent.retrieval.in_memory import InMemoryRetriever
from water_agent.retrieval.loader import load_clauses_jsonl

TRIVIUM_DIM = 1536
DEFAULT_HYBRID_ALPHA = 0.7
REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FIXTURE_PATH = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"
DEFAULT_INDEX_DIR = REPO_ROOT / "data" / "indexes"
INDEX_PATH_ENV = "WATER_AGENT_INDEX_PATH"

# 当前语料的权威索引。必须是具名常量而不是"目录下排序最后的那个"：`data/indexes/`
# 里同时存在 256 与 512 维两份索引，靠 ``sorted(...)[-1]`` 选中 512 纯属文件名巧合，
# 再放进一份 768 维就会**无声换库**（维度与 embedding.DEFAULT_DIMENSIONS 不符时
# 检索质量崩掉，但没有任何一处会报错）。改动这个常量时必须同步
# ``embedding.DEFAULT_DIMENSIONS`` / ``DEFAULT_INDEX_VERSION``。
DEFAULT_INDEX_NAME = "qwen3.7__512__curated.db"


class TriviumRetrievalError(RuntimeError):
    """真实 TriviumDB 检索失败时抛出。"""


#: 稳定错误码：当前 embedding 配置与索引建库时的不一致（P7-S10）。
RETRIEVAL_EMBEDDING_MISMATCH = "RETRIEVAL_EMBEDDING_MISMATCH"


class EmbeddingMismatchError(TriviumRetrievalError):
    """当前 embedding 模型 / 维度与索引建库时的不一致，或索引根本没有向量：拒绝做向量检索。

    换了 embedding 模型还用旧向量，检索会**静默**变差且没有任何报错——这是 P7-S10 最危险的失败模式，
    所以这里必须响亮。
    """

    code = RETRIEVAL_EMBEDDING_MISMATCH

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"{RETRIEVAL_EMBEDDING_MISMATCH}: {reason}")


# ---------------------------------------------------------------------------
# 索引元数据：建库时的 embedding 模型与维度（P7-S10）
# ---------------------------------------------------------------------------

INDEX_META_FORMAT = 1
INDEX_META_ABSENT = "absent"
INDEX_META_PRESENT = "present"
INDEX_META_INVALID = "invalid"
INDEX_META_NONE = "none"
MODE_HYBRID = "hybrid"
MODE_TEXT = "text"

#: 检索模式设置（P7-S10）：``auto`` = embedding 已配置且与索引匹配 → ``hybrid``，否则 ``text``；
#: ``hybrid`` = 强制混合（缺 embedding 或不匹配即失败）；``text`` = 强制关键词（BM25，不调用任何 embedding）。
RETRIEVAL_MODE_ENV = "WATER_AGENT_RETRIEVAL_MODE"
MODE_AUTO = "auto"
RETRIEVAL_MODE_SETTINGS = (MODE_AUTO, MODE_HYBRID, MODE_TEXT)
#: ``retrieval_mode_reason`` 的取值。text 模式**不是**语义检索，运行状态与界面必须如实标出原因。
REASON_FORCED = "forced"
REASON_EMBEDDING_NOT_CONFIGURED = "embedding_not_configured"
REASON_EMBEDDING_MISMATCH = "embedding_mismatch"
REASON_EMBEDDING_MATCHED = "embedding_matched"
REASON_FIXTURE = "fixture"


def retrieval_mode_setting(
    explicit: str | None = None, *, getenv: Callable[[str], str | None] = os.environ.get
) -> str:
    """检索模式设置：调用方显式给出 > ``WATER_AGENT_RETRIEVAL_MODE`` > ``auto``。取值不对即响亮失败。"""

    raw = (explicit or getenv(RETRIEVAL_MODE_ENV) or MODE_AUTO).strip().lower()
    if raw not in RETRIEVAL_MODE_SETTINGS:
        raise ValueError(f"{RETRIEVAL_MODE_ENV} 只能是 {'|'.join(RETRIEVAL_MODE_SETTINGS)}，得到 {raw!r}")
    return raw


def index_dimensions(db_path: str | Path) -> int:
    """库的实际维度（只读打开，不调用 embedding）。"""

    import triviumdb

    probe = triviumdb.TriviumDB(str(db_path), access_mode="read_only")
    try:
        return int(probe.dim())
    finally:
        probe.close()


class IndexMetaError(ValueError):
    """``<db>.index_meta.json`` 存在但读不出 / 字段不对：不知道向量是谁产的，就不能拿它做向量检索。"""


@dataclass(frozen=True, slots=True)
class IndexMeta:
    """索引旁 ``<db>.index_meta.json`` 的内容（``status=absent`` 时是旧索引的缺省假设）。"""

    embedding_model: str | None
    embedding_host: str | None
    dimensions: int
    index_version: str
    retrieval_modes: tuple[str, ...]
    status: str

    @property
    def has_vectors(self) -> bool:
        return MODE_HYBRID in self.retrieval_modes and self.embedding_model is not None


def index_meta_path(db_path: str | Path) -> Path:
    return Path(str(db_path) + ".index_meta.json")


def read_index_meta(db_path: str | Path, *, dimensions: int) -> IndexMeta:
    """读索引元数据。文件不存在 = P7-S10 之前建的旧索引：按百炼缺省模型（qwen3.7-text-embedding）对待。

    ``dimensions`` 是库的实际维度（``triviumdb.dim()``），旧索引的元数据假设用它。
    """

    from water_agent.generation.endpoints import (
        DEFAULT_EMBEDDING_BASE_URL,
        DEFAULT_EMBEDDING_MODEL,
        endpoint_host,
    )

    path = index_meta_path(db_path)
    if not path.exists():
        return IndexMeta(
            embedding_model=DEFAULT_EMBEDDING_MODEL,
            embedding_host=endpoint_host(DEFAULT_EMBEDDING_BASE_URL),
            dimensions=dimensions,
            index_version=f"{DEFAULT_EMBEDDING_MODEL}__{dimensions}__curated-v1",
            retrieval_modes=(MODE_HYBRID, MODE_TEXT),
            status=INDEX_META_ABSENT,
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("format") != INDEX_META_FORMAT:
            raise ValueError("unknown index_meta format")
        modes = tuple(str(mode) for mode in payload["retrieval_modes"])
        model = payload.get("embedding_model")
        meta = IndexMeta(
            embedding_model=str(model) if model else None,
            embedding_host=str(payload["embedding_host"]) if payload.get("embedding_host") else None,
            dimensions=int(payload["dimensions"]),
            index_version=str(payload["index_version"]),
            retrieval_modes=modes,
            status=INDEX_META_PRESENT,
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise IndexMetaError(f"{path.name} 读不出或字段不对：{exc}") from exc
    if not meta.retrieval_modes or not set(meta.retrieval_modes) <= {MODE_HYBRID, MODE_TEXT}:
        raise IndexMetaError(f"{path.name} 的 retrieval_modes 不合法：{list(meta.retrieval_modes)}")
    return meta


def embedding_mismatch(meta: IndexMeta, *, model: str, dimensions: int) -> str | None:
    """当前 embedding 配置能不能对这份索引做向量检索；不能时返回原因（模型或维度不一致、没有向量）。"""

    if not meta.has_vectors:
        return "索引是纯关键词索引（build_index.py --text-only），没有向量"
    if meta.embedding_model != model:
        return f"索引用 {meta.embedding_model} 建库，当前配置的 embedding 模型是 {model}（换模型必须重建索引）"
    if meta.dimensions != dimensions:
        return f"索引维度 {meta.dimensions}，当前 embedding 维度 {dimensions}"
    return None


def ensure_embedding_matches(db_path: str | Path, *, model: str, dimensions: int) -> IndexMeta:
    """向量检索的前置检查：索引元数据读不出、或与当前 embedding 配置不一致即 :class:`EmbeddingMismatchError`。"""

    try:
        meta = read_index_meta(db_path, dimensions=dimensions)
    except IndexMetaError as exc:
        raise EmbeddingMismatchError(str(exc)) from exc
    reason = embedding_mismatch(meta, model=model, dimensions=dimensions)
    if reason is not None:
        raise EmbeddingMismatchError(reason)
    return meta


class IndexSelectionError(RuntimeError):
    """索引目录非空，但选不出一个明确的索引时抛出。"""


def resolve_index_path(
    explicit: str | Path | None = None,
    *,
    getenv: Callable[[str], str | None] = os.environ.get,
    index_dir: Path | None = None,
    allow_default: bool = True,
) -> Path | None:
    """按显式优先级选出检索索引，选不出就报错而不是猜。

    优先级：调用方显式指定（CLI ``--db-path``）→ ``WATER_AGENT_INDEX_PATH``
    环境变量（部署侧覆盖）→ 目录下的 :data:`DEFAULT_INDEX_NAME`。

    返回 ``None`` 只代表**一份索引都没有**——这是 P0 的合法状态，由调用方回落到
    fixture 检索。目录里有索引却没有那份具名的，说明配置与产物不一致，抛
    :class:`IndexSelectionError`：此时静默改用别的索引会让维度、语料版本和
    ``baseline.json`` 三者对不上，比直接失败危险得多。

    ``allow_default=False`` 只关掉最后那层"目录下的默认索引"，前两层照旧生效：
    给"默认不该碰真实库、但显式配了就必须照用"的调用方用（mock 生成器，见
    ``runtime.agentic._resolve_db_path``）。这条分支刻意放在两层显式来源**之后**，
    否则部署侧设了 ``WATER_AGENT_INDEX_PATH`` 却拿到 fixture 答案，正是"无声降级"。
    """

    directory = DEFAULT_INDEX_DIR if index_dir is None else index_dir
    if explicit:
        path = Path(explicit)
        if not path.exists():
            raise IndexSelectionError(f"指定的索引不存在：{path}")
        return path
    configured = (getenv(INDEX_PATH_ENV) or "").strip()
    if configured:
        path = Path(configured)
        if not path.exists():
            raise IndexSelectionError(f"{INDEX_PATH_ENV} 指向的索引不存在：{path}")
        return path
    if not allow_default:
        return None
    if not directory.exists():
        return None
    default = directory / DEFAULT_INDEX_NAME
    if default.exists():
        return default
    available = sorted(p.name for p in directory.glob("*.db"))
    if not available:
        return None
    raise IndexSelectionError(
        f"{directory} 下没有默认索引 {DEFAULT_INDEX_NAME}，现有 {available}。"
        f"请设置 {INDEX_PATH_ENV} 显式指定，或重建默认索引——"
        "不自动改用其他索引：维度/语料版本不匹配会让检索无声劣化"
    )


class TriviumFallbackReason(str):
    """TriviumRetriever 当前回落原因。"""


class TriviumFallbackKind(str):
    """回落类型：显式 mock / fixture / none / real_error。"""


class TriviumRuntimeStatus(dict[str, Any]):
    """检索器运行态，供 CLI/harness 记录与诊断。"""



def mock_embed(text: str, dim: int = TRIVIUM_DIM) -> list[float]:
    """确定性伪嵌入：仅用于 P0/P1 离线验证与适配器自检，不替代真实嵌入模型。"""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    out: list[float] = []
    for i in range(dim):
        out.append((digest[i % len(digest)] / 255.0 - 0.5) * 2.0)
    return out



def _default_fixture_clauses() -> list[RetrievedClause]:
    """兜底 fixture；文件缺失时返回空列表而不是抛错。

    这是 fallback 的 fallback：fixture 自己不在位**不该**让检索器构造失败，否则
    部署里少一个 2KB 夹具就打死每一次问答。空列表的后果是检索返回空、由上层按
    "无依据"处理——``runtime_status`` 里 ``is_fallback``/``fallback_reason`` 照实
    暴露，不会假装检索成功。
    """

    clauses = []
    try:
        loaded = load_clauses_jsonl(DEFAULT_FIXTURE_PATH)
    except (FileNotFoundError, NotADirectoryError):
        return []
    for c in loaded:
        clauses.append(
            RetrievedClause(
                spec_id=c.spec_id,
                article=c.article,
                chapter=c.chapter,
                section=c.section,
                title=c.title,
                text=c.text,
            )
        )
    return clauses



def _parents_path(db_path: str | Path) -> Path:
    return Path(str(db_path) + ".parents.json")


def _text_index_path(db_path: str | Path) -> Path:
    """持久化文本索引的侧文件（``flush()`` 之后才存在）。"""

    return Path(str(db_path) + ".text")


def has_persisted_text_index(db_path: str | Path) -> bool:
    """库是否自带可加载的文本索引，决定能否以只读方式打开。

    只看"存在且非空"，完整性交给 triviumdb 自己校验：实测只读打开会用 ``.flush_ok``
    拒绝不完整的 generation。刻意**不**用"``hybrid_alpha=0`` 与 ``=1`` 命中是否相同"
    这类行为探测——节点数少于 ``top_k`` 时两边必然相同，会把健康的小库误判成坏的。
    """

    sidecar = _text_index_path(db_path)
    try:
        return sidecar.is_file() and sidecar.stat().st_size > 0
    except OSError:
        return False



def _friendly_parent_label(chunk: Chunk) -> str:
    if chunk.chunk_type == "chapter" and chunk.chapter:
        if chunk.title and not chunk.title.startswith("第"):
            return chunk.title
        return f"第{chunk.chapter}章"
    if chunk.chunk_type in {"section", "subsection"}:
        # 标题型无条号父块：chapter/section 常为哈希，不应直接展示给用户
        if chunk.chapter and re.fullmatch(r"[0-9a-f]{8}", chunk.chapter) and chunk.section and re.fullmatch(r"[0-9a-f]{16}", chunk.section):
            if chunk.title:
                plain_title = re.sub(r"^第[一二三四五六七八九十百千0-9]+章\s*", "", chunk.title).strip()
                return f"标题块 / {plain_title or chunk.title} / {chunk.chapter}"
            return f"标题块 / {chunk.chapter}"
        if chunk.title and not re.fullmatch(r"[\d.]+", chunk.title.strip()):
            return chunk.title
        if chunk.chapter and chunk.section:
            return f"{chunk.chapter} / {chunk.section}"
    return chunk.title or chunk.section or chunk.chapter or "标题块"


class TriviumRetriever(Retriever):
    """基于 TriviumDB 的混合检索适配器，失败时回落到 fixture 或抛错。"""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        fallback_clauses: list[RetrievedClause] | None = None,
        parent_store: dict[str, Chunk] | None = None,
        embedding_fn: Callable[[str], list[float]] | None = None,
        hybrid_alpha: float = DEFAULT_HYBRID_ALPHA,
        dim: int | None = None,
        retrieval_mode: str = MODE_HYBRID,
        retrieval_mode_reason: str = "",
        retrieval_mode_detail: str | None = None,
    ) -> None:
        if retrieval_mode not in (MODE_HYBRID, MODE_TEXT):
            raise ValueError(f"retrieval_mode 只能是 hybrid|text，得到 {retrieval_mode!r}")
        self.db_path = Path(db_path) if db_path is not None else None
        #: text 模式（P7-S10）：关键词检索（BM25），``search_hybrid(零向量, hybrid_alpha=0.0)``，
        #: **一次 embedding 都不调用**（含维度探测）。
        self._retrieval_mode = retrieval_mode
        self._retrieval_mode_reason = retrieval_mode_reason
        #: auto 因 embedding 与索引不匹配而降级时的具体原因（模型名 / 维度，不含 Key）
        self._retrieval_mode_detail = retrieval_mode_detail
        self._embedding_fn = embedding_fn or mock_embed
        self._hybrid_alpha = 0.0 if retrieval_mode == MODE_TEXT else hybrid_alpha
        self._fallback = InMemoryRetriever(fallback_clauses or _default_fixture_clauses())
        self._parent_store: dict[str, Chunk] = parent_store or self._load_parent_store()
        self._db = None
        self._db_error: str | None = None
        self._db_opened = False
        self._access_mode = "read_write"
        self._text_index_source = "none"
        self._fallback_kind: str = "fixture" if self.db_path is None or not self.db_path.exists() else "none"
        self._fallback_reason: str = "db path missing" if self.db_path is None else ("db path not found" if not self.db_path.exists() else "")
        self._used_fallback = self._fallback_kind != "none"
        open_dim = dim
        if self.db_path is not None and self.db_path.exists():
            try:
                import triviumdb

                if open_dim is None and retrieval_mode == MODE_TEXT:
                    # text 模式不碰 embedding：维度直接读库
                    open_dim = index_dimensions(self.db_path)
                # 未显式指定时，用 query 向量实际维度推断（写读一致）
                if open_dim is None:
                    try:
                        probe_len = len(self._embedding_fn("维度探测"))
                    except Exception:  # noqa: BLE001 — 探测失败回退默认维度
                        probe_len = 0
                    open_dim = probe_len or 1536
                # 只读优先。TriviumRetriever 从不改库，而 triviumdb 默认的
                # ``access_mode="read_write"`` 在**打开阶段**就要求文件可写、``close()``
                # 还要求目录可写——容器以只读根文件系统运行时（compose 的
                # ``read_only: true``）必然失败。实测 ``access_mode="read_only"``
                # + ``load_text_index=True`` 在 444 文件 / 555 目录上可正常检索，
                # 且命中与 read_write + 进程内重建**逐位一致**。
                # 没有 ``.text`` 侧文件的库只能走 read_write 重建（只读模式下
                # ``index_text`` 被拒），行为与此前保持一致。
                if has_persisted_text_index(self.db_path):
                    self._db = triviumdb.TriviumDB(
                        str(self.db_path),
                        dim=open_dim,
                        access_mode="read_only",
                        load_text_index=True,
                    )
                    self._access_mode = "read_only"
                else:
                    self._db = triviumdb.TriviumDB(str(self.db_path), dim=open_dim)
                    self._access_mode = "read_write"
                self._db_opened = True
                self._used_fallback = False
            except Exception as exc:  # noqa: BLE001 — pragma: no cover — triviumdb 打开失败降级 fixture
                self._db_error = str(exc)
                self._db = None
                self._fallback_kind = "fixture"
                self._fallback_reason = f"real db open failed: {exc}"
                self._used_fallback = True
        self._index_meta = self._index_meta_status(dim)
        self._dim = open_dim if self._db is not None else dim
        self._text_index_ready = False
        self._text_indexed_nodes = 0
        if self._db is not None:
            if self._access_mode == "read_only":
                # 侧文件已随库加载，无需重灌；``text_indexed_nodes`` 保持 0 表示
                # "本进程没有重建过"，真实来源看 ``text_index_source``。
                self._text_index_ready = True
                self._text_index_source = "persisted"
            else:
                self._rebuild_text_index()
                if self._text_index_ready:
                    self._text_index_source = "rebuilt"

    def _index_meta_status(self, dim: int | None) -> str:
        """索引旁有没有 ``index_meta.json``、读不读得出（只报状态；匹配与否由装配方判定）。"""

        if self._db is None or self.db_path is None:
            return INDEX_META_NONE
        try:
            return read_index_meta(self.db_path, dimensions=dim or 0).status
        except IndexMetaError:
            return INDEX_META_INVALID

    @property
    def fallback_kind(self) -> str:
        return self._fallback_kind

    @property
    def fallback_reason(self) -> str:
        return self._fallback_reason

    def _rebuild_text_index(self) -> None:
        """没有 ``.text`` 侧文件时，用 payload 在进程内重灌文本索引。

        ``index_text`` + ``build_text_index`` 的结果不会**自动**落盘：重开后
        ``search_hybrid`` 的文本分量恒为空——``hybrid_alpha`` 形同虚设、退化为纯向量。
        （紧跟一次 ``flush()`` 才会写出 ``.text`` 侧文件，那条路见模块说明的
        ``persisted``。）因此这里用 payload 里的 ``text`` 重灌一遍（3k 节点 <0.1s）。
        失败不静默：记入 ``db_error`` 并让 ``retrieve`` 抛 ``TriviumRetrievalError``。
        """
        assert self._db is not None
        try:
            count = 0
            for node_id in self._db.all_node_ids():
                payload = self._db.get_payload(int(node_id)) or {}
                text = payload.get("text") or ""
                if text:
                    self._db.index_text(int(node_id), text)
                    count += 1
            self._db.build_text_index()
            self._text_index_ready = True
            self._text_indexed_nodes = count
        except Exception as exc:  # noqa: BLE001 — 文本索引失败必须可观测，不能退化为纯向量
            self._db_error = f"text index rebuild failed: {exc}"
            self._text_index_ready = False

    @property
    def text_index_ready(self) -> bool:
        """真实库的文本索引是否已在本进程重建（False 时 retrieve 会拒绝服务）。"""
        return self._text_index_ready

    @property
    def parent_store(self) -> dict[str, Chunk]:
        return self._parent_store

    def _load_parent_store(self) -> dict[str, Chunk]:
        if self.db_path is None:
            return {}
        path = _parents_path(self.db_path)
        if not path.exists():
            self._db_error = f"parent_store missing: {path}"
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return {chunk_id: Chunk.model_validate(data) for chunk_id, data in payload.items()}
        except Exception as exc:  # noqa: BLE001 — parent_store 损坏时降级为空映射
            self._db_error = f"parent_store load failed: {exc}"
            return {}

    @property
    def is_fallback(self) -> bool:
        return self._used_fallback and self._db is None

    @property
    def db_error(self) -> str | None:
        return self._db_error

    @property
    def runtime_status(self) -> dict[str, Any]:
        """统一暴露真实库/显式 fallback 的运行态，供 CLI/harness 上报。"""
        return {
            "db_path": str(self.db_path) if self.db_path is not None else None,
            "is_fallback": self.is_fallback,
            "fallback_kind": self._fallback_kind,
            "fallback_reason": self._fallback_reason,
            "db_error": self._db_error,
            "text_index_ready": self._text_index_ready,
            "text_indexed_nodes": self._text_indexed_nodes,
            "text_index_source": self._text_index_source,
            "access_mode": self._access_mode,
            "db_opened": self._db_opened,
            "index_meta": self._index_meta,
            **self.retrieval_mode_status,
        }

    @property
    def retrieval_mode_status(self) -> dict[str, str | None]:
        """本检索器**实际**在用的检索模式与原因。没有真实索引（fixture 兜底）时是 fixture 语料上的关键词匹配。"""

        if self._db is None:
            return {
                "retrieval_mode": MODE_TEXT,
                "retrieval_mode_reason": REASON_FIXTURE,
                "retrieval_mode_detail": None,
            }
        return {
            "retrieval_mode": self._retrieval_mode,
            "retrieval_mode_reason": self._retrieval_mode_reason,
            "retrieval_mode_detail": self._retrieval_mode_detail,
        }

    def _enrich_with_parent(self, clause: RetrievedClause, payload: dict) -> RetrievedClause:
        parent_id = payload.get("parent_id")
        if not parent_id:
            return clause
        parent = self._parent_store.get(str(parent_id))
        if parent is None:
            return clause
        if parent.chunk_type in {"section", "subsection"} and parent.chapter and parent.section:
            if re.fullmatch(r"[0-9a-f]{8}", parent.chapter) and re.fullmatch(r"[0-9a-f]{16}", parent.section):
                prefix = _friendly_parent_label(parent)
            else:
                prefix_parts = [p for p in [parent.chapter, parent.section, _friendly_parent_label(parent)] if p]
                prefix = " / ".join(dict.fromkeys(prefix_parts))
        else:
            prefix = " / ".join([p for p in [parent.chapter, parent.section, _friendly_parent_label(parent)] if p])
        if prefix:
            clause.text = f"【{prefix}】\n{clause.text}"
            if not clause.chapter and parent.chapter and not re.fullmatch(r"[0-9a-f]{8}", parent.chapter):
                clause.chapter = parent.chapter
            if not clause.section and parent.section and not re.fullmatch(r"[0-9a-f]{16}", parent.section):
                clause.section = parent.section
        return clause

    def retrieve(self, query: str, top_k: int = 5) -> list[RetrievedClause]:
        if not query or not query.strip():
            return []
        if self._db is None:
            return self._fallback.retrieve(query, top_k=top_k)
        if not self._text_index_ready:
            raise TriviumRetrievalError(
                f"text index not ready for db={self.db_path}: {self._db_error or 'unknown'}"
            )
        if self._retrieval_mode == MODE_TEXT:
            # 关键词模式：零向量 + hybrid_alpha=0.0，排序只来自文本索引（BM25），不调用 embedding
            qvec = [0.0] * int(self._dim or 0)
        else:
            try:
                qvec = self._embedding_fn(query)
            except Exception as exc:
                self._db_error = str(exc)
                raise TriviumRetrievalError(f"embedding failed for query={query!r}: {exc}") from exc

        try:
            hits = self._db.search_hybrid(qvec, query, top_k=top_k, hybrid_alpha=self._hybrid_alpha)
        except Exception as exc:
            self._db_error = str(exc)
            raise TriviumRetrievalError(f"search_hybrid failed for query={query!r}: {exc}") from exc

        out: list[RetrievedClause] = []
        for hit in hits:
            payload = getattr(hit, "payload", {}) or {}
            chunk_type = payload.get("chunk_type")
            try:
                if chunk_type:
                    chunk = Chunk.model_validate(payload)
                    clause = RetrievedClause(
                        spec_id=chunk.spec_id,
                        article=chunk.article or "",
                        chapter=chunk.chapter,
                        section=chunk.section,
                        title=chunk.title,
                        text=chunk.text,
                    )
                else:
                    clause = RetrievedClause.model_validate(payload)
            except Exception as exc:
                self._db_error = str(exc)
                raise TriviumRetrievalError(f"payload parse failed for query={query!r}: {exc}") from exc
            clause.score = float(getattr(hit, "score", 0.0) or 0.0)
            clause = self._enrich_with_parent(clause, payload)
            out.append(clause)
        # 0 命中是合法结果（语料里没有相关条款），交给上层报"检索无结果"；
        # 这里绝不回落 fixture（is_fallback 保持 False），也不当成链路故障抛错。
        return out

    def close(self) -> None:
        if self._db is not None:
            try:
                self._db.close()
            except Exception:  # noqa: BLE001, S110 — 关闭失败无需上报
                pass
