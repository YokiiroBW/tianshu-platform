import { comfySemanticLabels, type ComfyCompile } from "./comfyTypes";

export function ComfyCompilePreview({ result }: { result: ComfyCompile }) {
  return (
    <div className="life-record comfy-preview" aria-label="工作流编译结果">
      <header>
        <strong>编译预览已就绪</strong>
        <span className="status-pill">
          {result.dimensions.width} × {result.dimensions.height}
        </span>
      </header>
      <p>工作流：{result.workflow_id}</p>
      <p className="muted">预览只编译本次内容；提交创作后才登记图片任务。</p>
      {Object.entries(result.prompts).map(([semantic, text]) => (
        <p key={semantic}>
          <strong>{comfySemanticLabels[semantic] ?? semantic}：</strong>
          {text || "未填写"}
        </p>
      ))}
      <details>
        <summary>查看节点写入结果 · {result.changes.length} 处</summary>
        <ul className="comfy-change-list">
          {result.changes.map((change, index) => (
            <li key={index}>
              {comfySemanticLabels[change.semantic] ?? change.semantic} → 节点{" "}
              {change.node_id} / {change.input}
              <pre>{JSON.stringify(change.after, null, 2)}</pre>
            </li>
          ))}
        </ul>
        <p>保留节点：{result.preserved_nodes.join("、") || "无额外节点"}</p>
      </details>
      {result.warnings.length > 0 && (
        <ul>
          {result.warnings.map((warning, index) => (
            <li key={index}>{warning}</li>
          ))}
        </ul>
      )}
      {result.model_receipt && <p>模型辅助已完成，结果通过工作流校验。</p>}
      <details>
        <summary>查看完整 API 工作流</summary>
        <pre>{JSON.stringify(result.graph, null, 2)}</pre>
        <p>内容摘要：{result.graph_sha256}</p>
      </details>
    </div>
  );
}
