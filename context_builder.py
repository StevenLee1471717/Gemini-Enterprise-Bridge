"""Multi-turn prompt construction, model tracking, and agentic conversation delta tracking."""

from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from schemas import ChatMessage, estimate_tokens, model_requests_new_chat, resolve_target_web_model
from agent_policy import (CARBON_AUTO_COMPACT_TOKENS, CARBON_CONTEXT_WINDOW, CARBON_EFFECTIVE_CONTEXT_PERCENT,
                          CODING_WORKFLOW, COMPACTION_INSTRUCTIONS,
                          has_coding_tools, reading_diagnostics, reading_reminder, deduplicate_tool_text,
                          upgrade_desktop_agent_instructions)
from codex_activity import lookup_codex_thread_activity, resolve_slot_activity
from tool_calling import (
    build_response_format_instructions,
    build_tool_delta_reminder,
    build_tool_system_instructions,
    compute_tools_fingerprint,
    format_assistant_history_entry,
    format_tool_result_entry,
    wire_history_entry,
    is_tool_generation_enabled,
)

_CALL_ID_IN_CANON_RE = re.compile(r'"id":\s*"[^"]*",\s*')
_EPHEMERAL_WRAPPER_RE = re.compile(
    r"<(?P<tag>system_reminder|system-reminder|current_datetime|app_user_message_source_json|canvas-context|environment_details|context_info)\b[^>]*>[\s\S]*?</(?P=tag)>",
    re.IGNORECASE,
)
_WHITESPACE_RUN_RE = re.compile(r"\s+")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_METADATA_BLOCK_RE = re.compile(
    r"<(?P<tag>INSTRUCTIONS|environment_context|permissions\s+instructions|collaboration_mode|"
    r"skills_instructions|turn_aborted|image_resize_notice|codex_apps_client_time_context|"
    r"app-context|system_reminder|system-reminder|current_datetime|"
    r"app_user_message_source_json|canvas-context|environment_details|context_info)\b[^>]*>[\s\S]*?</(?P=tag)>",
    re.IGNORECASE,
)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_VERTEX_SESSION_PATH_RE = re.compile(
    r"/(?:r/session|session|c)/([^/?#]+)",
    re.IGNORECASE,
)

_CODEX_INDEX_CACHE: Dict[str, Any] = {"sig": None, "titles": {}}
_CODEX_SQLITE_CACHE: Dict[str, Tuple[float, str]] = {}


def extract_vertex_session_id(raw_url: str) -> str:
    """Extract the concrete Gemini Enterprise session identifier from a Vertex URL, if present."""
    if not raw_url or not isinstance(raw_url, str):
        return ""
    try:
        parsed = urlparse(raw_url.strip())
    except Exception:
        return ""
    host = (parsed.netloc or "").lower()
    if host and "vertexaisearch.cloud.google.com" not in host:
        return ""
    match = _VERTEX_SESSION_PATH_RE.search(parsed.path or "")
    return match.group(1).strip() if match and match.group(1) else ""


def clean_prompt_to_title(raw_text: str, max_chars: int = 64) -> str:
    """Extract a clean, human-readable topic summary from a raw user message."""
    if not raw_text:
        return ""
    text = html.unescape(raw_text).replace("\xa0", " ")
    text = _METADATA_BLOCK_RE.sub(" ", text).strip()
    if not text:
        return ""

    lowered = text.lower()
    if (
        lowered.startswith("# agents.md instructions")
        or lowered.startswith("[system instructions]")
        or lowered.startswith("[previous session summary]")
        or lowered.startswith("[tool_result]")
    ):
        return ""
    if COMPACTION_INSTRUCTIONS.strip()[:36].lower() in lowered:
        return ""
    if "这是一次连通性体检" in text:
        return "一键模型连通体检"

    if "## My request:" in text:
        text = text.split("## My request:", 1)[1].strip()
    elif "[Current User Request]" in text:
        text = text.rsplit("[Current User Request]", 1)[1].strip()

    text = _MD_LINK_RE.sub(r"\1", text)
    for raw_line in text.splitlines():
        line = re.sub(r"^[#>\-*+\s]+", "", raw_line).strip()
        if not line:
            continue
        if line.startswith("```") or line.startswith("{") or line.startswith("["):
            continue
        line = _WHITESPACE_RUN_RE.sub(" ", line).strip()
        if len(line) > max_chars:
            return line[: max_chars - 1].rstrip() + "…"
        return line
    return ""


def _extract_compaction_summary_info(dialogue: Sequence[Tuple[str, str]]) -> Tuple[int, str]:
    """Count inline compaction checkpoint messages and fingerprint the latest summary."""
    summaries = [
        text
        for role, text in dialogue
        if role == "user"
        and (
            text.startswith("[Previous session summary]")
            or text.startswith("Another language model started to solve this problem and produced a summary")
        )
    ]
    if not summaries:
        return (0, "")
    fp = hashlib.sha1(summaries[-1].encode("utf-8", errors="ignore")).hexdigest()[:16]
    return (len(summaries), fp)


def is_compaction_prompt_text(raw_text: str) -> bool:
    """Return True when a trailing user message is an automated context compaction prompt."""
    if not raw_text:
        return False
    text = raw_text.strip()
    tail = text
    if "[Current User Request]" in tail:
        tail = tail.rsplit("[Current User Request]", 1)[1].strip()
    elif "## My request:" in tail:
        tail = tail.split("## My request:", 1)[1].strip()
    lower_tail = tail.lower()
    compact_head = COMPACTION_INSTRUCTIONS.strip()[:36].lower()
    if compact_head and compact_head in lower_tail:
        return True
    if lower_tail.startswith("you are performing a context checkpoint compaction"):
        return True
    if (
        "you are performing a context checkpoint compaction" in lower_tail
        and "handoff summary" in lower_tail
    ):
        return True
    return False


def is_compaction_dialogue(dialogue: Sequence[Tuple[str, str]]) -> bool:
    """Return True if normalized dialogue ends with a context compaction instruction."""
    return bool(
        dialogue
        and dialogue[-1][0] == "user"
        and is_compaction_prompt_text(dialogue[-1][1])
    )


def is_completed_compaction_turns(turns: Sequence[Tuple[str, str]]) -> bool:
    """Return True if archived/tracked turns represent a finished one-off compaction pass."""
    return bool(
        len(turns) >= 2
        and turns[-1][0] == "assistant"
        and is_compaction_dialogue(turns[:-1])
    )


def extract_summary_anchor(summary_text: str) -> str:
    """Extract a distinctive normalized snippet from a compaction summary for handoff matching."""
    if not summary_text:
        return ""
    lines: List[str] = []
    for raw_line in summary_text.splitlines():
        s = raw_line.strip()
        if not s:
            continue
        low = s.lower()
        if low.startswith("i will transfer ") or low.startswith("[[section:"):
            continue
        if low in {"## context checkpoint handoff summary", "# context checkpoint handoff summary", "[previous session summary]"}:
            continue
        lines.append(s)
        if sum(len(x) for x in lines) >= 140:
            break
    joined = _WHITESPACE_RUN_RE.sub(" ", " ".join(lines)).strip()
    return joined[:120] if len(joined) >= 24 else ""


def extract_dialogue_title(dialogue: Sequence[Tuple[str, str]]) -> str:
    """Find the first real user topic in normalized dialogue, handling compaction prompts."""
    if not dialogue:
        return ""
    is_compacting = is_compaction_dialogue(dialogue)
    scan_items = dialogue[:-1] if is_compacting else dialogue
    found = ""
    for role, content in scan_items:
        if role != "user":
            continue
        candidate = clean_prompt_to_title(content)
        if candidate:
            found = candidate
            break
    if is_compacting:
        return f"自动整理记忆 · {found}" if found else "自动整理对话记忆"
    return found


def lookup_codex_thread_title(
    session_id: Optional[str],
    codex_home: Optional[Path] = None,
) -> str:
    """Read-only lookup of a local Codex thread title by UUID (`prompt_cache_key`)."""
    sid = (session_id or "").strip()
    if not sid or not _UUID_RE.match(sid):
        return ""
    root = codex_home or (Path.home() / ".codex")

    # 1) Fast mtime-cached lookup in ~/.codex/session_index.jsonl
    try:
        idx_path = root / "session_index.jsonl"
        if idx_path.is_file():
            st = idx_path.stat()
            sig = (str(idx_path), st.st_mtime_ns, st.st_size)
            if _CODEX_INDEX_CACHE.get("sig") != sig:
                parsed_map: Dict[str, str] = {}
                for raw_line in idx_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    line = raw_line.strip()
                    if not line or "thread_name" not in line:
                        continue
                    try:
                        obj = json.loads(line)
                        tid = str(obj.get("id") or "").strip()
                        tname = str(obj.get("thread_name") or "").strip()
                        if tid and tname:
                            parsed_map[tid] = tname
                    except Exception:
                        continue
                _CODEX_INDEX_CACHE["sig"] = sig
                _CODEX_INDEX_CACHE["titles"] = parsed_map
            cached_title = (_CODEX_INDEX_CACHE.get("titles") or {}).get(sid, "")
            if cached_title:
                return cached_title
    except Exception:
        pass

    # 2) Read-only fallback in ~/.codex/state_5.sqlite (with 10s TTL per thread ID)
    now = time.monotonic()
    cache_key = f"{root}:{sid}"
    cached_entry = _CODEX_SQLITE_CACHE.get(cache_key)
    if cached_entry is not None and (now - cached_entry[0]) < 10.0:
        return cached_entry[1]

    resolved = ""
    try:
        db_path = root / "state_5.sqlite"
        if db_path.is_file():
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.05)
            try:
                row = con.execute(
                    "SELECT name, title FROM threads WHERE id = ? LIMIT 1",
                    (sid,),
                ).fetchone()
                if row:
                    short_name = str(row[0] or "").strip()
                    raw_first = str(row[1] or "").strip()
                    resolved = short_name or clean_prompt_to_title(raw_first)
            finally:
                con.close()
    except Exception:
        resolved = ""

    _CODEX_SQLITE_CACHE[cache_key] = (now, resolved)
    return resolved


def clean_web_tab_title(raw_title: str, max_chars: int = 56) -> str:
    """Clean a Gemini Enterprise browser tab title and filter out prompt/error noise."""
    if not raw_title:
        return ""
    t = _WHITESPACE_RUN_RE.sub(" ", str(raw_title)).strip()
    t = re.sub(r"\s*[-·|]\s*Gemini Enterprise\s*$", "", t, flags=re.IGNORECASE).strip()
    if not t:
        return ""
    low = t.lower()
    noise_prefixes = (
        "gemini enterprise",
        "vertex ai",
        "google",
        "new chat",
        "[system instructions]",
        "you are codex",
        "# agents.md",
        "<instructions>",
        "<environment_context>",
        "original_stream_",
        "tool_call",
        "[tool_result]",
        "tool stream disconnected",
        "stream disconnected",
    )
    if any(low.startswith(p) for p in noise_prefixes):
        return ""
    if "you are running as a coding agent" in low or "original_stream_" in low:
        return ""
    if len(t) > max_chars:
        return t[: max_chars - 1].rstrip() + "…"
    return t


def resolve_slot_session_title(
    ws: Dict[str, Any],
    prompt_title: str = "",
    turn_count: int = 0,
    codex_home: Optional[Path] = None,
) -> Tuple[str, str]:
    """Resolve (title, source) for a worker slot using Codex -> live prompt -> clean web title."""
    pinned = str(ws.get("pinned_session_id") or "").strip()
    if pinned.startswith("image_"):
        return (prompt_title or "图片生成与编辑任务", "task")
    if pinned.startswith("video_"):
        return (prompt_title or "视频生成任务", "task")
    if pinned.startswith("audio_"):
        return (prompt_title or "语音转写任务", "task")

    codex_title = lookup_codex_thread_title(pinned, codex_home=codex_home)
    if codex_title:
        return (codex_title, "codex")

    if prompt_title:
        return (prompt_title, "prompt")

    has_gateway_activity = (
        bool(ws.get("busy"))
        or int(ws.get("waiting_count") or 0) > 0
        or bool(pinned)
        or turn_count > 0
        or int(ws.get("completed_requests") or 0) > 0
    )
    if has_gateway_activity:
        web_title = clean_web_tab_title(str(ws.get("tab_title") or ""))
        if web_title:
            return (web_title, "web")
    return ("", "")


def _canonicalize_for_match(role: str, text: str) -> str:
    """Strip volatile per-turn client metadata tags and normalize whitespace for prefix comparison."""
    if not text:
        return ""
    cleaned = _EPHEMERAL_WRAPPER_RE.sub("", text)
    if role == "assistant" and "[TOOL_CALL]" in cleaned:
        cleaned = _CALL_ID_IN_CANON_RE.sub("", cleaned)
    return _WHITESPACE_RUN_RE.sub(" ", cleaned).strip()


from multimodal import NormalizedAttachment, deduplicate_attachments


@dataclass
class PreparedPrompt:
    prompt: str
    reset_chat: bool
    strategy_used: str
    normalized_history: List[Tuple[str, str]]
    target_model: Optional[str] = None
    tools_fingerprint: str = ""
    system_fingerprint: str = ""
    attachments: List[NormalizedAttachment] = field(default_factory=list)


@dataclass
class SessionRecoveryNotice:
    kind: str = "interrupted"
    reason: str = ""


@dataclass
class PendingCompactionCleanup:
    slot_key: str
    compaction_seq: int
    turn_seq: int
    vertex_session_id: str = ""
    origin_session_id: Optional[str] = None
    compaction_token: Optional[str] = None
    summary_anchor: str = ""
    pre_compact_turn_count: int = 0
    completed_at: float = 0.0
    delivered: bool = False
    force_superseded: bool = False
    tenant_id: str = "host"


@dataclass
class ArchivedSessionRecord:
    archive_key: str
    explicit_session_id: Optional[str]
    vertex_session_id: str
    session_url: str
    session_title: str
    active_model: Optional[str]
    tools_fp: str
    system_fp: str
    turns: List[Tuple[str, str]]
    metrics: Dict[str, Any]
    updated_at: float
    tenant_id: str = "host"


class SessionArchiveStore:
    """SQLite-backed L2 archive mapping evicted/historical Codex sessions to Gemini `/r/session/<id>` URLs."""

    MAX_ENTRIES = 300

    def __init__(self, archive_path: str | Path | None = None, ttl_seconds: float = 30 * 86400.0) -> None:
        self.ttl_seconds = max(300.0, float(ttl_seconds))
        self._lock = RLock()
        db_target = ":memory:"
        if archive_path:
            p = Path(archive_path)
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            db_target = str(p)
        self._db = sqlite3.connect(db_target, check_same_thread=False)
        if db_target != ":memory:":
            try:
                Path(db_target).chmod(0o600)
            except OSError:
                pass
        with self._lock, self._db:
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS session_archive (
                    archive_key TEXT PRIMARY KEY,
                    explicit_session_id TEXT,
                    vertex_session_id TEXT NOT NULL,
                    session_url TEXT NOT NULL,
                    session_title TEXT NOT NULL DEFAULT '',
                    active_model TEXT,
                    tools_fp TEXT NOT NULL DEFAULT '',
                    system_fp TEXT NOT NULL DEFAULT '',
                    turns_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'host'
                )
                """
            )
            existing_cols = {
                str(r[1]) for r in self._db.execute("PRAGMA table_info(session_archive)").fetchall()
            }
            if "tenant_id" not in existing_cols:
                self._db.execute(
                    "ALTER TABLE session_archive ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'host'"
                )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_sa_explicit_sid ON session_archive(explicit_session_id)"
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_sa_vertex_sid ON session_archive(vertex_session_id)"
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_sa_updated_at ON session_archive(updated_at DESC)"
            )

    @staticmethod
    def _row_to_record(row: tuple) -> Optional[ArchivedSessionRecord]:
        try:
            raw_turns = json.loads(row[8] or "[]")
            turns = [
                (str(item[0]), str(item[1]))
                for item in raw_turns
                if isinstance(item, (list, tuple)) and len(item) == 2
            ]
            metrics = json.loads(row[9] or "{}")
            if not isinstance(metrics, dict):
                metrics = {}
        except Exception:
            return None
        if not turns:
            return None
        return ArchivedSessionRecord(
            archive_key=str(row[0]),
            explicit_session_id=str(row[1]) if row[1] else None,
            vertex_session_id=str(row[2]),
            session_url=str(row[3]),
            session_title=str(row[4] or ""),
            active_model=str(row[5]) if row[5] else None,
            tools_fp=str(row[6] or ""),
            system_fp=str(row[7] or ""),
            turns=turns,
            metrics=metrics,
            updated_at=float(row[10] or 0.0),
            tenant_id=str(row[11] or "host") if len(row) > 11 and row[11] else "host",
        )

    def _prune_locked(self, now: float) -> None:
        self._db.execute(
            "DELETE FROM session_archive WHERE updated_at < ?",
            (now - self.ttl_seconds,),
        )
        overflow = self._db.execute(
            "SELECT archive_key FROM session_archive ORDER BY updated_at DESC LIMIT -1 OFFSET ?",
            (self.MAX_ENTRIES,),
        ).fetchall()
        if overflow:
            self._db.executemany(
                "DELETE FROM session_archive WHERE archive_key = ?",
                [(r[0],) for r in overflow],
            )

    def upsert(
        self,
        *,
        session_url: str,
        turns: Sequence[Tuple[str, str]],
        explicit_session_id: Optional[str] = None,
        session_title: str = "",
        active_model: Optional[str] = None,
        tools_fp: str = "",
        system_fp: str = "",
        metrics: Optional[Dict[str, Any]] = None,
        updated_at: Optional[float] = None,
        tenant_id: str = "host",
    ) -> Optional[str]:
        vsid = extract_vertex_session_id(session_url)
        if not vsid or not turns:
            return None
        sid = explicit_session_id.strip() if isinstance(explicit_session_id, str) and explicit_session_id.strip() else None
        eff_tenant = (tenant_id or "host").strip() or "host"
        now = float(updated_at if updated_at is not None else time.time())
        with self._lock, self._db:
            # Preserve previously learned explicit_session_id if updating the same Vertex session without one
            if not sid:
                prev_row = self._db.execute(
                    "SELECT explicit_session_id FROM session_archive WHERE vertex_session_id = ? AND tenant_id = ? LIMIT 1",
                    (vsid, eff_tenant),
                ).fetchone()
                if prev_row and prev_row[0]:
                    sid = str(prev_row[0])
            archive_key = f"sid:{sid}" if sid else f"vsid:{vsid}"
            self._db.execute(
                "DELETE FROM session_archive WHERE vertex_session_id = ? AND archive_key != ?",
                (vsid, archive_key),
            )
            if sid:
                self._db.execute(
                    "DELETE FROM session_archive WHERE explicit_session_id = ? AND archive_key != ?",
                    (sid, archive_key),
                )
            turns_json = json.dumps([[r, t] for r, t in turns], ensure_ascii=False)
            metrics_json = json.dumps(metrics or {}, ensure_ascii=False)
            self._db.execute(
                """
                INSERT OR REPLACE INTO session_archive (
                    archive_key, explicit_session_id, vertex_session_id, session_url,
                    session_title, active_model, tools_fp, system_fp,
                    turns_json, metrics_json, updated_at, tenant_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    archive_key,
                    sid,
                    vsid,
                    session_url.strip(),
                    (session_title or "").strip(),
                    active_model,
                    tools_fp or "",
                    system_fp or "",
                    turns_json,
                    metrics_json,
                    now,
                    eff_tenant,
                ),
            )
            self._prune_locked(now)
        return archive_key

    def get_by_explicit_session_id(
        self,
        explicit_session_id: Optional[str],
        *,
        tenant_id: Optional[str] = None,
    ) -> Optional[ArchivedSessionRecord]:
        if not explicit_session_id or not explicit_session_id.strip():
            return None
        sid = explicit_session_id.strip()
        with self._lock, self._db:
            self._prune_locked(time.time())
            if tenant_id:
                row = self._db.execute(
                    """
                    SELECT archive_key, explicit_session_id, vertex_session_id, session_url,
                           session_title, active_model, tools_fp, system_fp,
                           turns_json, metrics_json, updated_at, tenant_id
                    FROM session_archive
                    WHERE explicit_session_id = ? AND tenant_id = ?
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (sid, tenant_id),
                ).fetchone()
            else:
                row = self._db.execute(
                    """
                    SELECT archive_key, explicit_session_id, vertex_session_id, session_url,
                           session_title, active_model, tools_fp, system_fp,
                           turns_json, metrics_json, updated_at, tenant_id
                    FROM session_archive
                    WHERE explicit_session_id = ?
                    ORDER BY updated_at DESC LIMIT 1
                    """,
                    (sid,),
                ).fetchone()
        return self._row_to_record(row) if row else None

    def get_by_vertex_session_id(self, vertex_session_id: Optional[str]) -> Optional[ArchivedSessionRecord]:
        if not vertex_session_id or not vertex_session_id.strip():
            return None
        vsid = vertex_session_id.strip()
        with self._lock, self._db:
            self._prune_locked(time.time())
            row = self._db.execute(
                """
                SELECT archive_key, explicit_session_id, vertex_session_id, session_url,
                       session_title, active_model, tools_fp, system_fp,
                       turns_json, metrics_json, updated_at, tenant_id
                FROM session_archive
                WHERE vertex_session_id = ?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (vsid,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def list_recent(self, limit: int = 200) -> List[ArchivedSessionRecord]:
        with self._lock, self._db:
            self._prune_locked(time.time())
            rows = self._db.execute(
                """
                SELECT archive_key, explicit_session_id, vertex_session_id, session_url,
                       session_title, active_model, tools_fp, system_fp,
                       turns_json, metrics_json, updated_at, tenant_id
                FROM session_archive
                ORDER BY updated_at DESC LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        out: List[ArchivedSessionRecord] = []
        for r in rows:
            rec = self._row_to_record(r)
            if rec is not None:
                out.append(rec)
        return out

    def delete(self, archive_key: str) -> None:
        if not archive_key:
            return
        with self._lock, self._db:
            self._db.execute("DELETE FROM session_archive WHERE archive_key = ?", (archive_key,))

    def delete_by_vertex_session_id(self, vertex_session_id: str) -> None:
        if not vertex_session_id:
            return
        with self._lock, self._db:
            self._db.execute("DELETE FROM session_archive WHERE vertex_session_id = ?", (vertex_session_id,))

    def close(self) -> None:
        with self._lock:
            self._db.close()


class ConversationContextBuilder:
    """Converts stateless OpenAI message arrays (including tool loops) into stateful web prompts."""

    def __init__(
        self,
        default_mode: str = "smart",
        auto_reset_new_chat: bool = True,
        codex_home: Optional[Path] = None,
        archive_path: str | Path | None = None,
        archive_ttl_seconds: float = 30 * 86400.0,
    ) -> None:
        self.default_mode = default_mode
        self.auto_reset_new_chat = auto_reset_new_chat
        self._codex_home = codex_home
        self._archive = SessionArchiveStore(archive_path=archive_path, ttl_seconds=archive_ttl_seconds)
        # Keyed by browser client/tab identifier -> list of (role, canonical_text) already in web chat
        self._session_turns: Dict[str, List[Tuple[str, str]]] = {}
        self._session_models: Dict[str, Optional[str]] = {}
        self._session_tools_fp: Dict[str, str] = {}
        self._session_system_fp: Dict[str, str] = {}
        self._session_metrics: Dict[str, Dict[str, Any]] = {}
        self._session_titles: Dict[str, str] = {}
        self._session_urls: Dict[str, str] = {}
        self._session_explicit_ids: Dict[str, str] = {}
        self._session_tenants: Dict[str, str] = {}
        self._session_recovery: Dict[str, SessionRecoveryNotice] = {}
        self._session_force_reset: set[str] = set()
        self._hydration_misses: Dict[str, str] = {}
        self._evicted_slot_vsids: Dict[str, str] = {}
        self._ephemeral_probe_slots: set[str] = set()
        self._session_compactions: Dict[str, Tuple[int, str]] = {}
        self._compaction_seq_counter: int = 0
        self._slot_turn_seq: Dict[str, int] = {}
        self._pending_compactions: Dict[str, PendingCompactionCleanup] = {}

    def close(self) -> None:
        self._archive.close()

    def clear_session(self, session_key: str = "default", *, keep_force_reset: bool = False) -> None:
        self._session_turns.pop(session_key, None)
        self._session_models.pop(session_key, None)
        self._session_tools_fp.pop(session_key, None)
        self._session_system_fp.pop(session_key, None)
        self._session_metrics.pop(session_key, None)
        self._session_titles.pop(session_key, None)
        self._session_urls.pop(session_key, None)
        self._session_explicit_ids.pop(session_key, None)
        self._session_tenants.pop(session_key, None)
        self._session_recovery.pop(session_key, None)
        self._session_compactions.pop(f"slot:{session_key}", None)
        if not keep_force_reset:
            self._session_force_reset.discard(session_key)
        self._hydration_misses.pop(session_key, None)
        self._ephemeral_probe_slots.discard(session_key)
        self._pending_compactions.pop(session_key, None)
        self._slot_turn_seq[session_key] = self._slot_turn_seq.get(session_key, 0) + 1

    def invalidate_session(self, session_key: str) -> None:
        """A rejected/failed turn cannot be reused, even with automatic resets disabled."""
        self.clear_session(session_key)
        self._session_force_reset.add(session_key)

    def record_failed_turn(
        self,
        session_key: str,
        *,
        kind: str = "interrupted",
        reason: str = "",
        submitted: bool = True,
        tab_reset: bool = False,
    ) -> bool:
        """Keep an established web session alive on the first failure; reset only on repeat or blank failure."""
        if tab_reset:
            self.invalidate_session(session_key)
            return False
        tracked = self._session_turns.get(session_key)
        if not submitted:
            return bool(tracked)
        if not tracked or session_key in self._session_recovery:
            self.invalidate_session(session_key)
            return False
        self._session_recovery[session_key] = SessionRecoveryNotice(
            kind=kind or "interrupted",
            reason=(reason or "").strip(),
        )
        return True

    @staticmethod
    def _build_recovery_prompt_block(recovery: SessionRecoveryNotice, has_tools: bool = False) -> str:
        clean_reason = _WHITESPACE_RUN_RE.sub(" ", recovery.reason or "").replace("```", "'''").strip()[:240]
        if recovery.kind == "protocol_error":
            detail = f" Reason: {clean_reason}" if clean_reason else ""
            lines = [
                "[Retry Notice — Previous Reply Had a Format Error; Please Rewrite]",
                f"Your immediately preceding reply in this chat had an invalid format and was REJECTED without executing.{detail}",
                "Ignore your previous rejected reply completely — none of the tool calls or actions in it were run.",
                "Rewrite your response cleanly from scratch for the input below:",
            ]
            if has_tools:
                lines.extend(
                    [
                        '- For client tool calls, use a standalone three-backtick `codex_tool_call` code block on its own line containing valid RFC 8259 JSON `{"name": "...", "arguments": {...}}` (never backslash-escape Markdown characters inside JSON strings), close with three backticks on its own line, and stop immediately without `suggested_questions`.',
                        "- Emit ONE `codex_tool_call` block for edits or shell/script commands (multiple blocks are allowed ONLY for independent pure read-only lookups).",
                        "- Never output internal thinking ('Thinking Process:' / '思考过程：') or host-native tool calls.",
                    ]
                )
            else:
                lines.append("- Follow the requested output format strictly and do not output internal thinking.")
            return "\n".join(lines)
        if recovery.kind == "upstream_task_failure":
            return (
                "[Retry Notice — Previous Cloud Fallback Reply Rejected; Retry In Current Session]\n"
                "Your immediately preceding reply failed with a generic cloud/file fallback error and was REJECTED.\n"
                "Ignore your previous failed reply completely. Do NOT delegate to cloud/host agents or ask the user to upload local workspace files — "
                "use the declared `codex_tool_call` client tools or answer directly from the input below."
            )
        return (
            "[Retry Notice — Ignore Incomplete Previous Attempt]\n"
            "The immediately preceding submission or partial reply in this chat was interrupted, timed out, or cut off before completion and was NOT accepted.\n"
            "Ignore that incomplete previous attempt and any half-finished output above, and respond fresh and completely to the input below."
        )

    def drop_slot(self, session_key: str) -> None:
        self.flush_slot_to_archive(session_key)
        self.clear_session(session_key)
        self._evicted_slot_vsids.pop(session_key, None)

    def migrate_slot_key(self, old_key: str, new_key: str) -> None:
        if old_key == new_key:
            return
        for mapping in (self._session_turns, self._session_models, self._session_tools_fp,
                        self._session_system_fp, self._session_metrics, self._session_titles,
                        self._session_urls, self._session_explicit_ids, self._session_tenants,
                        self._session_recovery,
                        self._hydration_misses, self._evicted_slot_vsids,
                        self._slot_turn_seq, self._pending_compactions):
            if old_key in mapping:
                mapping[new_key] = mapping.pop(old_key)
        if new_key in self._pending_compactions:
            self._pending_compactions[new_key].slot_key = new_key
        if old_key in self._session_force_reset:
            self._session_force_reset.discard(old_key)
            self._session_force_reset.add(new_key)
        if old_key in self._ephemeral_probe_slots:
            self._ephemeral_probe_slots.discard(old_key)
            self._ephemeral_probe_slots.add(new_key)
        old_cmp_key = f"slot:{old_key}"
        if old_cmp_key in self._session_compactions:
            self._session_compactions[f"slot:{new_key}"] = self._session_compactions.pop(old_cmp_key)

    def is_probe_messages(self, messages: Sequence[ChatMessage]) -> bool:
        _, dialogue = self._normalize_messages(messages)
        return (
            len(dialogue) == 1
            and dialogue[0][0] == "user"
            and "这是一次连通性体检" in dialogue[0][1]
        )

    def mark_ephemeral_probe_slot(self, session_key: str) -> None:
        if session_key:
            self._ephemeral_probe_slots.add(session_key)
            self._session_titles[session_key] = "一键模型连通体检"

    def is_ephemeral_probe_slot(self, session_key: str) -> bool:
        return session_key in self._ephemeral_probe_slots

    def is_compaction_messages(self, messages: Sequence[ChatMessage]) -> bool:
        _, dialogue = self._normalize_messages(messages)
        return is_compaction_dialogue(dialogue)

    def is_ephemeral_compaction_slot(self, session_key: str) -> bool:
        return session_key in self._pending_compactions

    def delete_archived_by_vertex_session_id(self, vertex_session_id: str) -> None:
        if vertex_session_id:
            self._archive.delete_by_vertex_session_id(vertex_session_id)

    def register_completed_compaction(
        self,
        session_key: str,
        *,
        session_url: Optional[str] = None,
        origin_session_id: Optional[str] = None,
        summary_text: str = "",
        pre_compact_turn_count: int = 0,
        tenant_id: str = "host",
    ) -> PendingCompactionCleanup:
        self._compaction_seq_counter += 1
        vsid = extract_vertex_session_id(session_url or self._session_urls.get(session_key, ""))
        if vsid:
            self._archive.delete_by_vertex_session_id(vsid)
        eff_tenant = (tenant_id or self._session_tenants.get(session_key) or "host").strip() or "host"
        clean_origin = origin_session_id.strip() if isinstance(origin_session_id, str) and origin_session_id.strip() else None
        if clean_origin and eff_tenant == "host":
            orig_title = lookup_codex_thread_title(clean_origin, codex_home=self._codex_home)
            if orig_title:
                self._session_titles[session_key] = f"自动整理记忆 · {orig_title}"
                if session_key in self._session_metrics:
                    self._session_metrics[session_key]["session_title"] = self._session_titles[session_key]
        rec = PendingCompactionCleanup(
            slot_key=session_key,
            compaction_seq=self._compaction_seq_counter,
            turn_seq=self._slot_turn_seq.get(session_key, 0),
            vertex_session_id=vsid,
            origin_session_id=clean_origin,
            summary_anchor=extract_summary_anchor(summary_text),
            pre_compact_turn_count=max(0, int(pre_compact_turn_count)),
            completed_at=time.time(),
            delivered=False,
            tenant_id=eff_tenant,
        )
        self._pending_compactions[session_key] = rec
        return rec

    def mark_compaction_delivered(
        self,
        session_key: str,
        compaction_seq: int,
        *,
        compaction_token: Optional[str] = None,
    ) -> None:
        rec = self._pending_compactions.get(session_key)
        if rec is None or rec.compaction_seq != compaction_seq:
            return
        rec.delivered = True
        rec.completed_at = time.time()
        if compaction_token and compaction_token.strip():
            rec.compaction_token = compaction_token.strip()

    def find_superseded_compactions(
        self,
        messages: Sequence[ChatMessage],
        *,
        explicit_session_id: Optional[str] = None,
        consumed_tokens: Sequence[str] = (),
        tenant_id: str = "host",
    ) -> List[PendingCompactionCleanup]:
        """Return delivered compaction slots proven obsolete because the resumed session now holds the compacted state."""
        if not self._pending_compactions:
            return []
        _, dialogue = self._normalize_messages(messages)
        if is_compaction_dialogue(dialogue):
            return []
        eff_tenant = (tenant_id or "host").strip() or "host"
        req_sid = explicit_session_id.strip() if isinstance(explicit_session_id, str) and explicit_session_id.strip() else None
        token_set = {t.strip() for t in (consumed_tokens or ()) if isinstance(t, str) and t.strip()}
        norm_blob = ""
        matched: List[PendingCompactionCleanup] = []
        for rec in list(self._pending_compactions.values()):
            if not rec.delivered:
                continue
            if rec.force_superseded:
                matched.append(rec)
                continue
            if (rec.tenant_id or "host") != eff_tenant:
                continue
            if rec.compaction_token and rec.compaction_token in token_set:
                matched.append(rec)
                continue
            if rec.origin_session_id and req_sid and req_sid == rec.origin_session_id:
                matched.append(rec)
                continue
            if rec.summary_anchor:
                if not norm_blob:
                    norm_blob = _WHITESPACE_RUN_RE.sub(
                        " ",
                        "\n".join(content for _, content in dialogue),
                    )
                if rec.summary_anchor in norm_blob:
                    matched.append(rec)
                    continue
        return matched

    def find_out_of_band_superseded_compactions(
        self,
        now_ts: Optional[float] = None,
    ) -> List[PendingCompactionCleanup]:
        """Detect delivered compaction slots whose originating session already advanced or finished locally."""
        if not self._pending_compactions:
            return []
        now_val = time.time() if now_ts is None else float(now_ts)
        matched: List[PendingCompactionCleanup] = []
        for rec in list(self._pending_compactions.values()):
            if not rec.delivered:
                continue
            if rec.force_superseded:
                matched.append(rec)
                continue
            if rec.origin_session_id:
                advanced_elsewhere = any(
                    other_key != rec.slot_key
                    and self._session_explicit_ids.get(other_key) == rec.origin_session_id
                    and self.get_slot_last_updated_at(other_key) > rec.completed_at
                    for other_key in self._session_turns
                )
                if advanced_elsewhere:
                    matched.append(rec)
                    continue
                if (now_val - rec.completed_at) >= 15.0:
                    c_act = lookup_codex_thread_activity(
                        rec.origin_session_id,
                        codex_home=self._codex_home,
                        now_ts=now_val,
                    )
                    if c_act.get("known") and not c_act.get("active"):
                        matched.append(rec)
                        continue
        return matched

    def retire_reused_compaction_slot(self, session_key: str) -> Optional[PendingCompactionCleanup]:
        rec = self._pending_compactions.pop(session_key, None)
        if rec is not None and rec.vertex_session_id:
            self._archive.delete_by_vertex_session_id(rec.vertex_session_id)
        return rec

    def claim_compaction_for_reset(self, session_key: str, compaction_seq: int) -> Optional[PendingCompactionCleanup]:
        rec = self._pending_compactions.get(session_key)
        if rec is None or rec.compaction_seq != compaction_seq:
            return None
        if self._slot_turn_seq.get(session_key, 0) != rec.turn_seq:
            self._pending_compactions.pop(session_key, None)
            return None
        return self._pending_compactions.pop(session_key, None)

    def get_slot_last_updated_at(self, session_key: str) -> float:
        meta = self._session_metrics.get(session_key)
        if isinstance(meta, dict) and meta.get("updated_at"):
            try:
                return float(meta["updated_at"])
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    def get_slot_tenant_id(self, session_key: str) -> str:
        return self._session_tenants.get(session_key) or "host"

    def flush_slot_to_archive(
        self,
        session_key: str,
        *,
        fallback_url: str = "",
        fallback_explicit_session_id: Optional[str] = None,
    ) -> Optional[str]:
        """Persist the active conversation on `session_key` into the L2 archive if it has a Vertex session URL."""
        turns = self._session_turns.get(session_key)
        if not turns:
            return None
        if (
            session_key in self._ephemeral_probe_slots
            or self._session_titles.get(session_key) == "一键模型连通体检"
            or session_key in self._pending_compactions
            or is_completed_compaction_turns(turns)
        ):
            return None
        eff_url = self._session_urls.get(session_key) or fallback_url or ""
        if not extract_vertex_session_id(eff_url):
            return None
        eff_sid = self._session_explicit_ids.get(session_key) or fallback_explicit_session_id
        eff_tenant = self._session_tenants.get(session_key) or "host"
        return self._archive.upsert(
            session_url=eff_url,
            turns=turns,
            explicit_session_id=eff_sid,
            session_title=self._session_titles.get(session_key, ""),
            active_model=self._session_models.get(session_key),
            tools_fp=self._session_tools_fp.get(session_key, ""),
            system_fp=self._session_system_fp.get(session_key, ""),
            metrics=self._session_metrics.get(session_key),
            tenant_id=eff_tenant,
        )

    def evict_slot_to_archive(
        self,
        session_key: str,
        *,
        fallback_url: str = "",
        fallback_explicit_session_id: Optional[str] = None,
    ) -> None:
        """Archive `session_key`'s current conversation and mark the slot as needing a fresh chat."""
        self.flush_slot_to_archive(
            session_key,
            fallback_url=fallback_url,
            fallback_explicit_session_id=fallback_explicit_session_id,
        )
        evicted_vsid = extract_vertex_session_id(
            self._session_urls.get(session_key) or fallback_url or ""
        )
        self.invalidate_session(session_key)
        if evicted_vsid:
            self._evicted_slot_vsids[session_key] = evicted_vsid

    def restore_archived_to_slot(
        self,
        session_key: str,
        record: ArchivedSessionRecord,
        *,
        explicit_session_id: Optional[str] = None,
        session_url: Optional[str] = None,
    ) -> None:
        """Mount a verified historical Gemini session back onto a live worker slot."""
        self.clear_session(session_key)
        self._evicted_slot_vsids.pop(session_key, None)
        self._session_turns[session_key] = list(record.turns)
        self._session_tenants[session_key] = (record.tenant_id or "host").strip() or "host"
        if record.active_model:
            self._session_models[session_key] = record.active_model
        if record.tools_fp:
            self._session_tools_fp[session_key] = record.tools_fp
        self._session_system_fp[session_key] = record.system_fp
        if record.session_title:
            self._session_titles[session_key] = record.session_title
        eff_url = (session_url or record.session_url or "").strip()
        if eff_url:
            self._session_urls[session_key] = eff_url
        eff_sid = explicit_session_id or record.explicit_session_id
        if eff_sid:
            self._session_explicit_ids[session_key] = eff_sid
        restored_meta = dict(record.metrics) if record.metrics else {}
        restored_meta["slot_key"] = session_key
        if record.updated_at:
            restored_meta["updated_at"] = max(int(restored_meta.get("updated_at") or 0), int(record.updated_at))
        inline_cnt, inline_fp = _extract_compaction_summary_info(record.turns)
        restored_cmp = max(int(restored_meta.get("compaction_count") or 0), inline_cnt)
        restored_fp = str(restored_meta.get("compaction_fp") or inline_fp or "")
        restored_meta["compaction_count"] = restored_cmp
        if restored_fp:
            restored_meta["compaction_fp"] = restored_fp
        if restored_cmp > 0:
            cmp_key = eff_sid if eff_sid else f"slot:{session_key}"
            prev_c, prev_f = self._session_compactions.get(cmp_key, (0, ""))
            if restored_cmp >= prev_c:
                self._session_compactions[cmp_key] = (restored_cmp, restored_fp or prev_f)
        self._session_metrics[session_key] = restored_meta

    def reconcile_idle_slot_url(
        self,
        slot_key: str,
        live_tab_url: str,
        fallback_explicit_session_id: Optional[str] = None,
    ) -> bool:
        """Ensure an idle slot's tracked session matches its live browser URL; return True if stale binding evicted."""
        live_vsid = extract_vertex_session_id(live_tab_url)
        if self._session_turns.get(slot_key):
            tracked_url = self._session_urls.get(slot_key, "")
            tracked_vsid = extract_vertex_session_id(tracked_url)
            if not tracked_vsid and live_vsid:
                self._session_urls[slot_key] = live_tab_url.strip()
                self.flush_slot_to_archive(
                    slot_key,
                    fallback_explicit_session_id=fallback_explicit_session_id,
                )
                return False
            if tracked_vsid and live_vsid != tracked_vsid:
                self.evict_slot_to_archive(
                    slot_key,
                    fallback_url=tracked_url,
                    fallback_explicit_session_id=fallback_explicit_session_id,
                )
                return True
            return False

        if fallback_explicit_session_id and live_vsid:
            rec = self._archive.get_by_explicit_session_id(fallback_explicit_session_id)
            if rec is not None and rec.vertex_session_id and rec.vertex_session_id != live_vsid:
                self._evicted_slot_vsids[slot_key] = rec.vertex_session_id
                return True
        return False

    def hydrate_live_slots_from_archive(
        self,
        slot_urls: Dict[str, str],
    ) -> Dict[str, Optional[str]]:
        """Rehydrate untouched live browser tabs (e.g. after server restart) whose `/r/session/<id>` is in L2."""
        hydrated_sids: Dict[str, Optional[str]] = {}
        for slot_key, tab_url in slot_urls.items():
            if (
                slot_key in self._session_turns
                or slot_key in self._session_force_reset
                or slot_key in self._ephemeral_probe_slots
            ):
                continue
            vsid = extract_vertex_session_id(tab_url)
            if not vsid:
                continue
            if (
                self._hydration_misses.get(slot_key) == vsid
                or self._evicted_slot_vsids.get(slot_key) == vsid
            ):
                continue
            rec = self._archive.get_by_vertex_session_id(vsid)
            if rec is None:
                self._hydration_misses[slot_key] = vsid
                continue
            if rec.session_title == "一键模型连通体检":
                self._hydration_misses[slot_key] = vsid
                continue
            if is_completed_compaction_turns(rec.turns):
                self._archive.delete(rec.archive_key)
                self._hydration_misses[slot_key] = vsid
                age_ok = (time.time() - float(rec.updated_at or 0.0)) >= 15.0
                if age_ok and slot_key not in self._pending_compactions:
                    self._compaction_seq_counter += 1
                    self._pending_compactions[slot_key] = PendingCompactionCleanup(
                        slot_key=slot_key,
                        compaction_seq=self._compaction_seq_counter,
                        turn_seq=self._slot_turn_seq.get(slot_key, 0),
                        vertex_session_id=vsid,
                        origin_session_id=rec.explicit_session_id,
                        completed_at=float(rec.updated_at or time.time()),
                        delivered=True,
                        force_superseded=True,
                    )
                continue
            self.restore_archived_to_slot(slot_key, rec, session_url=tab_url)
            hydrated_sids[slot_key] = rec.explicit_session_id
        return hydrated_sids

    def delete_archived_session(self, archive_key: str) -> None:
        self._archive.delete(archive_key)

    def find_archived_session(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str = "gemini-enterprise-web",
        explicit_session_id: Optional[str] = None,
        exclude_vertex_session_ids: Sequence[str] = (),
        tenant_id: str = "host",
    ) -> Optional[ArchivedSessionRecord]:
        """Locate an archived Gemini session in L2 that matches the incoming multi-turn continuation."""
        if model_requests_new_chat(model):
            return None
        user_system_text, dialogue = self._normalize_messages(messages)
        if len(dialogue) <= 1:
            return None
        if dialogue[-1][0] == "user" and dialogue[-1][1].startswith("[NEW_CHAT]"):
            return None

        excluded = {v for v in exclude_vertex_session_ids if v}
        eff_tenant = (tenant_id or "host").strip() or "host"
        req_sid = explicit_session_id.strip() if isinstance(explicit_session_id, str) and explicit_session_id.strip() else None

        def _matches(rec: ArchivedSessionRecord) -> bool:
            if (rec.tenant_id or "host") != eff_tenant:
                return False
            if not rec.vertex_session_id or not rec.session_url or not rec.turns:
                return False
            if rec.vertex_session_id in excluded:
                return False
            if len(rec.turns) >= len(dialogue):
                return False
            sys_ok, _ = self._system_prompts_compatible(
                rec.system_fp, user_system_text, has_tracked_dialogue=len(rec.turns) >= 2
            )
            if not sys_ok:
                return False
            return self._history_matches(rec.turns, dialogue[: len(rec.turns)])

        if req_sid:
            direct = self._archive.get_by_explicit_session_id(req_sid, tenant_id=eff_tenant)
            if direct is not None and _matches(direct):
                return direct

        candidates: List[Tuple[int, float, ArchivedSessionRecord]] = []
        for rec in self._archive.list_recent(limit=200):
            if req_sid and rec.explicit_session_id and rec.explicit_session_id != req_sid:
                continue
            if _matches(rec):
                candidates.append((len(rec.turns), rec.updated_at, rec))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return candidates[0][2]

    def get_slot_context_metrics(self, session_key: str) -> Optional[Dict[str, Any]]:
        meta = self._session_metrics.get(session_key)
        return dict(meta) if isinstance(meta, dict) else None

    def set_session_title(self, session_key: str, title: str) -> None:
        cleaned = (title or "").strip()
        if session_key and cleaned:
            self._session_titles[session_key] = cleaned

    def _record_dialogue_title(self, session_key: str, dialogue: Sequence[Tuple[str, str]]) -> None:
        extracted = extract_dialogue_title(dialogue)
        if extracted:
            self._session_titles[session_key] = extracted

    def is_slot_session_active(self, slot_key: str, slot_dict: Dict[str, Any]) -> bool:
        """Return True if a slot is busy in Bridge or mid-turn in its bound Codex session."""
        slot_tenant = str(slot_dict.get("owner_tenant_id") or self._session_tenants.get(slot_key) or "host")
        if slot_tenant != "host":
            now_ts = time.time()
            return bool(
                slot_dict.get("busy")
                or int(slot_dict.get("waiting_count") or 0) > 0
                or float(slot_dict.get("pending_tool_call_until") or 0.0) > now_ts
            )
        act = resolve_slot_activity(
            slot_dict,
            fallback_session_id=self._session_explicit_ids.get(slot_key),
            codex_home=self._codex_home,
        )
        return bool(act.get("session_active"))

    def get_context_snapshot(self, worker_slots: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
        eff_limit_default = int(CARBON_CONTEXT_WINDOW * CARBON_EFFECTIVE_CONTEXT_PERCENT / 100)
        slots_out: List[Dict[str, Any]] = []
        seen_keys: set[str] = set()
        for ws in worker_slots or []:
            if not isinstance(ws, dict):
                continue
            skey = str(ws.get("slot_key") or "")
            if not skey:
                continue
            seen_keys.add(skey)
            meta = self._session_metrics.get(skey) or {}
            t_cnt = int(meta.get("turn_count") or 0)
            slot_tenant = str(ws.get("owner_tenant_id") or self._session_tenants.get(skey) or meta.get("tenant_id") or "host")
            fallback_sid = self._session_explicit_ids.get(skey)
            ws_for_lookup = ws if ws.get("pinned_session_id") or not fallback_sid else {**ws, "pinned_session_id": fallback_sid}
            act = resolve_slot_activity(
                ws,
                fallback_session_id=fallback_sid,
                codex_home=self._codex_home if slot_tenant == "host" else None,
            )
            if slot_tenant != "host" and act.get("activity_source") == "codex":
                act = {
                    "session_active": False,
                    "activity_phase": "idle",
                    "activity_label": "空闲待命",
                    "activity_source": "slot",
                    "tool_name": "",
                }
            sess_title, title_src = resolve_slot_session_title(
                ws_for_lookup,
                prompt_title=self._session_titles.get(skey, "") or str(meta.get("session_title") or ""),
                turn_count=t_cnt,
                codex_home=self._codex_home if slot_tenant == "host" else None,
            )
            is_probe_slot = skey in self._ephemeral_probe_slots
            is_compaction_slot = skey in self._pending_compactions
            if (
                skey in self._session_force_reset
                and not ws.get("busy")
                and int(ws.get("waiting_count") or 0) == 0
                and t_cnt == 0
                and not ws_for_lookup.get("pinned_session_id")
            ):
                sess_title, title_src = ("", "")
            if is_probe_slot and title_src != "codex":
                sess_title, title_src = ("一键模型连通体检", "task")
            elif is_compaction_slot and title_src != "codex":
                cmp_title = self._session_titles.get(skey, "") or "自动整理对话记忆"
                sess_title, title_src = (cmp_title, "task")
            total_toks = int(meta.get("total_context_tokens") or 0)
            limit_toks = int(meta.get("context_limit_tokens") or CARBON_CONTEXT_WINDOW)
            eff_limit_toks = int(limit_toks * CARBON_EFFECTIVE_CONTEXT_PERCENT / 100)
            compact_toks = int(meta.get("compact_threshold_tokens") or CARBON_AUTO_COMPACT_TOKENS)
            rem_compact = max(0, compact_toks - total_toks)
            rem_context = max(0, limit_toks - total_toks)
            base_updated = int(meta.get("updated_at") or ws.get("last_used_at") or 0)
            if t_cnt > 0 or ws_for_lookup.get("pinned_session_id") or act["session_active"]:
                eff_updated = max(base_updated, int(act.get("codex_updated_at") or 0))
                pinned_sid_str = str(ws_for_lookup.get("pinned_session_id") or "")
                tracked_cmp = int(meta.get("compaction_count") or 0)
                if pinned_sid_str and pinned_sid_str in self._session_compactions:
                    tracked_cmp = max(tracked_cmp, self._session_compactions[pinned_sid_str][0])
                slot_compactions = max(tracked_cmp, int(act.get("compaction_count") or 0))
            else:
                eff_updated = base_updated
                slot_compactions = 0
            slots_out.append({
                "slot_key": skey,
                "tab_id": ws.get("tab_id"),
                "tenant_id": slot_tenant,
                "busy": bool(ws.get("busy")),
                "waiting_count": int(ws.get("waiting_count") or 0),
                "session_active": bool(act["session_active"]),
                "activity_phase": str(act["activity_phase"]),
                "activity_label": str(act["activity_label"]),
                "activity_source": str(act["activity_source"]),
                "tool_name": str(act["tool_name"]),
                "pinned_session_id": ws_for_lookup.get("pinned_session_id"),
                "session_title": sess_title,
                "title_source": title_src,
                "ephemeral_probe": is_probe_slot,
                "ephemeral_compaction": is_compaction_slot,
                "active_model": str(ws.get("active_model") or meta.get("active_model") or ""),
                "turn_count": t_cnt,
                "user_turns": int(meta.get("user_turns") or 0),
                "compaction_count": slot_compactions,
                "total_context_tokens": total_toks,
                "context_limit_tokens": limit_toks,
                "effective_limit_tokens": eff_limit_toks,
                "compact_threshold_tokens": compact_toks,
                "remaining_to_compact_tokens": rem_compact,
                "remaining_context_tokens": rem_context,
                "utilization_pct": round(min(100.0, (total_toks / max(limit_toks, 1)) * 100.0), 2),
                "compact_pct": round(min(100.0, (total_toks / max(compact_toks, 1)) * 100.0), 1),
                "last_delta_tokens": int(meta.get("last_delta_tokens") or 0),
                "last_completion_tokens": int(meta.get("last_completion_tokens") or 0),
                "last_strategy": str(meta.get("last_strategy") or ""),
                "updated_at": eff_updated,
            })
        for skey, meta in self._session_metrics.items():
            if skey in seen_keys:
                continue
            m_copy = dict(meta)
            m_copy.setdefault("session_title", self._session_titles.get(skey, ""))
            m_copy.setdefault("title_source", "prompt" if m_copy.get("session_title") else "")
            m_copy.setdefault("session_active", bool(m_copy.get("busy")))
            m_copy.setdefault("activity_phase", "generating" if m_copy.get("busy") else "idle")
            m_copy.setdefault("activity_label", "工作中 · 模型回复" if m_copy.get("busy") else "空闲待命")
            t_tok = int(m_copy.get("total_context_tokens") or 0)
            l_tok = int(m_copy.get("context_limit_tokens") or CARBON_CONTEXT_WINDOW)
            c_tok = int(m_copy.get("compact_threshold_tokens") or CARBON_AUTO_COMPACT_TOKENS)
            m_copy.setdefault("effective_limit_tokens", int(l_tok * CARBON_EFFECTIVE_CONTEXT_PERCENT / 100))
            m_copy.setdefault("compact_threshold_tokens", c_tok)
            m_copy.setdefault("remaining_to_compact_tokens", max(0, c_tok - t_tok))
            m_copy.setdefault("remaining_context_tokens", max(0, l_tok - t_tok))
            m_copy.setdefault("compaction_count", 0)
            m_copy["compact_pct"] = round(min(100.0, (t_tok / max(c_tok, 1)) * 100.0), 1)
            slots_out.append(m_copy)
        working_count = sum(
            1
            for s in slots_out
            if bool(s.get("busy")) or int(s.get("waiting_count") or 0) > 0 or bool(s.get("session_active"))
        )
        active_slots = [
            s for s in slots_out
            if bool(s.get("busy")) or bool(s.get("session_active")) or (
                not bool(s.get("ephemeral_probe"))
                and not bool(s.get("ephemeral_compaction"))
                and (int(s.get("turn_count") or 0) > 0 or bool(s.get("session_title")))
            )
        ]
        active_slots.sort(
            key=lambda s: (
                1 if (s.get("busy") or s.get("session_active")) else 0,
                int(s.get("updated_at") or 0),
            ),
            reverse=True,
        )
        primary = active_slots[0] if active_slots else ({
            "slot_key": "",
            "tab_id": None,
            "busy": False,
            "session_active": False,
            "activity_phase": "idle",
            "activity_label": "空闲待命",
            "session_title": "",
            "title_source": "",
            "active_model": "",
            "turn_count": 0,
            "user_turns": 0,
            "compaction_count": 0,
            "total_context_tokens": 0,
            "context_limit_tokens": CARBON_CONTEXT_WINDOW,
            "effective_limit_tokens": eff_limit_default,
            "compact_threshold_tokens": CARBON_AUTO_COMPACT_TOKENS,
            "remaining_to_compact_tokens": CARBON_AUTO_COMPACT_TOKENS,
            "remaining_context_tokens": CARBON_CONTEXT_WINDOW,
            "utilization_pct": 0.0,
            "compact_pct": 0.0,
            "last_delta_tokens": 0,
            "last_completion_tokens": 0,
            "last_strategy": "",
            "updated_at": 0,
        })
        return {
            "primary": primary,
            "slots": slots_out,
            "active_sessions": len(active_slots),
            "active_session_count": len(active_slots),
            "working_session_count": working_count,
        }

    def get_session_model(self, session_key: str = "default") -> Optional[str]:
        return self._session_models.get(session_key)

    def has_active_conversation(self, session_key: str) -> bool:
        return bool(self._session_turns.get(session_key))

    def has_active_tool_session(self, session_key: str) -> bool:
        """Return True if `session_key` holds an active multi-turn tool-enabled conversation."""
        return bool(self._session_tools_fp.get(session_key)) and len(
            self._session_turns.get(session_key) or []
        ) >= 2

    @staticmethod
    def _system_prompts_compatible(
        prev_sys_fp: Optional[str],
        cur_sys_text: str,
        has_tracked_dialogue: bool = False,
    ) -> Tuple[bool, str]:
        """Return (compatible, appended_system_delta) tolerating volatile tags or appended skill blocks."""
        if prev_sys_fp is None or prev_sys_fp == cur_sys_text:
            return (True, "")
        c_prev = _canonicalize_for_match("system", prev_sys_fp)
        c_cur = _canonicalize_for_match("system", cur_sys_text)
        if c_prev == c_cur:
            return (True, "")
        if has_tracked_dialogue:
            if cur_sys_text.startswith(prev_sys_fp):
                return (True, cur_sys_text[len(prev_sys_fp) :].strip())
            # If the system prompt shares the primary prefix or only mutated dynamic sections, keep session alive
            common_prefix_len = 0
            for ch1, ch2 in zip(c_prev, c_cur):
                if ch1 != ch2:
                    break
                common_prefix_len += 1
            min_len = min(len(c_prev), len(c_cur))
            if min_len > 0 and common_prefix_len >= int(min_len * 0.65):
                prev_paras = {p.strip() for p in prev_sys_fp.split("\n\n") if p.strip()}
                new_paras = [
                    p.strip()
                    for p in cur_sys_text.split("\n\n")
                    if p.strip() and p.strip() not in prev_paras
                ]
                delta_sys = "\n\n".join(new_paras).strip()
                return (True, f"[Updated System Instructions]\n{delta_sys}" if delta_sys else "")
        return (False, "")

    @staticmethod
    def _normalize_messages(messages: Sequence[ChatMessage]) -> Tuple[str, List[Tuple[str, str]]]:
        system_parts: List[str] = []
        dialogue: List[Tuple[str, str]] = []
        call_id_to_name: Dict[str, str] = {}
        seen_tool_outputs: Dict[str, str] = {}

        for msg in messages:
            role = (msg.role or "user").strip().lower()
            text = msg.text_content().strip()

            if role in {"system", "developer"}:
                if text:
                    system_parts.append(text)
                continue

            if role == "assistant":
                tcs = msg.normalized_tool_calls()
                for tc in tcs:
                    tc_id = str(tc.get("id") or "")
                    fn_name = str((tc.get("function") or {}).get("name") or "")
                    if tc_id and fn_name:
                        call_id_to_name[tc_id] = fn_name
                if tcs:
                    canon_asst = format_assistant_history_entry(text, tcs)
                    dialogue.append(("assistant", canon_asst))
                elif text:
                    dialogue.append(("assistant", text))
                continue

            if role in {"tool", "function"}:
                resolved_name = msg.name or (
                    call_id_to_name.get(msg.tool_call_id) if msg.tool_call_id else None
                )
                if isinstance(msg.content, str):
                    text = deduplicate_tool_text(text, msg.tool_call_id or "", seen_tool_outputs)
                formatted_tool = format_tool_result_entry(
                    text,
                    tool_call_id=msg.tool_call_id,
                    name=resolved_name,
                )
                dialogue.append(("tool", formatted_tool))
                continue

            if text:
                dialogue.append(("user", text))

        return ("\n\n".join(system_parts).strip(), dialogue)

    def find_matching_slots(
        self,
        candidate_slot_keys: Sequence[str],
        messages: Sequence[ChatMessage],
        model: str = "gemini-enterprise-web",
        tenant_id: str = "host",
    ) -> List[str]:
        """Find worker slot keys whose active browser conversation matches `messages`'s prefix."""
        if not candidate_slot_keys or model_requests_new_chat(model):
            return []

        user_system_text, dialogue = self._normalize_messages(messages)
        if len(dialogue) <= 1:
            return []
        if dialogue[-1][0] == "user" and dialogue[-1][1].startswith("[NEW_CHAT]"):
            return []

        eff_tenant = (tenant_id or "host").strip() or "host"
        matched: List[Tuple[int, str]] = []
        for slot_key in candidate_slot_keys:
            slot_tenant = self._session_tenants.get(slot_key) or "host"
            if slot_tenant != eff_tenant:
                continue
            tracked = self._session_turns.get(slot_key)
            if not tracked or len(tracked) >= len(dialogue):
                continue
            prev_sys_fp = self._session_system_fp.get(slot_key)
            sys_ok, _ = self._system_prompts_compatible(
                prev_sys_fp, user_system_text, has_tracked_dialogue=len(tracked) >= 2
            )
            if not sys_ok:
                continue
            if self._history_matches(tracked, dialogue[: len(tracked)]):
                matched.append((len(tracked), slot_key))

        matched.sort(key=lambda item: item[0], reverse=True)
        return [slot_key for _, slot_key in matched]

    def record_completed_turn(
        self,
        session_key: str,
        sent_history: List[Tuple[str, str]],
        assistant_reply: Optional[str] = "",
        assistant_tool_calls: Optional[Sequence[Dict[str, Any]]] = None,
        active_model: Optional[str] = None,
        tools_fingerprint: Optional[str] = None,
        system_fingerprint: Optional[str] = None,
        logical_prompt_tokens: Optional[int] = None,
        web_prompt_tokens: Optional[int] = None,
        last_strategy: Optional[str] = None,
        session_url: Optional[str] = None,
        explicit_session_id: Optional[str] = None,
        is_compaction: bool = False,
        tenant_id: str = "host",
    ) -> None:
        if not is_compaction:
            self._pending_compactions.pop(session_key, None)
        eff_tenant = (tenant_id or self._session_tenants.get(session_key) or "host").strip() or "host"
        self._session_tenants[session_key] = eff_tenant
        updated = list(sent_history)
        if assistant_tool_calls:
            canon = format_assistant_history_entry(assistant_reply or "", assistant_tool_calls)
        else:
            canon = (assistant_reply or "").strip()
        updated.append(("assistant", canon))
        self._session_turns[session_key] = updated
        self._slot_turn_seq[session_key] = self._slot_turn_seq.get(session_key, 0) + 1
        self._session_recovery.pop(session_key, None)
        self._session_force_reset.discard(session_key)
        if active_model is not None:
            self._session_models[session_key] = active_model
        if tools_fingerprint is not None:
            self._session_tools_fp[session_key] = tools_fingerprint
        if system_fingerprint is not None:
            self._session_system_fp[session_key] = system_fingerprint
        if session_url and extract_vertex_session_id(session_url):
            self._session_urls[session_key] = session_url.strip()
        if explicit_session_id and explicit_session_id.strip():
            self._session_explicit_ids[session_key] = explicit_session_id.strip()
        elif last_strategy != "smart_incremental_delta":
            self._session_explicit_ids.pop(session_key, None)
        if (
            len(sent_history) == 1
            and sent_history[0][0] == "user"
            and "这是一次连通性体检" in sent_history[0][1]
        ):
            self._ephemeral_probe_slots.add(session_key)
            self._session_titles[session_key] = "一键模型连通体检"
        else:
            self._ephemeral_probe_slots.discard(session_key)

        comp_tokens = estimate_tokens(canon)
        if logical_prompt_tokens is not None:
            total_ctx = max(0, int(logical_prompt_tokens)) + comp_tokens
        else:
            sys_t = estimate_tokens(self._session_system_fp.get(session_key) or "")
            total_ctx = sys_t + sum(estimate_tokens(t) for _, t in updated)
        eff_model = active_model or self._session_models.get(session_key) or ""
        rem_compact = max(0, CARBON_AUTO_COMPACT_TOKENS - total_ctx)
        rem_context = max(0, CARBON_CONTEXT_WINDOW - total_ctx)
        prev_meta = self._session_metrics.get(session_key) or {}
        eff_sid_clean = self._session_explicit_ids.get(session_key, "")
        cmp_track_key = eff_sid_clean or f"slot:{session_key}"
        prev_cmp_cnt, prev_cmp_fp = self._session_compactions.get(
            cmp_track_key,
            (
                int(prev_meta.get("compaction_count") or 0) if not eff_sid_clean else 0,
                str(prev_meta.get("compaction_fp") or "") if not eff_sid_clean else "",
            ),
        )
        inline_cmp_cnt, cmp_fp = _extract_compaction_summary_info(updated)
        if inline_cmp_cnt > 0:
            if cmp_fp != prev_cmp_fp:
                compaction_count = max(inline_cmp_cnt, prev_cmp_cnt + 1)
            else:
                compaction_count = max(inline_cmp_cnt, prev_cmp_cnt)
            self._session_compactions[cmp_track_key] = (compaction_count, cmp_fp)
        elif last_strategy == "smart_single_turn" and len(updated) <= 2 and not eff_sid_clean:
            compaction_count = 0
            self._session_compactions.pop(cmp_track_key, None)
        else:
            compaction_count = prev_cmp_cnt
        self._session_metrics[session_key] = {
            "slot_key": session_key,
            "tab_id": None,
            "busy": False,
            "tenant_id": eff_tenant,
            "session_title": self._session_titles.get(session_key, ""),
            "active_model": eff_model,
            "turn_count": len(updated),
            "user_turns": sum(1 for r, _ in updated if r == "user"),
            "compaction_count": compaction_count,
            "compaction_fp": cmp_fp or prev_cmp_fp,
            "total_context_tokens": total_ctx,
            "context_limit_tokens": CARBON_CONTEXT_WINDOW,
            "effective_limit_tokens": int(CARBON_CONTEXT_WINDOW * CARBON_EFFECTIVE_CONTEXT_PERCENT / 100),
            "compact_threshold_tokens": CARBON_AUTO_COMPACT_TOKENS,
            "remaining_to_compact_tokens": rem_compact,
            "remaining_context_tokens": rem_context,
            "utilization_pct": round(min(100.0, (total_ctx / max(CARBON_CONTEXT_WINDOW, 1)) * 100.0), 2),
            "compact_pct": round(min(100.0, (total_ctx / max(CARBON_AUTO_COMPACT_TOKENS, 1)) * 100.0), 1),
            "last_delta_tokens": max(0, int(web_prompt_tokens or 0)),
            "last_completion_tokens": comp_tokens,
            "last_strategy": last_strategy or "smart_single_turn",
            "updated_at": int(time.time()),
        }
        if not is_compaction:
            self.flush_slot_to_archive(session_key)

    def build(
        self,
        messages: Sequence[ChatMessage],
        model: str = "gemini-enterprise-web",
        session_key: str = "default",
        mode_override: str | None = None,
        tools: Optional[Sequence[Dict[str, Any]]] = None,
        tool_choice: Optional[Any] = None,
        parallel_tool_calls: Optional[bool] = True,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> PreparedPrompt:
        mode = (mode_override or self.default_mode or "smart").lower()
        had_pending_compaction = session_key in self._pending_compactions
        if had_pending_compaction:
            self.retire_reused_compaction_slot(session_key)
        explicit_new_chat = (
            model_requests_new_chat(model)
            or session_key in self._session_force_reset
            or had_pending_compaction
        )
        target_model = resolve_target_web_model(model)
        pending_recovery = self._session_recovery.get(session_key)

        norm_tools = list(tools or [])
        tools_enabled = is_tool_generation_enabled(norm_tools, tool_choice)
        tools_fp = (
            compute_tools_fingerprint(norm_tools)
            if tools_enabled
            else ""
        )

        user_system_text, dialogue = self._normalize_messages(messages)
        per_msg_atts: List[List[NormalizedAttachment]] = []
        sys_atts: List[NormalizedAttachment] = []
        for msg_item in messages:
            r_low = (msg_item.role or "user").strip().lower()
            extracted_atts = msg_item.extract_attachments()
            if r_low in {"system", "developer"}:
                sys_atts.extend(extracted_atts)
            elif r_low == "assistant":
                if msg_item.normalized_tool_calls() or msg_item.text_content().strip():
                    per_msg_atts.append([])
            elif r_low in {"tool", "function"}:
                per_msg_atts.append(extracted_atts)
            else:
                if msg_item.text_content().strip() or extracted_atts:
                    per_msg_atts.append(extracted_atts)
        all_unique_atts = deduplicate_attachments([*sys_atts, *(a for sub in per_msg_atts for a in sub)])
        effective_system_text = "\n\n".join(
            upgrade_desktop_agent_instructions(msg.text_content().strip())
            for msg in messages
            if (msg.role or "user").strip().lower() in {"system", "developer"} and msg.text_content().strip()
        ).strip()

        tool_sys_text = build_tool_system_instructions(
            norm_tools,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
        )
        if has_coding_tools(norm_tools) and tool_sys_text and CODING_WORKFLOW.strip() not in effective_system_text:
            tool_sys_text += "\n\n" + CODING_WORKFLOW
        fmt_sys_text = build_response_format_instructions(response_format)

        combined_sys_sections = [s for s in (effective_system_text, tool_sys_text, fmt_sys_text) if s]
        combined_system_text = "\n\n".join(combined_sys_sections).strip()

        if not dialogue:
            has_any_att = bool(all_unique_atts)
            fallback_prompt = combined_system_text or ("" if has_any_att else "Hello")
            reset_only_sys = explicit_new_chat or self.auto_reset_new_chat or (pending_recovery is not None)
            if reset_only_sys:
                self.clear_session(session_key, keep_force_reset=True)
                self._session_force_reset.add(session_key)
            return PreparedPrompt(
                prompt=fallback_prompt,
                reset_chat=reset_only_sys,
                strategy_used="system_only",
                normalized_history=[("user", fallback_prompt or "[Attachments]")],
                target_model=target_model,
                tools_fingerprint=tools_fp,
                system_fingerprint=user_system_text,
                attachments=all_unique_atts,
            )

        # Check inline [NEW_CHAT] directive on trailing user turn
        if dialogue[-1][0] == "user" and dialogue[-1][1].startswith("[NEW_CHAT]"):
            explicit_new_chat = True
            stripped_user = dialogue[-1][1][len("[NEW_CHAT]") :].lstrip()
            dialogue[-1] = ("user", stripped_user)

        if explicit_new_chat:
            self.clear_session(session_key, keep_force_reset=True)
            self._session_force_reset.add(session_key)
            pending_recovery = None

        tracked = self._session_turns.get(session_key)
        recovery_notice = self._build_recovery_prompt_block(pending_recovery, has_tools=tools_enabled) if pending_recovery else ""

        has_trailing_tool_results = any(
            r == "tool" for r, _ in self._trailing_non_assistant_slice(dialogue)
        )
        delta_reminder = build_tool_delta_reminder(
            norm_tools,
            tool_choice=tool_choice,
            has_tool_results=has_trailing_tool_results,
        )

        if has_coding_tools(norm_tools):
            read_hint = reading_reminder(reading_diagnostics(messages))
            if read_hint:
                delta_reminder = "\n\n".join(filter(None, [delta_reminder, read_hint]))

        # Track target_model transition on this session_key (UI switches model in-place cleanly)
        if target_model is not None:
            self._session_models[session_key] = target_model

        if mode == "last_message":
            matches_last = bool(
                not explicit_new_chat
                and tracked
                and len(tracked) < len(dialogue)
                and self._history_matches(tracked, dialogue[: len(tracked)])
            )
            tail_prompt = self._format_delta_entries(
                self._trailing_non_assistant_slice(dialogue) or [dialogue[-1]],
                extra_instructions=[fmt_sys_text, delta_reminder] if len(dialogue) > 1 else [],
            )
            reset_last = explicit_new_chat or (self.auto_reset_new_chat and len(dialogue) == 1) or (pending_recovery is not None and not matches_last)
            if reset_last:
                self.clear_session(session_key, keep_force_reset=True)
                self._session_force_reset.add(session_key)
            elif recovery_notice and matches_last:
                tail_prompt = f"{recovery_notice}\n\n{tail_prompt}"
            self._record_dialogue_title(session_key, dialogue)
            prompt = self._with_system_if_first_turn(
                last_text=tail_prompt,
                system_text=combined_system_text,
                is_first_turn=len(dialogue) == 1,
            )
            tail_len = len(self._trailing_non_assistant_slice(dialogue) or [dialogue[-1]])
            tail_atts = deduplicate_attachments([
                *(sys_atts if len(dialogue) == 1 else []),
                *(a for sub in per_msg_atts[-tail_len:] for a in sub),
            ])
            return PreparedPrompt(
                prompt=prompt,
                reset_chat=reset_last,
                strategy_used="last_message",
                normalized_history=dialogue,
                target_model=target_model,
                tools_fingerprint=tools_fp,
                system_fingerprint=user_system_text,
                attachments=tail_atts,
            )

        if mode == "transcript":
            reset_trans = explicit_new_chat or self.auto_reset_new_chat or (pending_recovery is not None)
            if reset_trans:
                self.clear_session(session_key, keep_force_reset=True)
                self._session_force_reset.add(session_key)
            self._record_dialogue_title(session_key, dialogue)
            prompt = self._format_full_transcript(
                system_text=combined_system_text,
                dialogue=dialogue,
                trailing_reminder=delta_reminder,
            )
            return PreparedPrompt(
                prompt=prompt,
                reset_chat=reset_trans,
                strategy_used="transcript",
                normalized_history=dialogue,
                target_model=target_model,
                tools_fingerprint=tools_fp,
                system_fingerprint=user_system_text,
                attachments=all_unique_atts,
            )

        # Smart mode:
        # 1) Single-turn user prompt -> brand-new local conversation!
        #    When `auto_reset_new_chat` is True (default), ALWAYS reset_chat=True (even on the first
        #    request after server startup, in case the browser tab already has an old `/r/session/...` open;
        #    page-driver.js fast-paths in 0ms if the tab is already on a blank new chat).
        if len(dialogue) == 1 and dialogue[0][0] == "user":
            reset_chat = explicit_new_chat or self.auto_reset_new_chat or (pending_recovery is not None)
            if reset_chat:
                self.clear_session(session_key, keep_force_reset=True)
                self._session_force_reset.add(session_key)
                if target_model is not None:
                    self._session_models[session_key] = target_model
            self._record_dialogue_title(session_key, dialogue)
            prompt = self._with_system_if_first_turn(
                last_text=dialogue[0][1],
                system_text=combined_system_text,
                is_first_turn=True,
            )
            if re.fullmatch(r"\[(?:Image|File|Audio|Video) attachment [^\]]+\](?:\s+\[(?:Image|File|Audio|Video) attachment [^\]]+\])*", prompt.strip()):
                prompt = ""
            return PreparedPrompt(
                prompt=prompt,
                reset_chat=reset_chat,
                strategy_used="smart_single_turn",
                normalized_history=dialogue,
                target_model=target_model,
                tools_fingerprint=tools_fp,
                system_fingerprint=user_system_text,
                attachments=all_unique_atts,
            )

        # 2) Multi-turn continuation of the SAME local conversation (including tool_result loops!)
        prev_sys_fp = self._session_system_fp.get(session_key)
        system_matches, sys_delta_block = self._system_prompts_compatible(
            prev_sys_fp, user_system_text, has_tracked_dialogue=bool(tracked and len(tracked) >= 2)
        )
        if (
            not explicit_new_chat
            and tracked
            and system_matches
            and len(tracked) < len(dialogue)
            and self._history_matches(tracked, dialogue[: len(tracked)])
        ):
            delta_entries = dialogue[len(tracked) :]
            prev_fp = self._session_tools_fp.get(session_key, "")
            extra_blocks: List[str] = []
            if sys_delta_block:
                extra_blocks.append(sys_delta_block)
            if tools_fp and tools_fp != prev_fp and tool_sys_text:
                extra_blocks.append(tool_sys_text)
            if fmt_sys_text:
                extra_blocks.append(fmt_sys_text)

            delta_has_tools = any(r == "tool" for r, _ in delta_entries)
            inc_reminder = build_tool_delta_reminder(
                norm_tools,
                tool_choice=tool_choice,
                has_tool_results=delta_has_tools,
            )
            if has_coding_tools(norm_tools):
                read_hint = reading_reminder(reading_diagnostics(messages))
                inc_reminder = "\n\n".join(filter(None, [inc_reminder, read_hint]))
            if inc_reminder:
                extra_blocks.append(inc_reminder)
            self._record_dialogue_title(session_key, dialogue)
            prompt = self._format_delta_entries(delta_entries, extra_instructions=extra_blocks)
            if recovery_notice:
                prompt = f"{recovery_notice}\n\n{prompt}"
            delta_atts = deduplicate_attachments(
                a for sub in per_msg_atts[len(tracked) :] for a in sub
            )
            if delta_atts and re.fullmatch(r"\[(?:Image|File|Audio|Video) attachment [^\]]+\](?:\s+\[(?:Image|File|Audio|Video) attachment [^\]]+\])*", prompt.strip()):
                prompt = ""
            return PreparedPrompt(
                prompt=prompt,
                reset_chat=False,
                strategy_used="smart_incremental_delta",
                normalized_history=dialogue,
                target_model=target_model,
                tools_fingerprint=tools_fp,
                system_fingerprint=user_system_text,
                attachments=delta_atts,
            )

        # 3) Switched to a DIFFERENT multi-turn local conversation not matching current browser tab:
        #    Start a clean web session (`reset_chat=True` when auto_reset_new_chat=True) and pack its history.
        reset_packed = explicit_new_chat or self.auto_reset_new_chat or (pending_recovery is not None)
        if reset_packed:
            self.clear_session(session_key, keep_force_reset=True)
            self._session_force_reset.add(session_key)
            if target_model is not None:
                self._session_models[session_key] = target_model
        self._record_dialogue_title(session_key, dialogue)
        prompt = self._format_full_transcript(
            system_text=combined_system_text,
            dialogue=dialogue,
            trailing_reminder=delta_reminder,
        )
        return PreparedPrompt(
            prompt=prompt,
            reset_chat=reset_packed,
            strategy_used="smart_packed_context",
            normalized_history=dialogue,
            target_model=target_model,
            tools_fingerprint=tools_fp,
            system_fingerprint=user_system_text,
            attachments=all_unique_atts,
        )

    @staticmethod
    def _trailing_non_assistant_slice(dialogue: Sequence[Tuple[str, str]]) -> List[Tuple[str, str]]:
        idx = len(dialogue)
        while idx > 0 and dialogue[idx - 1][0] != "assistant":
            idx -= 1
        return list(dialogue[idx:])

    @staticmethod
    def _format_delta_entries(
        delta_entries: Sequence[Tuple[str, str]],
        extra_instructions: Optional[Sequence[str]] = None,
    ) -> str:
        extras = [e.strip() for e in (extra_instructions or []) if e and e.strip()]
        if len(delta_entries) == 1 and delta_entries[0][0] == "user" and not extras:
            return delta_entries[0][1]

        blocks: List[str] = []
        for role, content in delta_entries:
            content = wire_history_entry(role, content)
            if role == "tool":
                blocks.append(content)
            elif role == "assistant":
                blocks.append(f"Assistant: {content}")
            else:
                blocks.append(content if len(delta_entries) == 1 else f"User: {content}")

        if delta_entries and delta_entries[-1][0] == "assistant":
            blocks.append("Please continue based on the conversation above.")

        blocks.extend(extras)
        return "\n\n".join(b for b in blocks if b)

    @classmethod
    def _history_matches(
        cls,
        tracked: Sequence[Tuple[str, str]],
        incoming_prefix: Sequence[Tuple[str, str]],
    ) -> bool:
        if len(tracked) != len(incoming_prefix):
            return False
        for (r1, t1), (r2, t2) in zip(tracked, incoming_prefix):
            if r1 != r2:
                return False
            s1 = t1.strip()
            s2 = t2.strip()
            if s1 == s2:
                continue
            if _canonicalize_for_match(r1, s1) == _canonicalize_for_match(r2, s2):
                continue
            return False
        return True

    @staticmethod
    def _with_system_if_first_turn(last_text: str, system_text: str, is_first_turn: bool) -> str:
        if system_text and is_first_turn:
            return f"[System Instructions]\n{system_text}\n\n[User Request]\n{last_text}"
        return last_text

    @classmethod
    def _format_full_transcript(
        cls,
        system_text: str,
        dialogue: Sequence[Tuple[str, str]],
        trailing_reminder: str = "",
    ) -> str:
        sections: List[str] = []
        if system_text:
            sections.append(f"[System Instructions]\n{system_text}")

        tail = cls._trailing_non_assistant_slice(dialogue)
        split_idx = len(dialogue) - len(tail) if tail else max(0, len(dialogue) - 1)
        prior = dialogue[:split_idx]
        current_entries = dialogue[split_idx:]

        if prior:
            lines: List[str] = ["[Conversation History]"]
            for role, content in prior:
                content = wire_history_entry(role, content)
                if role == "user":
                    lines.append(f"User: {content}")
                elif role == "assistant":
                    lines.append(f"Assistant: {content}")
                else:
                    lines.append(content)
            sections.append("\n".join(lines))

        current_body = cls._format_delta_entries(
            current_entries,
            extra_instructions=[trailing_reminder] if trailing_reminder else None,
        )
        heading = (
            "[Tool Execution Results]"
            if current_entries and all(r == "tool" for r, _ in current_entries)
            else "[Current User Request]"
        )
        sections.append(f"{heading}\n{current_body}")
        return "\n\n".join(sections)
