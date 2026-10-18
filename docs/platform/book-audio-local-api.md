# 本地听书接口

在“订阅 → 书籍订阅 → 听书设置”选择本地接口，填写服务根地址、模型与音色；API Key 可选。平台不安装本地模型。地址可以是已部署的 NAS/局域网语音服务，改地址会清除原凭据，既有生成任务保持原端点并等待重新配置。

请求为 `POST <根地址>/v1/audio/speech`，JSON：

```json
{"model":"your-model","input":"本段正文。","voice":"your-voice","speed":1.0,"response_format":"mp3"}
```

配置了 Key 时发送 `Authorization: Bearer ...`。响应应为完整的 MP3 字节（建议 `audio/mpeg`），不能返回登录页、JSON 链接或 WAV；请求不跟随重定向。单段最多900个Unicode字符，180秒期限，32MiB音频上限。按自然断句分段，每段独立缓存；本地接口无时间戳，因此按实际分段时长高亮原始文字，不显示伪造逐字时间。

单个章节最终保存为 MP3 和同名 JSON 时间轴，位于书库“类别/作者/书名/audio/音色版本/章节ID”。全局内容缓存位于书库 `.tianshu-audio-cache`，供重复任务、失败恢复及同内容章节复用；不要只删除缓存而保留台账。文件丢失会显示待处理，手动重试只请求缺失片段。

收到请求后断线、超时、5xx、不可解码响应都会按“结果不明”处理，不自动重发。服务明确返回401/402等拒绝则停止；429延后重试。界面可手动重试，提示外部服务可能再次收费。字符上限按UTC天统计提交量，明确拒绝会退还占用，未知结果保留占用。未使用的服务不会成为自动回退。

Edge 使用锁定的 [edge-tts](https://github.com/rany2/edge-tts) 在线客户端，保留原生句子时间戳。Fish 采用[官方带时间戳流接口](https://docs.fish.audio/api-reference/endpoint/openapi-v1/text-to-speech-stream-with-timestamps)，每个 `chunk_seq` 的累计时间轴替换上一快照，音频块保持原顺序。Fish模型必须明确选定，免费档可用性由账号决定；本地HTTP夹具通过不等于真实Fish额度验证。
