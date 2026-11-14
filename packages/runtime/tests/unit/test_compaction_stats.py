"""压缩时长历史 + 进度预估单测（2026-11-14）。"""

from ginno_runtime import compaction_stats


def test_predict_none_when_insufficient_history(isolated_home):
    # 无历史 → None（前端回退旧缓动）
    assert compaction_stats.predict(50) is None
    compaction_stats.record(3.0, 50)
    # 仅 1 条样本仍不足（_MIN_SAMPLES=2）
    assert compaction_stats.predict(50) is None


def test_record_and_predict_median(isolated_home):
    for d, n in ((2.0, 50), (4.0, 50), (6.0, 50), (100.0, 50)):
        compaction_stats.record(d, n)
    # 中位数对离群值（100）稳健：median(2,4,6,100)=5.0；n 相同→不缩放
    p = compaction_stats.predict(50)
    assert p == 5.0, p


def test_predict_scales_with_message_count(isolated_home):
    for _ in range(3):
        compaction_stats.record(4.0, 40)
    # 本次消息数是历史中位的 2 倍 → 缩放 ×2（夹在 0.5~2.0）
    assert compaction_stats.predict(80) == 8.0
    # 远超 2 倍也被夹到 ×2
    assert compaction_stats.predict(400) == 8.0
    # 远小于则夹到 ×0.5
    assert compaction_stats.predict(1) == 2.0


def test_record_ignores_nonpositive(isolated_home):
    compaction_stats.record(0, 50)
    compaction_stats.record(-1, 50)
    assert compaction_stats.stats()["samples"] == 0


def test_history_trimmed_to_keep(isolated_home):
    for i in range(60):
        compaction_stats.record(1.0 + i, 50)
    # 仅保留最近 _KEEP(50) 条
    assert compaction_stats.stats()["samples"] == 50
