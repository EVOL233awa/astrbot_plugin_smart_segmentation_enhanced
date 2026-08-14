# AstrBot 智能分段插件 (个人维护版)

在主 LLM 回复生成后先调用一个分段模型，把完整回复拆成更像真人聊天的多条消息；发送前只保留首段，首段发送完成后再按延迟异步补发剩余段。

> ⚠️ **声明与背景（个人维护版）**
> 
> 本项目为原插件 [exynos967/astrbot_plugin_smart_segmentation](https://github.com/exynos967/astrbot_plugin_smart_segmentation) 的个人维护分支。
> 
> **深度优化适配本地轻量模型（如 Qwen3.5-4B-MTP Q4KXL）**：
> 在本地运行 Qwen3.5-4B-MTP Q4KXL 这类小参数模型时，通常会遇到指令遵循较弱（输出严格 JSON 格式时易崩溃）、容易复读 Few-Shot 示例、以及推理 Token 消耗敏感等痛点。为此，本项目对交互协议进行了深度重构（将高消耗的 JSON 格式优化为轻量级的管道符 `|` 分割形式），升级了提示词防线，并增加了一系列本地降级与防幻觉机制。这使得该插件不仅能运行在云端大模型上，在本地轻量级模型下也能提供更稳定的运行表现。

## 特性

### 核心能力 (继承自原版)
- 使用 AstrBot 原生 hooks：`on_llm_response`、`on_decorating_result`、`after_message_sent`。
- 只处理 LLM/Agent 的纯文本结果，不改变图片、语音、文件等非文本消息链。
- 支持括号动作/神态描述独立成段，并避免整段动作文本被继续拆分。

## 个人维护版增强特性

- **小模型稳定性重构**：改用轻量管道符协议、极简 Prompt（保留 3 条 Few-Shot）与幻觉熔断，显著降低小模型的格式崩溃与复读风险。
- **本地高精度切分与原文标点保留**：不依赖分段模型也能稳定分条；使用分段模型时以原文重建，确保原文标点 100% 保留。
- **复杂结构保护**：代码围栏、JSON、Markdown 表格/列表/引用、行内代码、链接/图片与短动作括号按优先级单遍隔离，块级独立成段、行内随文打包不拆；正文里的 `|`（如 `cat | grep`、`|x|`）会先被替换成占位符，等模型按 `|` 断句后再回填，避免与分段协议的 `|` 断句符冲突。
- 完整变更记录见 [CHANGELOG.md](CHANGELOG.md)。



## ⚙️ 测试环境与推荐配置（重要）

### 1. 风格兼容性声明
> ⚠️ **注意**：在开发与日常测试过程中，**维护者始终采用 `natural`（自然）分段风格**。由于未能对其他分段风格（如 `conservative` 或 `active`）进行充分的回归测试，我不确定切换风格后是否会产生意料之外的分段异常。**强烈建议保持使用 `natural` 风格以确保稳定性**。

### 2. 针对本地轻量模型（Qwen3.5-4B-MTP Q4KXL）的推荐配置参考
如果你的分段模型运行在本地（例如通过 Ollama 运行 `hf.co/unsloth/Qwen3.5-4B-MTP-GGUF:UD-Q4_K_XL`），以下是经过维护者实测并长期使用的稳定配置：

*   **分段模型 Provider ID**：`ollama/hf.co/unsloth/Qwen3.5-4B-MTP-GGUF:UD-Q4_K_XL`
*   **分段风格**：`natural`（强烈推荐）
*   **分段模型温度（Temperature）**：`0`（建议设为0。对于本地小模型，降低随机性是防止格式崩溃最有效的手段）
*   **最大分段数量**：`12`
*   **单条消息最大字符数（`max_chars_per_segment`）**：`180`
*   **Markdown 清洗模式（`markdown_clean_mode`）**：`light`（日常聊天清洗轻量标记，JSON、代码围栏、表格等重度结构跳过；如需测试全部清洗可临时改为 `heavy`）
*   **分段模型超时时间 (秒)**：`20`（本地运行由于硬件性能或首字延迟加载较慢，建议放宽至 20 秒）
*   **补发基础延迟 (秒)**：`2`
*   **按字符增加延迟 (秒/字符)**：`0.05`
*   **单段最大补发延迟（秒）**：`3`
*   **不想调用模型时**：开启 `use_local_only=true`，此时 `provider_id`、`temperature`、`max_tokens`、`timeout_seconds` 等模型参数可忽略。

## 配置说明（全局列表）

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `enabled` | 启用/禁用插件 | `true` |
| `use_local_only` | 仅使用本地高精度切分，不调用分段模型 | `false` |
| `provider_id` | 分段模型 Provider ID，留空使用当前会话模型 | `""` |
| `style` | `natural` / `conservative` / `active` | `natural` |
| `min_length` | 文本短于该长度不分段 | `15` |
| `max_segments` | 单次最多分段数量 | `8` |
| `max_chars_per_segment` | 单条消息最大字符数，超过后按标点二次拆分 | `180` |
| `markdown_clean_mode` | `off` / `light` / `heavy`；light 只清洗轻量标记并跳过重度结构，heavy 清洗全部 Markdown 标记 | `light` |
| `temperature` | 分段模型温度 | `0.3` |
| `max_tokens` | 分段模型最大输出 token | `600` |
| `timeout_seconds` | 分段模型超时时间 | `12.0` |
| `delay_base` | 补发基础延迟（秒） | `0.35` |
| `delay_per_char` | 按字符增加的延迟（秒/字符） | `0.015` |
| `delay_max` | 单段最大补发延迟（秒） | `1.2` |

> 💡 **使用建议**：建议关闭 AstrBot 内置的 `platform_settings.segmented_reply.enable`，避免其与本插件产生重复分段。

## 架构说明

架构图如下，箭头表示调用/写入方向，不画返回方向：

![智能分段插件架构图](assets/architecture.png)

- `on_llm_response` 只负责预计算，不发送消息。
- `on_decorating_result` 负责替换首段并登记补发，不直接发送剩余段。
- `after_message_sent` 在首段发送成功后启动剩余段异步补发。
- 缓存未命中时，`on_decorating_result` 会调用本地高精切分兜底。
- heavy 清洗使用内容保全式正则，保留链接 URL、行内代码与 JSON/代码块正文。

## 技术实现与 AI 辅助说明

本项目的核心分段逻辑与优化策略（包含协议重构、幻觉阻断、Markdown 保护等）在编写与重构过程中，参考并使用了 AI 模型进行代码逻辑验证与优化建议。

- **免责声明**：虽然我已经对代码进行了多轮个人测试与场景验证，确保其在本地 Qwen3.5-4B-MTP Q4KXL 环境下的稳定性，但由于代码逻辑复杂，仍不能保证在所有大模型、所有上下文环境下绝对无偏差。使用本插件产生的任何意外输出或逻辑问题，请用户根据实际情况进行调试或反馈。
- **开源精神**：本项目旨在解决个人在本地小模型使用中的实际痛点。代码完全开源，欢迎开发者审查代码逻辑，提交 Issue 或 PR 共同完善。

## 测试

- 从插件根目录运行全部测试：`python -m unittest discover -s tests -v`
- 或直接运行：`python tests/test_segmentation.py`

## 许可证

GPL-3.0-or-later
