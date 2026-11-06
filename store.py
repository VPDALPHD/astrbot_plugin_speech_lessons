"""规则存储与复习调度（SM-2）。

设计要点：
- 规则（怎么说）和事实记忆（发生过什么）分开存，互不干扰；
- 复习推进只由「主人明确反馈」驱动，不做"注入即复习成功"那种伪复习；
- 闲置衰减兜底：久没用又没反馈的规则权重慢慢掉，跌破阈值归档。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    rule             TEXT    NOT NULL,
    wrong            TEXT    NOT NULL DEFAULT '',
    scene            TEXT    NOT NULL DEFAULT 'all',
    tags             TEXT    NOT NULL DEFAULT '[]',
    weight           REAL    NOT NULL DEFAULT 1.5,
    source           TEXT    NOT NULL DEFAULT 'chat',
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
    raw_text         TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_lessons_status ON lessons(status);
CREATE INDEX IF NOT EXISTS idx_lessons_scene ON lessons(scene);
"""

MIN_EF = 1.3
MAX_INTERVAL_DAYS = 120.0


@dataclass
class Rule:
    id: int
    rule: str
    wrong: str
    scene: str
    tags: list[str]
    weight: float
    source: str
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


def rule_fp(text: str) -> str:
    """规则指纹：去空白、去标点后取前 80 字，用于去重与取代判定。"""
    cleaned = "".join(
        ch for ch in (text or "") if ch.strip() and ch not in "，。！？、；：“”‘’（）《》…—,.!?;:\"'()[]{}<>"
    )
    return cleaned[:80]


class LessonStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        with self._lock:
            self._conn.executescript(SCHEMA)
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(lessons)")}
            if "rule_fp" not in cols:
                self._conn.execute("ALTER TABLE lessons ADD COLUMN rule_fp TEXT NOT NULL DEFAULT ''")
            self._conn.commit()
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lessons_fp ON lessons(rule_fp)"
            )
            self._conn.commit()

    # ------------------------------------------------------------------ 写入
    def add(
        self,
        rule: str,
        wrong: str = "",
        scene: str = "all",
        tags: Iterable[str] | None = None,
        weight: float = 1.5,
        source: str = "chat",
        raw_text: str = "",
    ) -> tuple[int, bool]:
        """新增一条规则。返回 (id, 是否新建)。同指纹的旧规则会被取代。"""
        now = time.time()
        fp = rule_fp(rule)
        tag_json = json.dumps(list(tags or []), ensure_ascii=False)
        with self._lock:
            row = self._conn.execute(
                "SELECT id, weight FROM lessons WHERE rule_fp=? AND status='active' "
                "AND scene=? ORDER BY id DESC LIMIT 1",
                (fp, scene),
            ).fetchone()
            if row:
                # 同一句话又教了一遍：只加强权重，不重复建条目
                new_weight = min(3.0, float(row["weight"]) + 0.4)
                self._conn.execute(
                    "UPDATE lessons SET weight=?, updated_at=?, last_used_at=?, "
                    "feedback_up=feedback_up+1 WHERE id=?",
                    (new_weight, now, now, row["id"]),
                )
                self._conn.commit()
                return int(row["id"]), False
            cur = self._conn.execute(
                "INSERT INTO lessons (rule, wrong, scene, tags, weight, source, status,"
                " created_at, updated_at, last_used_at, rule_fp, raw_text)"
                " VALUES (?,?,?,?,?,?,'active',?,?,?,?,?)",
                (rule, wrong, scene, tag_json, weight, source, now, now, now, fp, raw_text),
            )
            new_id = int(cur.lastrowid or 0)
            self._conn.commit()
            return new_id, True

    # ------------------------------------------------------------------ 查询
    @staticmethod
    def _to_rule(row: sqlite3.Row) -> Rule:
        try:
            tags = json.loads(row["tags"] or "[]")
        except Exception:
            tags = []
        return Rule(
            id=int(row["id"]),
            rule=str(row["rule"] or ""),
            wrong=str(row["wrong"] or ""),
            scene=str(row["scene"] or "all"),
            tags=[str(t) for t in tags if str(t).strip()],
            weight=float(row["weight"] or 0.0),
            source=str(row["source"] or ""),
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
        )

    def active(self, scene: str | None = None, limit: int = 200) -> list[Rule]:
        with self._lock:
            if scene:
                rows = self._conn.execute(
                    "SELECT * FROM lessons WHERE status='active' AND scene IN ('all', ?) "
                    "ORDER BY weight DESC, id DESC LIMIT ?",
                    (scene, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM lessons WHERE status='active' "
                    "ORDER BY weight DESC, id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [self._to_rule(r) for r in rows]

    def by_id(self, rule_id: int) -> Rule | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM lessons WHERE id=?", (rule_id,)
            ).fetchone()
            return self._to_rule(row) if row else None

    def count_active(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM lessons WHERE status='active'"
            ).fetchone()
            return int(row["c"] if row else 0)

    # ------------------------------------------------------------------ 调度
    def touch(self, ids: Iterable[int]) -> None:
        """记一次"用过"。只累计使用次数，不推进复习间隔。"""
        now = time.time()
        ids = [int(i) for i in ids if i]
        if not ids:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE lessons SET use_count=use_count+1, last_used_at=? WHERE id=?",
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
                    "SELECT review_reps, review_ef, review_interval, weight FROM lessons WHERE id=?",
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
                        "UPDATE lessons SET review_reps=?, review_ef=?, review_interval=?,"
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
                        "UPDATE lessons SET review_reps=0, review_ef=?, review_interval=0,"
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
                "SELECT id, weight, last_used_at, next_review_at, review_reps "
                "FROM lessons WHERE status='active'"
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
                        "UPDATE lessons SET status='archived', weight=?, updated_at=? WHERE id=?",
                        (max(0.0, weight), now, row["id"]),
                    )
                    archived += 1
                else:
                    self._conn.execute(
                        "UPDATE lessons SET weight=?, updated_at=? WHERE id=?",
                        (weight, now, row["id"]),
                    )
            self._conn.commit()
        return archived

    def archive(self, rule_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE lessons SET status='archived', updated_at=? WHERE id=?",
                (time.time(), rule_id),
            )
            self._conn.commit()
            return bool(cur.rowcount)

    def clear(self) -> int:
        with self._lock:
            cur = self._conn.execute("UPDATE lessons SET status='archived', updated_at=?", (time.time(),))
            self._conn.commit()
            return int(cur.rowcount or 0)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
