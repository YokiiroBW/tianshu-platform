import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

/**
 * 合成后台（`tests/backend/*`）需要的两个环境变量：已发布合同目录与带 cryptography 的工具
 * 解释器。两者都是机器级路径（合同在主工作区根，只读输入；证书工具是运行时自带的解释器），
 * 不适合写进仓库，但缺失时 `fixtures.py` 会在导入时就 `KeyError`。
 *
 * 因此这里按顺序解析：已设置的环境变量 → 工作树 `.runtime/workspace-context.json` 记录的
 * 主工作区根 → 都不可用时给出明确错误，而不是猜一个不存在的路径。
 */
const here = dirname(fileURLToPath(import.meta.url));
const context = resolve(here, "../../.runtime/workspace-context.json");
const DEFAULT_TLS_PYTHON =
  "C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe";

function workspaceRoot(): string | null {
  if (!existsSync(context)) return null;
  const recorded = JSON.parse(readFileSync(context, "utf8")).workspace;
  return typeof recorded === "string" ? recorded : null;
}

export function fixtureEnv() {
  const candidates = [process.env.TS012_CONTRACT_DIR];
  const workspace = workspaceRoot();
  if (workspace)
    candidates.push(resolve(workspace, "contracts/text-dialogue/v1"));
  const contract = candidates.find(
    (item): item is string => typeof item === "string" && existsSync(item),
  );
  if (!contract)
    throw new Error(
      `找不到已发布合同目录：请设置 TS012_CONTRACT_DIR，或在 ${context} 里记录主工作区。`,
    );
  return {
    TS012_CONTRACT_DIR: contract,
    TS013_TLS_PYTHON: process.env.TS013_TLS_PYTHON ?? DEFAULT_TLS_PYTHON,
  };
}
