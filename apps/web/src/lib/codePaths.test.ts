import { describe, expect, it } from "vitest";
import { folderToMountFor } from "./codePaths";

describe("folderToMountFor（design §3.2：根外文件的「加为工作区」目标目录）", () => {
  it("提供文件所在目录", () => {
    expect(folderToMountFor("/Volumes/ext2/evidence/run.log")).toBe("/Volumes/ext2/evidence");
    expect(folderToMountFor("/Users/x/proj/a/b.py")).toBe("/Users/x/proj/a");
  });

  it("过宽的目录不给一键挂载", () => {
    for (const file of [
      "/a.log",
      "/Users/x",
      "/tmp/a.log",
      "/private/tmp/a.log",
      "/var/a.log",
      "/private/var/a.log",
      "/Volumes/x",
      "/Users/Shared/x",
    ]) {
      expect(folderToMountFor(file)).toBeNull();
    }
  });

  it("相对路径与目录路径不提供", () => {
    expect(folderToMountFor("relative/a.py")).toBeNull();
    expect(folderToMountFor("/Users/x/proj/")).toBeNull();
  });
});
