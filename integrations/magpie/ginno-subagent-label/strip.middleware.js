export function onProvider(body, ctx) {
  if (ctx.agent === "ginno" && body.client_metadata?.ginno_label_test === "1") {
    delete body.client_metadata;
    return body;
  }
}
