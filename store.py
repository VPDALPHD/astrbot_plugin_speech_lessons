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
"""

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
            self._conn.commit()
            self._archive_legacy()

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
    ) -> tuple[int, bool]:
        """新增一张卡。返回 (id, 是否新建)。同指纹的旧卡只加强权重。"""
        if not good and not bad:
            return 0, False
        now = time.time()
        fp = card_fp(scene, bad, good)
        tag_json = json.dumps(list(tags or []), ensure_ascii=False)
        with self._lock:
            row = self._conn.execute(
                "SELECT id, weight, principle FROM cards WHERE card_fp=? AND status='active' "
                "ORDER BY id DESC LIMIT 1",
                (fp,),
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
            self._conn.commit()
            return new_id, True

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

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
