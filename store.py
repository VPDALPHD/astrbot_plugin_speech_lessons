"""说话卡存储与复习调度（SM-2）。

设计要点：
- 存的是「说话卡」不是「规则」：场景 / 反例 / 正例 / 一句原则，
  注入时能渲染成"别人这么说 → 我该这么接"的对照，比祈使句更能改语气；
- 复习推进只由「主人明确反馈」驱动，不做"注入即复习成功"那种伪复习；
- 闲置衰减兜底：久没用又没反馈的卡权重慢慢掉，跌破阈值归档；
- v1.0.0 的 lessons 表保留但整表归档，不参与注入（清空重来）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    scene            TEXT    NOT NULL DEFAULT '',
    bad              TEXT    NOT NULL DEFAULT '',
    good             TEXT    NOT NULL DEFAULT '',
    principle        TEXT    NOT NULL DEFAULT '',
    tags             TEXT    NOT NULL DEFAULT '[]',
    confidence       REAL    NOT NULL DEFAULT 0.6,
    source           TEXT    NOT NULL DEFAULT 'text',
    weight           REAL    NOT NULL DEFAULT 1.5,
    status           TEXT    NOT NULL DEFAULT 'active',
    created_at       REAL    NOT NULL,
    updated_at       REAL    NOT NULL,
    last_used_at     REAL    NOT NULL DEFAULT 0,
    decay_settled_at REAL    NOT NULL DEFAULT 0,
    use_count        INTEGER NOT NULL DEFAULT 0,
    review_reps      INTEGER NOT NULL DEFAULT 0,
    review_ef        REAL    NOT NULL DEFAULT 2.5,
    review_interval  REAL    NOT NULL DEFAULT 0,
    next_review_at   REAL    NOT NULL DEFAULT 0,
    feedback_up      INTEGER NOT NULL DEFAULT 0,
    feedback_down    INTEGER NOT NULL DEFAULT 0,
    superseded_by    INTEGER NOT NULL DEFAULT 0,
    card_fp          TEXT    NOT NULL DEFAULT '',
    raw_text         TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_cards_status ON cards(status);
CREATE INDEX IF NOT EXISTS idx_cards_fp ON cards(card_fp);
CREATE TABLE IF NOT EXISTS card_vectors (
    card_id          INTEGER PRIMARY KEY,
    model            TEXT    NOT NULL DEFAULT '',
    dim              INTEGER NOT NULL DEFAULT 0,
    vec              TEXT    NOT NULL DEFAULT '[]',
    updated_at       REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_card_vectors_model ON card_vectors(model);
CREATE TABLE IF NOT EXISTS guide (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    content          TEXT    NOT NULL DEFAULT '',
    updated_at       REAL    NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS journal_pages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    day              TEXT    NOT NULL,
    content          TEXT    NOT NULL DEFAULT '',
    source           TEXT    NOT NULL DEFAULT 'auto',
    use_count        INTEGER NOT NULL DEFAULT 0,
    last_used_at     REAL    NOT NULL DEFAULT 0,
    recall_count     INTEGER NOT NULL DEFAULT 0,
    created_at       REAL    NOT NULL,
    updated_at       REAL    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_journal_day ON journal_pages(day);
CREATE TABLE IF NOT EXISTS journal_interaction_labels (
    text             TEXT PRIMARY KEY,
    is_interaction   INTEGER NOT NULL CHECK (is_interaction IN (0, 1))
);
CREATE TABLE IF NOT EXISTS journal_vectors (
    page_id          INTEGER PRIMARY KEY,
    model            TEXT    NOT NULL DEFAULT '',
    dim              INTEGER NOT NULL DEFAULT 0,
    vec              TEXT    NOT NULL DEFAULT '[]',
    updated_at       REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_journal_vectors_model ON journal_vectors(model);
CREATE TABLE IF NOT EXISTS journal_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id          INTEGER NOT NULL,
    day              TEXT    NOT NULL DEFAULT '',
    kind             TEXT    NOT NULL DEFAULT 'item',
    seq              INTEGER NOT NULL DEFAULT 0,
    text             TEXT    NOT NULL DEFAULT '',
    tag              TEXT    NOT NULL DEFAULT '',
    created_at       REAL    NOT NULL,
    updated_at       REAL    NOT NULL,
    hit_count        INTEGER NOT NULL DEFAULT 0,
    last_hit_at      REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_journal_items_page ON journal_items(page_id, seq);
CREATE INDEX IF NOT EXISTS idx_journal_items_day ON journal_items(day, kind, seq);
"""

# 条目标签：只用于「按记录程度过滤注入」，不影响存储与展示。
#   interaction = 人际互动 / 人际交流（怎么跟人说话、怎么回应、怎么相处）
#   task        = 事务性内容（测试、调试、工具操作、报错、文件、日程…）
#   ''          = 老数据 / 没判断出来的，两种模式下都算可见（宁可多给，不要少给）
ITEM_TAGS = ("", "interaction", "task")

MIN_EF = 1.3
MAX_INTERVAL_DAYS = 120.0
_PUNCT = "，。！？、；：“”‘’（）《》…—,.!?;:\"'()[]{}<>~· "


def norm_key(text: str) -> str:
    """归一化取指纹：去空白标点，用于去重与相似判定。"""
    cleaned = "".join(ch for ch in (text or "") if ch not in _PUNCT).strip()
    return cleaned


def render_items_snapshot(items: Iterable[dict]) -> str:
    """一页的行 → 文本快照（唯一实现）。

    规则：条目顺序编号；批注（note）不编号；划掉的（struck）占号并包 [strike]。
    存储层写快照、插件层渲染注入文本都走这里，避免两份逻辑漂移
    （历史教训：store 里手抄一份，把 "\\n" 写成了字面量，快照塌成一行）。
    """
    out: list[str] = []
    n = 0
    for item in items or []:
        kind = str(item.get("kind") or "item")
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        if kind == "struck":
            n += 1
            out.append(f"{n}. [strike]{text}[/strike]")
        elif kind == "note":
            out.append(text)
        else:
            n += 1
            out.append(f"{n}. {text}")
    return "\n".join(out)


def card_fp(scene: str, bad: str, good: str) -> str:
    """卡片指纹：场景+反例+正例归一化后拼接，同指纹视为同一张卡。"""
    return "|".join(
        [norm_key(scene)[:40], norm_key(bad)[:60], norm_key(good)[:60]]
    )[:200]


@dataclass
class Card:
    id: int
    scene: str
    bad: str
    good: str
    principle: str
    tags: list[str]
    confidence: float
    source: str
    weight: float
    status: str
    created_at: float
    last_used_at: float
    use_count: int
    review_reps: int
    review_ef: float
    review_interval: float
    next_review_at: float
    feedback_up: int
    feedback_down: int
    superseded_by: int
    raw_text: str


class CardStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate_columns_locked()
            self._conn.commit()
            self._archive_legacy()
        self.journal_migrate_from_guide()

    def _migrate_columns_locked(self) -> None:
        """Add missing columns to existing databases."""
        card_cols = {row[1] for row in self._conn.execute("PRAGMA table_info(cards)")}
        if "decay_settled_at" not in card_cols:
            self._conn.execute(
                "ALTER TABLE cards ADD COLUMN decay_settled_at REAL NOT NULL DEFAULT 0"
            )
            self._conn.execute("UPDATE cards SET decay_settled_at=?", (time.time(),))
        try:
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(journal_items)")}
            if cols and "tag" not in cols:
                self._conn.execute(
                    "ALTER TABLE journal_items ADD COLUMN tag TEXT NOT NULL DEFAULT ''"
                )
                logger.info("[speech_lessons] 指南书条目表已补 tag 列")
        except sqlite3.Error as exc:
            logger.warning("[speech_lessons] 补 tag 列失败: %s", exc)

    @staticmethod
    def norm_tag(tag: str) -> str:
        """规范标签：只认 interaction / task，其余一律当「没标」。"""
        body = str(tag or "").strip().lower()
        return body if body in ITEM_TAGS else ""

    def _archive_legacy(self) -> None:
        """v1.0.0 的 lessons 表整表归档：那些正则抽出来的句子不可用。"""
        try:
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(lessons)")}
            if "status" not in cols:
                return
            cur = self._conn.execute(
                "UPDATE lessons SET status='archived', updated_at=? "
                "WHERE status='active'",
                (time.time(),),
            )
            self._conn.commit()
            if cur.rowcount:
                logger.info(
                    "[speech_lessons] 旧版 lessons 归档 %d 条（v2 不再使用）",
                    cur.rowcount,
                )
        except sqlite3.Error:
            return

    # ------------------------------------------------------------------ 写入
    def add(
        self,
        scene: str = "",
        bad: str = "",
        good: str = "",
        principle: str = "",
        tags: Iterable[str] | None = None,
        confidence: float = 0.6,
        source: str = "text",
        weight: float = 1.5,
        raw_text: str = "",
        embedding: list[float] | None = None,
        embedding_model: str = "",
        similar_threshold: float = 0.0,
        max_active: int | None = None,
        correction: tuple[int, str] | None = None,
    ) -> tuple[int, bool]:
        """新增一张卡。返回 (id, 是否新建)。同指纹的旧卡只加强权重。

        语义近似且正反例一致时强化旧卡。明确纠正时校验旧正例后替换。
        容量限制只在真正新建时检查。
        """
        if not good and not bad:
            return 0, False
        now = time.time()
        fp = card_fp(scene, bad, good)
        tag_json = json.dumps(list(tags or []), ensure_ascii=False)
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = None
            if correction is not None:
                target_id, expected_good = correction
                row = self._conn.execute(
                    "SELECT id, weight, principle, scene, bad, good FROM cards "
                    "WHERE id=? AND good=? AND status='active'",
                    (target_id, expected_good),
                ).fetchone()
            correcting = row is not None
            if row is None:
                row = self._conn.execute(
                    "SELECT id, weight, principle, scene, bad, good FROM cards "
                    "WHERE card_fp=? AND status='active' ORDER BY id DESC LIMIT 1",
                    (fp,),
                ).fetchone()
            if row is None and embedding and similar_threshold > 0:
                near = self._find_similar_locked(embedding, embedding_model, similar_threshold)
                if near is not None:
                    candidate = self._conn.execute(
                        "SELECT id, weight, principle, scene, bad, good FROM cards WHERE id=?",
                        (near[0],),
                    ).fetchone()
                    if candidate and (
                        norm_key(candidate["good"]) == norm_key(good)
                        and norm_key(candidate["bad"]) == norm_key(bad)
                    ):
                        row = candidate
            if row:
                card_id = int(row["id"])
                principle_text = (principle or str(row["principle"] or "")).strip()
                if correcting:
                    self._conn.execute(
                        "UPDATE cards SET scene=?, bad=?, good=?, principle=?, tags=?, "
                        "confidence=?, raw_text=?, card_fp=?, updated_at=?, last_used_at=? WHERE id=?",
                        (scene, bad, good, principle_text, tag_json, confidence, raw_text,
                         fp, now, now, card_id),
                    )
                else:
                    self._conn.execute(
                        "UPDATE cards SET weight=?, principle=?, confidence=?, updated_at=?, "
                        "last_used_at=?, feedback_up=feedback_up+1 WHERE id=?",
                        (min(3.0, float(row["weight"]) + 0.4), principle_text,
                         confidence, now, now, card_id),
                    )
                vector_matches = correcting or (row["scene"], row["good"]) == (scene, good)
                if embedding and vector_matches and principle_text == principle:
                    self._save_vector_locked(card_id, embedding, embedding_model, now)
                elif correcting or principle_text != row["principle"]:
                    self._conn.execute("DELETE FROM card_vectors WHERE card_id=?", (card_id,))
                return card_id, False
            if max_active is not None:
                active = self._conn.execute(
                    "SELECT COUNT(*) FROM cards WHERE status='active'"
                ).fetchone()[0]
                if active >= max_active:
                    return 0, False
            cur = self._conn.execute(
                "INSERT INTO cards (scene, bad, good, principle, tags, confidence, source,"
                " weight, status, created_at, updated_at, last_used_at, card_fp, raw_text)"
                " VALUES (?,?,?,?,?,?,?,?,'active',?,?,?,?,?)",
                (
                    scene,
                    bad,
                    good,
                    principle,
                    tag_json,
                    confidence,
                    source,
                    weight,
                    now,
                    now,
                    now,
                    fp,
                    raw_text,
                ),
            )
            new_id = int(cur.lastrowid or 0)
            if new_id and embedding:
                self._save_vector_locked(new_id, embedding, embedding_model, now)
            return new_id, True

    # ------------------------------------------------------------------ 向量
    def _save_vector_locked(
        self, card_id: int, vec: list[float], model: str, now: float
    ) -> None:
        try:
            payload = json.dumps([round(float(x), 6) for x in vec], separators=(",", ":"))
        except (TypeError, ValueError):
            return
        self._conn.execute(
            "INSERT INTO card_vectors (card_id, model, dim, vec, updated_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(card_id) DO UPDATE SET model=excluded.model,"
            " dim=excluded.dim, vec=excluded.vec, updated_at=excluded.updated_at",
            (int(card_id), str(model or ""), len(vec), payload, float(now)),
        )

    def save_vector(self, card_id: int, vec: list[float], model: str = "") -> bool:
        if not card_id or not vec:
            return False
        with self._lock:
            self._save_vector_locked(int(card_id), vec, model, time.time())
            self._conn.commit()
        return True

    def vectors(self, model: str = "") -> dict[int, list[float]]:
        """取某模型下的全部向量（card_id → vec）。换模型后旧向量自动不参与比对。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT card_id, vec FROM card_vectors WHERE model=?", (str(model or ""),)
            ).fetchall()
        out: dict[int, list[float]] = {}
        for row in rows:
            try:
                vec = [float(x) for x in json.loads(row["vec"] or "[]")]
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if vec:
                out[int(row["card_id"])] = vec
        return out

    def _find_similar_locked(
        self, vec: list[float], model: str, threshold: float
    ) -> tuple[int, float] | None:
        from .embedding import cosine

        rows = self._conn.execute(
            "SELECT v.card_id, v.vec FROM card_vectors v"
            " JOIN cards c ON c.id = v.card_id"
            " WHERE v.model=? AND c.status='active'",
            (str(model or ""),),
        ).fetchall()
        best: tuple[int, float] | None = None
        for row in rows:
            try:
                other = [float(x) for x in json.loads(row["vec"] or "[]")]
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            score = cosine(vec, other)
            if score >= threshold and (best is None or score > best[1]):
                best = (int(row["card_id"]), score)
        return best

    def find_similar(
        self, vec: list[float], model: str = "", threshold: float = 0.88
    ) -> tuple[int, float] | None:
        if not vec:
            return None
        with self._lock:
            return self._find_similar_locked(list(vec), model, threshold)

    def vector_count(self, model: str = "") -> int:
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM card_vectors WHERE model=?",
                    (str(model or ""),),
                ).fetchone()
            return int(row["n"]) if row else 0
        except Exception:
            return 0

    # ------------------------------------------------------------------ 查询
    @staticmethod
    def _to_card(row: sqlite3.Row) -> Card:
        try:
            tags = json.loads(row["tags"] or "[]")
        except Exception:
            tags = []
        return Card(
            id=int(row["id"]),
            scene=str(row["scene"] or ""),
            bad=str(row["bad"] or ""),
            good=str(row["good"] or ""),
            principle=str(row["principle"] or ""),
            tags=[str(t) for t in tags if str(t).strip()],
            confidence=float(row["confidence"] or 0.0),
            source=str(row["source"] or ""),
            weight=float(row["weight"] or 0.0),
            status=str(row["status"] or "active"),
            created_at=float(row["created_at"] or 0),
            last_used_at=float(row["last_used_at"] or 0),
            use_count=int(row["use_count"] or 0),
            review_reps=int(row["review_reps"] or 0),
            review_ef=float(row["review_ef"] or 2.5),
            review_interval=float(row["review_interval"] or 0),
            next_review_at=float(row["next_review_at"] or 0),
            feedback_up=int(row["feedback_up"] or 0),
            feedback_down=int(row["feedback_down"] or 0),
            superseded_by=int(row["superseded_by"] or 0),
            raw_text=str(row["raw_text"] or ""),
        )

    def active(self, limit: int = 200) -> list[Card]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM cards WHERE status='active' "
                "ORDER BY weight DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._to_card(r) for r in rows]

    def guide_card_material(self, start: float, end: float) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT scene,principle FROM cards WHERE status='active' AND created_at>=? AND created_at<? "
                "ORDER BY created_at,id",
                (start, end),
            ).fetchall()
        return ["：".join(filter(None, row)) for row in rows if any(row)]

    def by_id(self, card_id: int) -> Card | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM cards WHERE id=?", (card_id,)
            ).fetchone()
            return self._to_card(row) if row else None

    def count_active(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM cards WHERE status='active'"
            ).fetchone()
            return int(row["c"] if row else 0)

    # ------------------------------------------------------- 面板用：查询/改
    _EDITABLE = ("scene", "bad", "good", "principle", "tags", "weight", "status")

    def query(
        self, status: str = "active", keyword: str = "", limit: int = 300, *,
        page: int = 1, sort: str = "", ascending: bool = False,
    ) -> tuple[list[dict], int, int]:
        """Return a sorted page, the matching count, and the clamped page number."""
        where = " FROM cards WHERE 1=1"
        args: list[Any] = []
        if status in ("active", "archived"):
            where += " AND status=?"
            args.append(status)
        if keyword:
            like = f"%{keyword}%"
            where += (
                " AND (scene LIKE ? OR bad LIKE ? OR good LIKE ? OR principle LIKE ?"
                " OR tags LIKE ?)"
            )
            args.extend([like] * 5)
        column = {"time": "created_at", "uses": "use_count"}.get(sort)
        direction = "ASC" if ascending else "DESC"
        if column:
            order = f"{column} {direction}, id {direction}"
        else:
            order = "(status='active') DESC, weight DESC, id DESC"
        with self._lock:
            total = int(self._conn.execute("SELECT COUNT(*)" + where, args).fetchone()[0])
            page = max(1, min(page, (total + limit - 1) // limit))
            rows = self._conn.execute(
                "SELECT *" + where + f" ORDER BY {order} LIMIT ? OFFSET ?",
                [*args, limit, (page - 1) * limit],
            ).fetchall()
            return [self._to_dict(row) for row in rows], total, page

    @staticmethod
    def _to_dict(row: sqlite3.Row) -> dict:
        try:
            tags = json.loads(row["tags"] or "[]")
        except Exception:
            tags = []
        keys = (
            "id", "scene", "bad", "good", "principle", "confidence", "source",
            "weight", "status", "created_at", "updated_at", "last_used_at",
            "use_count", "review_reps", "review_ef", "review_interval",
            "next_review_at", "feedback_up", "feedback_down", "raw_text",
        )
        data = {k: row[k] for k in keys if k in row.keys()}
        data["tags"] = [str(t) for t in tags if str(t).strip()]
        return data

    def update_fields(self, card_id: int, fields: dict) -> bool:
        """面板保存。只接受白名单字段；改了 scene/bad/good 会重算指纹。"""
        sets: list[str] = []
        args: list[Any] = []
        for key in self._EDITABLE:
            if key not in fields:
                continue
            value = fields[key]
            if key == "tags":
                if not isinstance(value, list):
                    value = [t for t in str(value or "").replace("，", ",").split(",") if t.strip()]
                value = json.dumps([str(t).strip() for t in value if str(t).strip()], ensure_ascii=False)
            elif key == "weight":
                try:
                    value = max(0.0, min(5.0, float(value)))
                except (TypeError, ValueError):
                    continue
            elif key == "status":
                if value not in ("active", "archived"):
                    continue
            else:
                value = str(value or "")[:400]
            sets.append(f"{key}=?")
            args.append(value)
        if not sets:
            return False
        now = time.time()
        sets.append("updated_at=?")
        args.append(now)
        with self._lock:
            row = self._conn.execute(
                "SELECT scene, bad, good FROM cards WHERE id=?", (card_id,)
            ).fetchone()
            if not row:
                return False
            scene = fields.get("scene", row["scene"])
            bad = fields.get("bad", row["bad"])
            good = fields.get("good", row["good"])
            sets.append("card_fp=?")
            args.append(card_fp(scene, bad, good))
            args.append(int(card_id))
            cur = self._conn.execute(
                f"UPDATE cards SET {', '.join(sets)} WHERE id=?", args
            )
            self._conn.commit()
            return bool(cur.rowcount)

    def restore(self, card_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cards SET status='active', decay_settled_at=?, updated_at=? WHERE id=?",
                (time.time(), time.time(), card_id),
            )
            self._conn.commit()
            return bool(cur.rowcount)

    def delete(self, card_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM cards WHERE id=?", (card_id,))
            self._conn.commit()
            return bool(cur.rowcount)

    def stats(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active,"
                " SUM(CASE WHEN status='archived' THEN 1 ELSE 0 END) AS archived,"
                " COALESCE(SUM(use_count),0) AS uses,"
                " COALESCE(AVG(CASE WHEN status='active' THEN weight END),0) AS avg_weight,"
                " COALESCE(SUM(feedback_up),0) AS up,"
                " COALESCE(SUM(feedback_down),0) AS down"
                " FROM cards"
            ).fetchone()
        return {
            "total": int(row["total"] or 0),
            "active": int(row["active"] or 0),
            "archived": int(row["archived"] or 0),
            "uses": int(row["uses"] or 0),
            "avg_weight": round(float(row["avg_weight"] or 0), 2),
            "feedback_up": int(row["up"] or 0),
            "feedback_down": int(row["down"] or 0),
        }

    # ------------------------------------------------------------------ 调度
    def touch(self, ids: Iterable[int]) -> None:
        """记一次"用过"。只累计使用次数，不推进复习间隔。"""
        now = time.time()
        ids = [int(i) for i in ids if i]
        if not ids:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE cards SET use_count=use_count+1, last_used_at=? WHERE id=?",
                [(now, i) for i in ids],
            )
            self._conn.commit()

    def feedback(self, ids: Iterable[int], positive: bool) -> int:
        """主人反馈：正向推进 SM-2，负向回退并降权重。"""
        now = time.time()
        changed = 0
        with self._lock:
            for i in [int(x) for x in ids if x]:
                row = self._conn.execute(
                    "SELECT review_reps, review_ef, review_interval, weight FROM cards WHERE id=? AND status='active'",
                    (i,),
                ).fetchone()
                if not row:
                    continue
                reps = int(row["review_reps"] or 0)
                ef = float(row["review_ef"] or 2.5)
                interval = float(row["review_interval"] or 0.0)
                weight = float(row["weight"] or 1.5)
                if positive:
                    reps += 1
                    if reps == 1:
                        interval = 1.0
                    elif reps == 2:
                        interval = 3.0
                    else:
                        interval = round(max(interval, 1.0) * ef, 2)
                    interval = min(interval, MAX_INTERVAL_DAYS)
                    weight = min(3.0, weight + 0.25)
                    self._conn.execute(
                        "UPDATE cards SET review_reps=?, review_ef=?, review_interval=?,"
                        " next_review_at=?, weight=?, feedback_up=feedback_up+1, updated_at=?"
                        " WHERE id=?",
                        (reps, ef, interval, now + interval * 86400.0, weight, now, i),
                    )
                else:
                    reps = 0
                    interval = 0.0
                    ef = max(MIN_EF, ef - 0.2)
                    weight = max(0.2, weight - 0.35)
                    self._conn.execute(
                        "UPDATE cards SET review_reps=0, review_ef=?, review_interval=0,"
                        " next_review_at=?, weight=?, feedback_down=feedback_down+1,"
                        " updated_at=? WHERE id=?",
                        (ef, now, weight, now, i),
                    )
                changed += 1
            self._conn.commit()
        return changed

    def apply_idle_decay(self, per_day: float, archive_weight: float) -> int:
        """Settle only newly elapsed idle time after the first 24 hours."""
        now = time.time()
        archived = 0
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            rows = self._conn.execute(
                "SELECT id, weight, last_used_at, created_at, decay_settled_at FROM cards "
                "WHERE status='active'"
            ).fetchall()
            for row in rows:
                last = float(row["last_used_at"] or row["created_at"])
                start = max(last + 86400.0, float(row["decay_settled_at"]))
                if now <= start:
                    continue
                elapsed_days = (now - start) / 86400.0
                weight = max(0.0, float(row["weight"]) - max(0.0, per_day) * elapsed_days)
                status = "archived" if per_day > 0 and weight <= archive_weight else "active"
                self._conn.execute(
                    "UPDATE cards SET status=?, weight=?, decay_settled_at=?, updated_at=? WHERE id=?",
                    (status, weight, now, now, row["id"]),
                )
                archived += status == "archived"
        return archived

    def archive(self, card_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cards SET status='archived', updated_at=? WHERE id=?",
                (time.time(), card_id),
            )
            self._conn.commit()
            return bool(cur.rowcount)

    def clear(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE cards SET status='archived', updated_at=? WHERE status='active'",
                (time.time(),),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    # ------------------------------------------------------------ 人类伪装指南
    def get_guide(self) -> str:
        """取「人类伪装指南」正文（单行表，没有就返回空串）。"""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT content FROM guide WHERE id=1"
                ).fetchone()
            return str(row["content"] or "") if row else ""
        except Exception:
            return ""

    def save_guide(self, content: str) -> bool:
        text = str(content or "").strip()
        with self._lock:
            self._conn.execute(
                "INSERT INTO guide (id, content, updated_at) VALUES (1, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET content=excluded.content,"
                " updated_at=excluded.updated_at",
                (text, time.time()),
            )
            self._conn.commit()
        return True

    # ------------------------------------------------------------ 人类伪装指南 · 日记
    def journal_migrate_from_guide(self) -> None:
        """旧版「一整篇指南」迁移成第一篇日记（只迁移一次，原表保留不动）。"""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM journal_pages"
                ).fetchone()
                if row and int(row["n"] or 0) > 0:
                    return
                old = self._conn.execute(
                    "SELECT content, updated_at FROM guide WHERE id=1"
                ).fetchone()
            if not old or not str(old["content"] or "").strip():
                return
            ts = float(old["updated_at"] or time.time())
            day = time.strftime("%Y-%m-%d", time.localtime(ts))
            with self._lock:
                self._conn.execute(
                    "INSERT INTO journal_pages (day, content, source, created_at, updated_at)"
                    " VALUES (?,?,?,?,?)",
                    (day, str(old["content"]).strip(), "legacy", ts, ts),
                )
                self._conn.commit()
            logger.info(
                "[speech_lessons] 旧版指南已迁移为日记第一页（%s，%d 字）",
                day,
                len(str(old["content"]).strip()),
            )
        except Exception:  # noqa: BLE001 - 迁移失败不影响启动
            return

    def migrate_entries(self, parse_lines):
        """Import legacy snapshots exactly once, retaining the originals for recovery."""
        with self._lock, self._conn as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS guide_state (key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
            )
            if db.execute("SELECT 1 FROM guide_state WHERE key='revision'").fetchone():
                return
            db.execute(
                """CREATE TABLE guide_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT, pos INTEGER NOT NULL, written INTEGER NOT NULL,
                day TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL, hit_count INTEGER NOT NULL DEFAULT 0,
                last_hit_at REAL NOT NULL DEFAULT 0)"""
            )
            db.execute("CREATE INDEX guide_position ON guide_entries(pos)")
            db.execute("CREATE INDEX guide_date ON guide_entries(day)")
            db.execute("CREATE INDEX guide_recency ON guide_entries(written)")
            db.execute(
                "CREATE TABLE guide_embeddings (model TEXT, text TEXT, vector TEXT NOT NULL, PRIMARY KEY(model,text))"
            )
            db.execute(
                """INSERT INTO guide_entries(id,pos,written,day,kind,text,created_at,updated_at,hit_count,last_hit_at)
                SELECT i.id,i.seq,i.seq,COALESCE(NULLIF(i.day,''),p.day,''),i.kind,i.text,
                    i.created_at,i.updated_at,i.hit_count,i.last_hit_at
                FROM journal_items i LEFT JOIN journal_pages p ON p.id=i.page_id"""
            )
            pages = db.execute(
                """SELECT * FROM journal_pages p WHERE NOT EXISTS
                (SELECT 1 FROM journal_items i WHERE i.page_id=p.id) ORDER BY day,id"""
            ).fetchall()
            for page in pages:
                for seq, (kind, text) in enumerate(parse_lines(page["content"]), 1):
                    db.execute(
                        """INSERT INTO guide_entries(pos,written,day,kind,text,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?)""",
                        (seq, seq, page["day"], kind, text, page["created_at"], page["updated_at"]),
                    )
            ids = db.execute("SELECT id FROM guide_entries ORDER BY day,pos,id").fetchall()
            db.executemany(
                "UPDATE guide_entries SET pos=?,written=? WHERE id=?",
                [(n, n, row[0]) for n, row in enumerate(ids, 1)],
            )
            db.execute("INSERT INTO guide_state VALUES ('revision',1)")

    def guide_snapshot(self, interaction=False):
        """Read a consistent revision and the ordered entries visible in this scope."""
        with self._lock, self._conn as db:
            db.execute("BEGIN")
            rev = db.execute("SELECT value FROM guide_state WHERE key='revision'").fetchone()[0]
            rows = db.execute(
                """SELECT e.id,e.day,e.kind,e.text,e.written,e.updated_at
                FROM guide_entries e LEFT JOIN journal_interaction_labels c ON c.text=e.text
                WHERE (?=0 OR c.is_interaction=1) ORDER BY e.pos,e.id""",
                (int(interaction),),
            ).fetchall()
            return rev, [dict(row) for row in rows]

    def guide_reviewed(self, day):
        with self._lock:
            return bool(
                self._conn.execute(
                    "SELECT 1 FROM guide_state WHERE key=?", ("review:" + day,)
                ).fetchone()
            )

    def change_entries(
        self,
        day,
        rows,
        *,
        original=(),
        revision=None,
        append=False,
        review=False,
        rewrite_day=False,
    ):
        """Apply one ordered edit or append transaction; stale editors cannot overwrite a concurrent change."""
        with self._lock, self._conn as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT value FROM guide_state WHERE key='revision'").fetchone()[0]
            if (
                review
                and db.execute(
                    "SELECT 1 FROM guide_state WHERE key=?", ("review:" + day,)
                ).fetchone()
            ):
                return []
            if revision is not None and current != revision:
                raise ValueError("打开编辑器后指南有更新，请重新打开编辑器后再保存。")
            if append:
                entries = []
                tail = db.execute("SELECT COALESCE(MAX(pos),0) FROM guide_entries").fetchone()[0]
                seen = set(
                    map(
                        tuple,
                        db.execute(
                            "SELECT kind,text FROM guide_entries WHERE day=? AND kind!='struck'",
                            (day,),
                        ),
                    )
                )
            else:
                entries = [
                    dict(r) for r in db.execute("SELECT * FROM guide_entries ORDER BY pos,id")
                ]
                tail = 0
                seen = set()
            by_id = {r["id"]: r for r in entries}
            positions = {r["id"]: i for i, r in enumerate(entries)}
            if rewrite_day:
                original = [r["id"] for r in entries if r["day"] == day]
            original = list(original)
            if any(i not in by_id for i in original) or len(set(original)) != len(original):
                raise ValueError("选中的记录已变化。")
            start = positions[original[0]] if original else len(entries)
            if (
                not rewrite_day
                and [r["id"] for r in entries[start : start + len(original)]] != original
            ):
                raise ValueError("只能编辑连续的书页。")
            if not append and not original and entries and not rewrite_day:
                raise ValueError("缺少编辑范围。")
            selected = set(original)
            kept = set()
            new_ids = []
            written = db.execute("SELECT COALESCE(MAX(written),0) FROM guide_entries").fetchone()[0]
            now = time.time()
            for row in rows:
                kind, body, key = row["kind"], row["text"].strip(), row.get("id")
                if kind not in ("item", "note", "struck") or not body:
                    raise ValueError("条目类型或内容无效。")
                if key is not None:
                    if key not in selected or key in kept:
                        raise ValueError("条目编号超出当前编辑范围。")
                    kept.add(key)
                    if (kind, body) != (by_id[key]["kind"], by_id[key]["text"]):
                        db.execute(
                            "UPDATE guide_entries SET kind=?,text=?,updated_at=? WHERE id=?",
                            (kind, body, now, key),
                        )
                else:
                    if append and (kind, body) in seen:
                        continue
                    seen.add((kind, body))
                    written += 1
                    key = db.execute(
                        """INSERT INTO guide_entries(pos,written,day,kind,text,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?)""",
                        (tail + len(new_ids) + 1, written, day, kind, body, now, now),
                    ).lastrowid
                new_ids.append(key)
            db.executemany("DELETE FROM guide_entries WHERE id=?", [(i,) for i in selected - kept])
            if not append:
                order = [r["id"] for r in entries if r["id"] not in selected]
                order[start:start] = new_ids
                db.executemany(
                    "UPDATE guide_entries SET pos=? WHERE id=?",
                    [
                        (i, key)
                        for i, key in enumerate(order, 1)
                        if key not in by_id or by_id[key]["pos"] != i
                    ],
                )
            if original or new_ids:
                db.execute("UPDATE guide_state SET value=value+1 WHERE key='revision'")
            if review:
                db.execute("INSERT INTO guide_state VALUES (?,1)", ("review:" + day,))
            return new_ids

    def guide_vectors(self, model):
        with self._lock:
            return {
                text: json.loads(vector)
                for text, vector in self._conn.execute(
                    "SELECT text,vector FROM guide_embeddings WHERE model=?", (model,)
                )
            }

    def save_guide_vectors(self, model, pairs):
        with self._lock, self._conn as db:
            db.executemany(
                "INSERT OR REPLACE INTO guide_embeddings VALUES (?,?,?)",
                [(model, text, json.dumps(vec)) for text, vec in pairs if vec],
            )

    def journal_unclassified_texts(self, limit: int = 32) -> list[str]:
        """Return the latest unique texts awaiting independent classification."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT i.text FROM guide_entries i "
                "LEFT JOIN journal_interaction_labels c ON c.text=i.text "
                "WHERE i.kind!='struck' AND c.text IS NULL "
                "GROUP BY i.text ORDER BY MAX(i.id) DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [str(row["text"]) for row in rows]

    def journal_save_interaction_labels(self, labels: list[tuple[str, bool]]) -> None:
        """Persist positive and negative decisions, keyed by the exact text."""
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT OR IGNORE INTO journal_interaction_labels(text, is_interaction) VALUES (?, ?)",
                labels,
            )

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
