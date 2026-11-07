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
from dataclasses import dataclass
from typing import Any, Iterable

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
        """老库补列（幂等）：v2.5.2 起 journal_items 带 tag。"""
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
    ) -> tuple[int, bool]:
        """新增一张卡。返回 (id, 是否新建)。同指纹的旧卡只加强权重。

        ``embedding`` + ``similar_threshold`` 一起给时，先用向量余弦找近似卡：
        相似度 ≥ 阈值就当成同一张（只升权重、补要点），否则才新建。
        """
        if not good and not bad:
            return 0, False
        now = time.time()
        fp = card_fp(scene, bad, good)
        tag_json = json.dumps(list(tags or []), ensure_ascii=False)
        with self._lock:
            # ① 指纹命中（字面完全相同）
            row = self._conn.execute(
                "SELECT id, weight, principle FROM cards WHERE card_fp=? AND status='active' "
                "ORDER BY id DESC LIMIT 1",
                (fp,),
            ).fetchone()
            if row is None and embedding and similar_threshold > 0:
                # ② 语义近似
                near = self._find_similar_locked(
                    embedding, embedding_model, similar_threshold
                )
                if near is not None:
                    row = self._conn.execute(
                        "SELECT id, weight, principle FROM cards WHERE id=?",
                        (near[0],),
                    ).fetchone()
            if row:
                new_weight = min(3.0, float(row["weight"]) + 0.4)
                # 同一张卡又教一遍：原则可以更新成更准的说法
                principle_text = (principle or str(row["principle"] or "")).strip()
                self._conn.execute(
                    "UPDATE cards SET weight=?, principle=?, confidence=?, updated_at=?,"
                    " last_used_at=?, feedback_up=feedback_up+1 WHERE id=?",
                    (new_weight, principle_text, confidence, now, now, row["id"]),
                )
                self._conn.commit()
                return int(row["id"]), False
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
            self._conn.commit()
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

    def query(self, status: str = "active", keyword: str = "", limit: int = 300) -> list[dict]:
        """面板列卡。status=active/archived/all，keyword 模糊搜所有文本字段。"""
        sql = "SELECT * FROM cards WHERE 1=1"
        args: list[Any] = []
        if status in ("active", "archived"):
            sql += " AND status=?"
            args.append(status)
        if keyword:
            like = f"%{keyword}%"
            sql += (
                " AND (scene LIKE ? OR bad LIKE ? OR good LIKE ? OR principle LIKE ?"
                " OR tags LIKE ?)"
            )
            args.extend([like] * 5)
        sql += " ORDER BY (status='active') DESC, weight DESC, id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
            return [self._to_dict(r) for r in rows]

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
                "UPDATE cards SET status='active', updated_at=? WHERE id=?",
                (time.time(), card_id),
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
                    "SELECT review_reps, review_ef, review_interval, weight FROM cards WHERE id=?",
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
        """闲置衰减兜底：按天折权重，跌破阈值归档。返回归档条数。"""
        now = time.time()
        archived = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, weight, last_used_at, next_review_at FROM cards "
                "WHERE status='active'"
            ).fetchall()
            for row in rows:
                last = float(row["last_used_at"] or row["next_review_at"] or 0)
                if last <= 0:
                    continue
                idle_days = max(0.0, (now - last) / 86400.0)
                if idle_days < 1.0:
                    continue
                weight = float(row["weight"] or 0.0) - per_day * idle_days
                if weight <= archive_weight:
                    self._conn.execute(
                        "UPDATE cards SET status='archived', weight=?, updated_at=? WHERE id=?",
                        (max(0.0, weight), now, row["id"]),
                    )
                    archived += 1
                else:
                    self._conn.execute(
                        "UPDATE cards SET weight=?, updated_at=? WHERE id=?",
                        (weight, now, row["id"]),
                    )
            self._conn.commit()
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

    @staticmethod
    def _to_page(row: sqlite3.Row) -> dict:
        content = str(row["content"] or "")
        return {
            "id": int(row["id"]),
            "day": str(row["day"] or ""),
            "content": content,
            "chars": len(content.strip()),
            "source": str(row["source"] or ""),
            "use_count": int(row["use_count"] or 0),
            "recall_count": int(row["recall_count"] or 0),
            "created_at": float(row["created_at"] or 0),
            "updated_at": float(row["updated_at"] or 0),
        }

    def journal_pages(self, limit: int = 200) -> list[dict]:
        """按日期倒序取日记页（新的在前）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM journal_pages ORDER BY day DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [self._to_page(r) for r in rows]

    def journal_recent(self, limit: int = 2) -> list[dict]:
        """最近 N 页，返回时按时间正序（方便按先后读）。"""
        pages = self.journal_pages(limit=max(1, int(limit)))
        return list(reversed(pages))

    def journal_page(self, page_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM journal_pages WHERE id=?", (int(page_id),)
            ).fetchone()
        return self._to_page(row) if row else None

    def journal_search(self, keyword: str, limit: int = 5) -> list[dict]:
        """按关键词翻页：正文模糊匹配（关键词查询用）。"""
        word = str(keyword or "").strip()
        if not word:
            return []
        like = f"%{word}%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM journal_pages WHERE content LIKE ?"
                " ORDER BY day DESC, id DESC LIMIT ?",
                (like, max(1, int(limit))),
            ).fetchall()
        return [self._to_page(r) for r in rows]

    def journal_page_by_day(self, day: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM journal_pages WHERE day=?", (str(day or ""),)
            ).fetchone()
        return self._to_page(row) if row else None

    def journal_upsert(self, day: str, content: str, source: str = "panel") -> int:
        """按天写一页：已有就整页覆盖，没有就新建。"""
        text = str(content or "").strip()
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT id, created_at FROM journal_pages WHERE day=?", (str(day),)
            ).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE journal_pages SET content=?, source=?, updated_at=? WHERE id=?",
                    (text, str(source or "panel"), now, int(row["id"])),
                )
                page_id = int(row["id"])
            else:
                cur = self._conn.execute(
                    "INSERT INTO journal_pages (day, content, source, created_at, updated_at)"
                    " VALUES (?,?,?,?,?)",
                    (str(day), text, str(source or "panel"), now, now),
                )
                page_id = int(cur.lastrowid or 0)
            self._conn.commit()
        return page_id

    def journal_append(self, day: str, text: str, source: str = "auto") -> tuple[int, bool]:
        """往某天的日记后面续一段（同页重复内容会跳过）。返回 (页 id, 是否写入)。"""
        chunk = str(text or "").strip()
        if not chunk:
            page = self.journal_page_by_day(day)
            return (int(page["id"]) if page else 0), False
        page = self.journal_page_by_day(day)
        if page and chunk in str(page["content"] or ""):
            return int(page["id"]), False
        if not page:
            return self.journal_upsert(day, chunk, source), True
        merged = (str(page["content"] or "").rstrip() + "\n\n" + chunk).strip()
        return self.journal_upsert(day, merged, source), True

    def journal_delete(self, page_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM journal_pages WHERE id=?", (int(page_id),)
            )
            self._conn.execute(
                "DELETE FROM journal_vectors WHERE page_id=?", (int(page_id),)
            )
            self._conn.execute(
                "DELETE FROM journal_items WHERE page_id=?", (int(page_id),)
            )
            self._conn.commit()
            return bool(cur.rowcount)

    def journal_replace_all(self, pages: list[dict], source: str = "review") -> int:
        """整本日记重写：清空后按页写入（精炼用）。"""
        now = time.time()
        rows = []
        for page in pages:
            day = str(page.get("day") or "").strip()
            content = str(page.get("content") or "").strip()
            if not day or not content:
                continue
            rows.append((day, content, str(source or "review"), now, now))
        with self._lock:
            self._conn.execute("DELETE FROM journal_pages")
            self._conn.execute("DELETE FROM journal_vectors")
            for row in rows:
                self._conn.execute(
                    "INSERT INTO journal_pages (day, content, source, created_at, updated_at)"
                    " VALUES (?,?,?,?,?)",
                    row,
                )
            self._conn.commit()
        return len(rows)

    def journal_stats(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS pages, COALESCE(SUM(LENGTH(content)),0) AS chars,"
                " MAX(day) AS latest_day, MAX(updated_at) AS latest_at"
                " FROM journal_pages"
            ).fetchone()
        return {
            "pages": int(row["pages"] or 0) if row else 0,
            "chars": int(row["chars"] or 0) if row else 0,
            "latest_day": str(row["latest_day"] or "") if row else "",
            "latest_at": float(row["latest_at"] or 0) if row else 0.0,
        }

    def journal_touch(self, ids: Iterable[int]) -> None:
        now = time.time()
        ids = [int(i) for i in ids if i]
        if not ids:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE journal_pages SET use_count=use_count+1, last_used_at=? WHERE id=?",
                [(now, i) for i in ids],
            )
            self._conn.commit()

    def journal_mark_recall(self, ids: Iterable[int]) -> None:
        """被「想起」的旧页记一次召回，用于以后判断哪些页还活着。"""
        ids = [int(i) for i in ids if i]
        if not ids:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE journal_pages SET recall_count=recall_count+1 WHERE id=?",
                [(i,) for i in ids],
            )
            self._conn.commit()

    def journal_save_vector(self, page_id: int, vec: list[float], model: str = "") -> bool:
        if not page_id or not vec:
            return False
        try:
            payload = json.dumps([round(float(x), 6) for x in vec], separators=(",", ":"))
        except (TypeError, ValueError):
            return False
        with self._lock:
            self._conn.execute(
                "INSERT INTO journal_vectors (page_id, model, dim, vec, updated_at)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(page_id) DO UPDATE SET model=excluded.model,"
                " dim=excluded.dim, vec=excluded.vec, updated_at=excluded.updated_at",
                (int(page_id), str(model or ""), len(vec), payload, time.time()),
            )
            self._conn.commit()
        return True

    def journal_vectors(self, model: str = "") -> dict[int, list[float]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT page_id, vec FROM journal_vectors WHERE model=?",
                (str(model or ""),),
            ).fetchall()
        out: dict[int, list[float]] = {}
        for row in rows:
            try:
                vec = [float(x) for x in json.loads(row["vec"] or "[]")]
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if vec:
                out[int(row["page_id"])] = vec
        return out

    def journal_vector_meta(self, model: str = "") -> dict[int, float]:
        """每页向量最后一次算出来的时间（用来判断向量是否比页面还旧）。"""
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT page_id, updated_at FROM journal_vectors WHERE model=?",
                    (str(model or ""),),
                ).fetchall()
        except sqlite3.Error:
            return {}
        return {int(r["page_id"]): float(r["updated_at"] or 0) for r in rows}

    def journal_vector_count(self, model: str = "") -> int:
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM journal_vectors WHERE model=?",
                    (str(model or ""),),
                ).fetchone()
            return int(row["n"]) if row else 0
        except Exception:
            return 0

    # ------------------------------------------------------------ 结构化条目
    def journal_items(self, page_id: int) -> list[dict]:
        """取某一页的行（按 seq 升序）：kind=item/note/struck。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM journal_items WHERE page_id=? ORDER BY seq ASC, id ASC",
                (int(page_id),),
            ).fetchall()
        return [
            {
                "id": int(r["id"]),
                "page_id": int(r["page_id"]),
                "day": str(r["day"] or ""),
                "kind": str(r["kind"] or "item"),
                "seq": int(r["seq"] or 0),
                "text": str(r["text"] or ""),
                "tag": self.norm_tag(r["tag"]),
                "hit_count": int(r["hit_count"] or 0),
            }
            for r in rows
        ]

    def journal_items_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM journal_items").fetchone()
        return int(row["n"]) if row else 0

    def journal_replace_items(
        self, page_id: int, day: str, items: list[dict], snapshot: str
    ) -> None:
        """整页替换：清掉该页旧行、按顺序写入新行，并更新 content 快照。"""
        now = time.time()
        with self._lock:
            self._conn.execute(
                "DELETE FROM journal_items WHERE page_id=?", (int(page_id),)
            )
            for index, item in enumerate(items, 1):
                kind = str(item.get("kind") or "item")
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                self._conn.execute(
                    "INSERT INTO journal_items"
                    " (page_id, day, kind, seq, text, tag, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        int(page_id),
                        str(day or ""),
                        kind,
                        index,
                        text,
                        self.norm_tag(item.get("tag")),
                        now,
                        now,
                    ),
                )
            self._conn.execute(
                "UPDATE journal_pages SET content=?, updated_at=? WHERE id=?",
                (str(snapshot or ""), now, int(page_id)),
            )
            # 内容变了，旧向量就是过期的：删掉，召回时会按新内容懒重算
            self._conn.execute(
                "DELETE FROM journal_vectors WHERE page_id=?", (int(page_id),)
            )
            self._conn.commit()

    def journal_item(self, item_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM journal_items WHERE id=?", (int(item_id),)
            ).fetchone()
        if not row:
            return None
        return {
            "id": int(row["id"]),
            "page_id": int(row["page_id"]),
            "day": str(row["day"] or ""),
            "kind": str(row["kind"] or "item"),
            "seq": int(row["seq"] or 0),
            "text": str(row["text"] or ""),
            "tag": self.norm_tag(row["tag"]),
        }

    def journal_update_item(
        self,
        item_id: int,
        *,
        text: str | None = None,
        kind: str | None = None,
        tag: str | None = None,
    ) -> bool:
        sets: list[str] = []
        params: list[Any] = []
        if text is not None:
            sets.append("text=?")
            params.append(str(text).strip())
        if kind is not None:
            sets.append("kind=?")
            params.append(str(kind))
        if tag is not None:
            sets.append("tag=?")
            params.append(self.norm_tag(tag))
        if not sets:
            return False
        sets.append("updated_at=?")
        params.append(time.time())
        params.append(int(item_id))
        with self._lock:
            cur = self._conn.execute(
                "UPDATE journal_items SET " + ", ".join(sets) + " WHERE id=?",
                tuple(params),
            )
            self._conn.commit()
            return bool(cur.rowcount)

    def journal_delete_item(self, item_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM journal_items WHERE id=?", (int(item_id),)
            )
            self._conn.commit()
            return bool(cur.rowcount)

    def journal_insert_item(
        self,
        page_id: int,
        day: str,
        after_seq: int,
        kind: str,
        text: str,
        tag: str = "",
    ) -> int:
        """在某行之后插入一行（after_seq=0 表示插到最前面）。"""
        now = time.time()
        body = str(text or "").strip()
        if not body:
            return 0
        with self._lock:
            self._conn.execute(
                "UPDATE journal_items SET seq=seq+1 WHERE page_id=? AND seq>?",
                (int(page_id), int(after_seq)),
            )
            cur = self._conn.execute(
                "INSERT INTO journal_items"
                " (page_id, day, kind, seq, text, tag, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    int(page_id),
                    str(day or ""),
                    str(kind or "item"),
                    int(after_seq) + 1,
                    body,
                    self.norm_tag(tag),
                    now,
                    now,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def journal_untagged_items(self, limit: int = 5000) -> list[dict]:
        """还没打标签的条目（升级后一次性补标用）。"""
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT id, text, kind FROM journal_items"
                    " WHERE tag='' LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
        except sqlite3.Error:
            return []
        return [
            {"id": int(r["id"]), "text": str(r["text"] or ""), "kind": str(r["kind"] or "item")}
            for r in rows
        ]

    def journal_tag_stats(self) -> dict:
        """条目按标签计数：面板顶部用。"""
        out = {"interaction": 0, "task": 0, "untagged": 0}
        try:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT tag, COUNT(*) AS n FROM journal_items GROUP BY tag"
                ).fetchall()
        except sqlite3.Error:
            return out
        for row in rows:
            tag = self.norm_tag(row["tag"])
            key = tag or "untagged"
            out[key] = out.get(key, 0) + int(row["n"] or 0)
        return out

    def journal_touch_items(self, ids: Iterable[int]) -> None:
        ids = [int(i) for i in ids if i]
        if not ids:
            return
        now = time.time()
        with self._lock:
            self._conn.executemany(
                "UPDATE journal_items SET hit_count=hit_count+1, last_hit_at=? WHERE id=?",
                [(now, i) for i in ids],
            )
            self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
