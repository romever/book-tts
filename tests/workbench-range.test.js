const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function loadWorkbench() {
  const appPath = path.join(__dirname, "..", "static", "app.js");
  const source = fs.readFileSync(appPath, "utf8");
  const instrumented = source.replace(
    /\}\)\(\);\s*$/,
    "globalThis.__workbenchTest = { workbench, stagePayload, stageState };\n})();",
  );
  const context = {
    console,
    document: { body: { dataset: { page: "test", bookId: "book-1" } } },
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    window: { clearTimeout: () => {}, setTimeout: () => 0 },
  };
  vm.runInNewContext(instrumented, context, { filename: appPath });
  return context.__workbenchTest;
}

test("按章节锁定后始终提交章节范围，不能提交残留的分段切片", () => {
  const { workbench, stagePayload } = loadWorkbench();
  workbench.mode = "chapters";
  workbench.chapterStart = 1;
  workbench.chapterEnd = 1;
  workbench.rangeConfirmed = true;
  workbench.lockedRange = { start_segment: 13, end_segment: 18 };

  assert.deepEqual(
    JSON.parse(JSON.stringify(stagePayload("synthesize"))),
    {
      selection_mode: "chapters",
      chapter_start: 1,
      chapter_end: 1,
      start_stage: "synthesize",
      end_stage: "synthesize",
      output_mode: "none",
    },
  );
});

test("第一章只有 12/22 段语音时不能进入导出步骤", () => {
  const { workbench, stageState } = loadWorkbench();
  workbench.mode = "chapters";
  workbench.chapterStart = 1;
  workbench.chapterEnd = 1;
  workbench.rangeConfirmed = true;
  workbench.lockedRange = { start_segment: 1, end_segment: 22 };
  workbench.stagePreviews = {
    precheck: { precheck_count: 4, precheck_completed_count: 4 },
    synthesize: { audio_total_count: 22, audio_completed_count: 12 },
  };
  const data = {
    executions: [
      {
        resolved_start_sequence: 0,
        resolved_end_sequence: 21,
        start_stage: "synthesize",
        end_stage: "synthesize",
        current_stage: "synthesize",
        status: "completed",
      },
    ],
    active_execution: null,
  };

  assert.deepEqual(
    JSON.parse(JSON.stringify(stageState(data))),
    { current: "synthesize", index: 3, review: false },
  );
});
