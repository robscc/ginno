/** Composer → runtime 的单条 WS 文本帧上限（字节）。
 *
 * 与后端 `ws/loop.py` 的 MAX_INBOUND_TEXT_BYTES 同值：uvicorn 对 >16MB 的
 * 帧直接 1009 断连（侦察 2026-11-22 实测），后端先以 1MB 拒超大文本并回
 * error 事件（不断连）；前端同口径提前拦截，给「文本过长」可见反馈。
 * 两处改动必须同步本常量与后端定义。
 */
export const MAX_SEND_TEXT_BYTES = 1_048_576;

/** 文本编码后的 UTF-8 字节数（与 WS 帧的实际计费口径一致）。 */
export function textByteLength(text: string): number {
  return new TextEncoder().encode(text).length;
}
