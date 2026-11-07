"""clean_model_error_text / model_error_reset_at（2026-11-08 429 显示优化）。

智谱 429 限流的原始报文一句话里信息重复两遍（中文重置时间 + 英文
out-of-its-allowance 括注）且带 APIError 前缀与 "(429: you can retry
your request)" 尾巴——清洗后应只剩一句人话，重置时间可结构化提取。
"""

from ginno_runtime.api.stream import clean_model_error_text, model_error_reset_at

ZHIPU_429 = (
    "APIError: Zhipu GLM: 您已达到每周/每月使用上限, 您的限额将在 "
    "2026-11-09 14:31:21 重置。(out of its allowance: not asked again "
    "before Nov 9 14:31 UTC+8) (429: you can retry your request)"
)


def test_zhipu_429_dedup_and_cleanup():
    out = clean_model_error_text(ZHIPU_429)
    assert out == (
        "Zhipu GLM: 您已达到每周/每月使用上限，您的限额将在 "
        "2026-11-09 14:31:21 重置。"
    )
    # 重置时间保留恰好一份（原文两份：中文主句 + 英文括注）
    assert out.count("2026-11-09") == 1
    for noise in ("APIError", "out of its allowance", "you can retry", "429"):
        assert noise not in out


def test_reset_at_extraction():
    assert model_error_reset_at(ZHIPU_429) == "2026-11-09 14:31:21"
    assert model_error_reset_at(clean_model_error_text(ZHIPU_429)) == "2026-11-09 14:31:21"
    assert model_error_reset_at("Connection reset by peer") is None
    assert model_error_reset_at("") is None


def test_plain_error_passthrough():
    # 无括注/前缀的普通错误：只做逗号归一与限长，不吞内容
    assert clean_model_error_text("Connection reset by peer") == "Connection reset by peer"
    assert "timeout" in clean_model_error_text("ReadTimeout: request timeout (30s)")


def test_empty_falls_back_to_raw():
    # 正则意外吞噬到全空时回退原文，不得向前端发空串
    assert clean_model_error_text("(429: you can retry your request)") == (
        "(429: you can retry your request)"
    )


def test_limit():
    out = clean_model_error_text("x" * 500, limit=120)
    assert len(out) == 120
