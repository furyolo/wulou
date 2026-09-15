# 云端模型协议兼容方案调研

调研日期：2026-09-15。范围是本项目所需的三类直接 HTTP 协议：OpenAI Responses、OpenAI Chat Completions、Anthropic Claude Messages。

## 三种协议的关键差异

| 协议 | 请求入口与鉴权 | 结构化结果 | 本项目的结果读取 |
| --- | --- | --- | --- |
| Responses | `POST /responses`，Bearer 密钥 | `text.format: json_schema` | `output_text`，或 `output` 中的 `output_text` |
| Chat Completions | `POST /chat/completions`，Bearer 密钥 | `response_format: {type: json_object}`；JSON Schema 随提示词传入以兼容不支持 `json_schema` 的网关 | `choices[0].message.content` |
| Claude Messages | `POST /messages`，`x-api-key` 与 `anthropic-version` | `output_config.format: {type: json_schema}`；发送前转换为 Claude 支持的 schema 子集 | `content` 中的 `text` JSON |

Claude 使用 `output_config.format` 而非 OpenAI 的 `response_format`。本项目保留完整业务 schema；对 Claude 发送时移除 `minimum`、`maximum` 等不支持的约束并补入字段说明，响应仍由本地业务校验器严格验证。

## 业界方案

| 项目/方案 | 做法 | 适用性 |
| --- | --- | --- |
| LiteLLM | 以统一调用接口、统一输出和 Provider Adapter 覆盖 OpenAI、Anthropic 等大量供应商；可选 Proxy 提供路由、重试与回退。 | 供应商很多、需要按价格/可用性自动回退时很合适；为当前仅三种协议直接引入完整代理则偏重。 |
| OpenRouter | 将多个模型提供商统一到 OpenAI 兼容入口，路由和供应商适配由网关承担。 | 用户希望把兼容压力交给外部网关时方便；会引入网关依赖、模型可用性与数据路径变化。 |
| Vercel AI SDK 一类 Provider 抽象 | 应用内部保留统一的消息/生成模型，协议差异只放在 provider 边界。 | 架构思想适合本项目；其 JavaScript 运行时本身不适合直接替换当前 Python 服务。 |

## 结论：采用轻量内部适配器

当前最合适的是“统一分类语义 + 三个薄协议适配器”，而不是把任意供应商都伪装成同一种 OpenAI 请求。

1. 上层继续只构造一次分类提示、JSON Schema 和分类校验；三种协议只在发送和解析边界转换。
2. 每个模型方案保存协议、地址、模型、密钥；连接测试使用表单草稿请求模型目录 `/models`，不携带模型名、不发送生成请求、不保存密钥也不写入分类数据。返回的模型目录供两个模型选择框复用；兼容网关未实现模型目录时，仅在其可达时显示“可连接”，详细兼容诊断仅留在本机服务日志。
3. 三种协议都支持离线批处理：Responses 与 Chat Completions 复用 OpenAI Batch 的文件上传与任务接口；Claude Messages 使用原生 Message Batches 的 `requests`、`processing_status` 和 `results_url`。本地台账统一以 JSONL 保存请求与结果，并按 `custom_id` 归并结果。
4. 将来如需要五种以上供应商、跨供应商回退、限流/成本路由或统一审计，再评估在服务端接入 LiteLLM Proxy。届时应用仍可保留本适配层，以避免把业务分类逻辑绑定到网关。

## 连接测试策略

通用客户端不能只用“请求是否能发出”判断模型可用：这不能验证密钥、协议或所选模型。项目采用两级测试：优先 `GET /models/{model}`，以无生成请求验证地址、密钥和模型；若兼容网关未实现 Models API（通常为 404/405/5xx），再发送不带推理参数、最多 1 个输出 token 的 `ping`。认证/权限错误（401/403）不会降级，以免掩盖密钥问题。

## 参考资料

- [OpenAI Responses API](https://platform.openai.com/docs/api-reference/responses)
- [OpenAI Chat Completions API](https://platform.openai.com/docs/api-reference/chat)
- [Anthropic Messages API](https://docs.anthropic.com/en/api/messages)
- [Anthropic Message Batches guide](https://docs.anthropic.com/en/docs/build-with-claude/batch-processing)
- [LiteLLM documentation](https://docs.litellm.ai/docs/)
- [OpenRouter API overview](https://openrouter.ai/docs/api_reference/overview)
- [Vercel AI SDK providers](https://ai-sdk.dev/providers/ai-sdk-providers)
