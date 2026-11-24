import { describe, expect, it } from "vitest";
import { splitInlineWidgets } from "./inlineWidgets";

const data = { title: "Latest commits", items: [{ label: "Team's repo", value: 'Ready "today"', status: "ok" }] };
const marker = `<ginno-block type="render_widget" data='${JSON.stringify(data)}' summary="One update"></ginno-block>`;

describe("inline widget compatibility", () => {
  it("renders a standalone widget in order with surrounding prose", () => {
    expect(splitInlineWidgets(`Before\n\n${marker}\n\nAfter`, false)).toEqual([
      { text: "Before\n\n" }, { widget: { kind: "stat_list", data, summary: "One update" } }, { text: "\n\nAfter" },
    ]);
  });

  it("preserves fenced examples and invalid or unrelated blocks", () => {
    for (const text of [`\`\`\`html\n${marker}\n\`\`\``, `~~~~\n${marker}\n~~~~`,
      marker.replace("render_widget", "attach_ref"), marker.replace('"items":', '"items"='),
      marker.replace(' summary=', ' onclick=')]) {
      expect(splitInlineWidgets(text, false)).toEqual([{ text }]);
    }
  });

  it("buffers a partial envelope until complete", () => {
    const incomplete = `Before\n${marker.slice(0, -15)}`;
    expect(splitInlineWidgets(incomplete, true)).toEqual([{ text: "Before\n" }]);
    expect(splitInlineWidgets(incomplete, false)).toEqual([{ text: incomplete }]);
    expect(splitInlineWidgets(`Before\n${marker}`, true)).toHaveLength(2);
  });

  it("accepts a declarative chart and rejects non-numeric points", () => {
    const chart = { type: "bar", x: "repo", y: "commits", data: [{ repo: "Web", commits: 4 }] };
    const wrap = (value: unknown) => `<ginno-block type="render_widget" data='${JSON.stringify(value)}'></ginno-block>`;
    expect(splitInlineWidgets(wrap(chart), false)).toMatchObject([{ widget: { kind: "chart", data: chart } }]);
    const invalid = wrap({ ...chart, data: [{ repo: "Web", commits: {} }] });
    expect(splitInlineWidgets(invalid, false)).toEqual([{ text: invalid }]);
  });
});
