# Changelog

AstrBot 智能分段插件（增强版）的版本变更记录。

格式遵循 [Keep a Changelog 2.0.0](https://keepachangelog.com/en/2.0.0/)，版本号遵循 [Semantic Versioning](https://semver.org/spec/v2.0.0.html)。

## [1.5.8] - 2026-11-28

正文不被清洗或切分吞掉，新增两个配置项。

### Added

- `strip_thinking_tags`（默认开启）：开启时删除代码块外的 `<thinking>…</thinking>` 整段，关闭后插件完全不碰 thinking 标签。AstrBot 4.29.0 之前需要开启；4.29.0 起宿主会自己处理，可关闭以避免插件改写正文。
- `qq_official_follow_up`（默认关闭）：QQ 官方（非 webhook）平台默认整条发送；AstrBot 4.20.0 及以上可开启分段补发。

### Changed

- 段数上限与单条长度上限无法同时满足时，优先保证段数上限，单条可能超过 `max_chars_per_segment`。

### Fixed

- 正文里出现未闭合的 `<thinking>` 时，后面的内容不再被整段删除。
- 行内代码里的 `|` 不再被强制切分切开：local 引擎与含受保护块的锚点路径都跳过行内代码、链接与 URL。
- 含代码块的回复里，只含 `|` 的片段改挂到下一段，不再拼到闭合围栏后把围栏行变成 ```` ```| ````。
- 配置全角 `｜` 作强制切分符号时，llm 引擎对半角 `|` 也会切分，与 local 引擎和配置页的全半角等价承诺一致。
- 括号之间只有一个空格时不再丢失，`version (beta) (final)` 不会粘成 `(beta)(final)`。
- 两侧都省略竖线的 Markdown 表格（`项目 | 状态`）在 heavy 清洗下也会去掉分隔行。
- `min_length: inf` 这类配置不再让设置解析抛异常，回落到默认值。
- QQ 官方（非 webhook）平台默认整条发送，旧版宿主不再出现只收到首段、其余分段静默丢失。
- 正文里出现与分段锚点同形的 `ZZSEG…` 字样时按普通文本处理，不再导致整条回复降级为本地切分。
- 代码块里的 `<tool_call>`、`<function_call>`、`[System:` 原样保留；未闭合的协议标签不再吞掉其后的整段回复。
- 引用里的代码块能正常闭合：`> ```python` 的闭围栏写成裸 ` ``` ` 时，后面的正文不再被并进代码块；引用正文后面的围栏（`> 示例：```python`）同样处理。
- `2*3*4`、`4*5` 这类乘法写法不再被当成斜体删成 `234`、`45`；`*斜体*` 仍正常清洗。
- heavy 清洗下 `>_<`、`><` 的 `>` 不再被 Markdown 渲染删掉；普通引用 `> 引用` 仍照常去掉标记。
- heavy 清洗下 `~~~` 围栏里的代码原样保留，不再删掉 `# 标题`、`---` 行或改写 `**粗体**`；闭合围栏比开启围栏长时（四个反引号闭合三个）也不再残留反引号。
- 段数需要压到上限时，正文不再拼到代码块的闭合围栏后面；含代码块的混合段改用空行分隔合并。
- 只对模型回复分段：`/help`、`/status` 等指令结果和其他插件生成的纯文本回复不再被插件拆条。
- 一次失败留下的空缓存条目不再压住同内容正常的分段结果，结果不再取决于两条缓存的先后顺序。
- 正文里的 `|` 不再因分段占位符被切开而丢失：还原数量对不上时整条改走本地切分。
- 上游插件在回复头尾追加的 emoji、标点不再被预处理分段替换吞掉，跟在首段或末段后面发出。
- 旧回复的首段卡住时，同一会话的新回复不再被旧补发队列占住会话锁最长 5 分钟才分段。

## [1.5.7] - 2026-11-27

分段不再重复、丢失原文或切坏动作段。

### Fixed

- 句子以颜文字或语气符号结尾时，紧跟的动作括号独立成条：`等了好久没回音，越想越急啊>_<（把手机放下）` 分成两条。
- 编号列表的序号不再被复制：`结论。1. 先备份。` 不会多发一个 `1`。
- `***`、`___` 这类纯符号片段不再从消息里消失；挂在动作括号前时，动作段仍独立成条。
- 符号间夹空格的分隔线（`* * *`、`_ _ _`、`- - -`）在 heavy 清洗下整行清掉，不再残留 `* *` 半行。
- 句末标点后带闭合引号时，括号仍按动作段独立：`她说“先这样。”（点头）然后挂断。` 分成三条。
- 英文单句点按句末处理：`Hello.（笑）World.` 的动作段独立成条；连续点号仍是停顿，`清单...（想了一会儿）可以。` 保持一条。
- 模型把全角括号从中间切开时恢复成完整动作段：`orz（沮丧）` 不再被切成 `orz（沮` + `丧）`。
- 模型路径下，段首的纯标点并入下一段，不再单独成条。
- 模型路径下，只含强制符号的片段挂回前一段末尾，不再单独成条。
- 含代码块的消息里，动作括号也会独立成条，不再因保护块存在而与普通消息分法不同。
- 强制切分符号不再切进行内代码与链接：`` `a|b` ``、`https://example.com/a|b` 保持完整。
- LLM 模式下配置 `|` 作为强制切分符号也能正常生效。
- 打开短尾合并时，强制切分产生的短尾不再被合回前段，强制切分仍然生效。
- 新消息先于旧回复登记补发时，旧回复的剩余分段也会加速发完，新回复不必等旧队列。
- 用户已在面板选择 `segmenter=llm` 时，首次迁移不再把它改回 `local`。
- 配置里的 0/1 按布尔解析：手写 `enabled: 0` 能真正关闭插件。

## [1.5.6] - 2026-11-27

颜文字不再与动作段粘成一条，消息原文不再被改动。

### Fixed

- 颜文字或波浪号紧贴动作括号时，动作段独立成条且顺序保持原文：`✨>_<（点头）你好呀。`、`～（叹气）算了。` 各自拆成三条。
- 全角括号前的字母不再触发函数调用避让：`qwq（难过）怎么办。` 能拆出独立的动作段；`split()`、`arr[0]` 仍不切。

### Changed

- 分段只切开原文，不改变顺序与内容；省略号、破折号与句读标点仍可并入相邻段，波浪号与颜文字保持独立。

### Removed

- 半角尖括号不再转义为全角（`<` → `＜`、`>` → `＞`）。纯文本链路（NapCat、Telegram 等）不解析 HTML，转义只会改动消息原文；代码块与行内代码本来就不受影响。若所在平台会把 `<...>` 当作 HTML 解析，需自行在提示词中约束。

## [1.5.5] - 2026-11-24

符号不再误删误切，追问不再等旧回复。

### Fixed

- 裸链接里的 `?`、`~` 不再被当成句末，消息里出现网址不会被切成两条。
- Markdown 清洗为 light 时，星号与波浪号不再被误删：`5 * 3 = 15`、`10~~20 度` 保持原样。
- 颜文字保持原样：`>_<` 不再变成 `＞_＜`；贴着字母的尖括号仍按原规则转义。
- 编号列表与英文缩写不再被拆错：`1. 打开设置 2. 关闭开关` 的序号跟着自己的条目，`e.g.`、`p.m.` 后的点号不再断句。
- 数字区间（`10~20`）不再在波浪号处断句。
- 漏写右括号时，后续句子仍正常分段，不再整条不分段。
- 空括号（`（）`、`()`、`[]`）不再被当成动作段：函数调用、占位符这类写法不会再单独成一条，也不会被切成碎气泡。
- 消息开头只有 `！？` 这类符号时，挂到下一句一起发，不再单独一条。
- 合并英文句子时保留句间空格，不再出现 `p.m.We`。
- `[At:…]`、`[Face:…]`、`[CQ:…]` 这类标记不再被当成动作括号单独成条。
- 补发发送超时后不再重试，避免同一条消息重复到达；明确失败最多重试一次。

### Changed

- 同一会话来了新消息时，上一条回复剩余的分段改为快速发出，追问的回复不必等旧消息按原节奏排完；剩余内容不丢弃。加速间隔可在面板配置（默认 0.2 秒），也可整体关闭。
- 官方 QQ、微信公众号与钉钉平台不再分段：这些平台的主动消息受配额或接口限制，改为整条发出，避免只发出第一段。

## [1.5.4] - 2026-11-04

会话与围栏边界更稳定。

### Fixed

- 会话锁等待与后台补发：各设 60 秒超时；等待超时后整条回复发出，发送超时重试一次并释放会话。
- 补发队列：过期分段被丢弃时记录 warning 与丢弃条数。
- 未闭合围栏：正文后直接跟三反引号时不再整段删除或吞并后续正文；带信息串的开启行不再留下只开不合的围栏。
- 四反引号围栏：内部整行三反引号不再提前闭合；`markdown` 容器内的语言标签围栏按嵌套块识别。
- 锚点位置：受保护块与行内代码、链接混排时，分段结果不再重复或错位。
- 强制符号：只含强制符号的片段挂回前一段末尾，不再单独成条。
- 本地保护词：`no_split_around` 在本地切分引擎下生效。
- 配置健壮性：`smart` 延迟的符号列表为标量时不再抛 `TypeError`。
- 分段回退：模型结果与原文仅存在空白差异时不再判为改写失败，含换行的回复不再整条改用本地切分。
- 行内代码：light 清洗不再把四反引号行内代码削成两个反引号。

## [1.5.3] - 2026-10-17

日志规范符合插件市场要求。

### Changed

- 内部：日志统一改用 `from astrbot.api import logger`，不再使用内置 `logging`。

## [1.5.2] - 2026-10-17

本地切分与 Markdown 边界修复。

### Fixed

- 本地切分保留 ASCII 句间空格与换行、段落分隔；`Hello. World.` 不再粘连，多行文本不再压成一行。
- 行内代码中的 `<`、`>` 保持原样，普通正文仍按原规则转义。
- 强制切分符号命中标点簇时整簇保留：`真的吗！！！` 不再拆成 `真的吗！` + `！` + `！`。
- 括号动作后的收尾标点归属动作段：`（点头）。` 独立成段。
- 引号内部的句末标点不再产生切点。
- Markdown 块与块间正文分别处理；相邻结构块合并不再超过 `max_chars_per_segment`。
- 包含式对齐补切沿用 `ellipsis_pause_min_chars`。
- `smart` 延迟符号冲突检测按 NFKC 与省略号长度归一，`！`/`!`、`……`/`...` 视为同一符号。
- 段数达到上限需要重排时，不再拆开动作段。

### Changed

- Markdown 块识别覆盖无首尾竖线的 GFM 表格、`~~~` 围栏、Setext 标题、列表缩进续行、紧贴正文的围栏与 `markdown` 嵌套围栏。

## [1.5.1] - 2026-09-15

运行时拆分为模块，行为不变。

### Changed

- 内部：`main.py` 按职责拆分为 `plugin/` 包，公开 API 与导入路径不变。

## [1.5.0] - 2026-09-14

新增本地切分、smart 延迟与保护。

### Added

- 完整句子分段：本地切分以完整句子为单元；单句超长时才按「句末标点 > 分号 > 逗号」切开。
- 省略号停顿独立成段：`……` 停顿段不再与后续句子粘连；句首省略号与短语气停顿留在原句。
- 配置 `ellipsis_pause_min_chars`：省略号停顿独立成段的最小长度，默认 4。
- `smart` 发送延迟：`delay_strategy` 新增 `smart`；按上一段结尾符号增减等待，省略号多等、感叹号后急促，符号与秒数可配置。
- 配置 `jitter_max_seconds`：`smart` 策略的随机抖动上限，默认 0.15 秒。
- 短尾合并：`merge_short_tail`（默认关）与 `short_tail_threshold`（默认 2）；末条短消息并入上一条，受保护内容不参与。
- 保护词：`no_split_around`（默认空）；切点后紧跟配置词时不切开，超过单条上限仍会切。
- 强制切分符号：`force_split_symbols`（默认空）；出现即分条，符号留在前一条末尾，受保护块内不切。
- 含受保护块的消息不再整条降级：代码围栏、JSON、表格等替换为锚点后调用模型，返回前还原；锚点异常时降级本地切分。
- 发送引擎配置 `segmenter`：`local` / `llm`，默认 `local`；模型相关配置仅在 `llm` 时显示。

### Changed

- `linear` 延迟不再有随机抖动，抖动改为 `smart` 专属。
- 补发基础延迟默认值 1.25 → 1.3 秒；任何间隔不低于 1 秒。
- `min_length` 默认值 12 → 10。
- 句末标点或括号后的省略号归下一条开头，避免符号连排在段尾。

### Deprecated

- `use_local_only` 已弃用并从界面隐藏；旧值自动映射到 `segmenter`（`true` → `local`，`false` → `llm`），行为不变，1.x 保留旧键，计划 2.0 再评估移除。

## [1.4.1] - 2026-09-11

分段代码拆分为模块，行为不变。

### Changed

- 内部：`segmentation.py` 拆分为 `segmentation/` 包，公开 API 与导入路径不变。

## [1.4.0] - 2026-09-02

修复缓存失配与括号切分。

### Fixed

- Tool Loop 场景缓存失配：工具调用后文本含插入内容时，按内容签名定位预处理分段，仅补切头尾，不再整条重切。
- 句中括号：正文内括号数字、缩写、插入语不再独立成段；句首动作与句末标点后的动作仍独立成段。
- 空分段缓存：超时、异常、无 provider 的空分段不再参与拼合，避免中间文本被吞。
- 强制合并段内空行：动作段与正文直接连排；受保护块之间保留空行。
- 对齐边界标点：首尾标点在插入内容切分时不再重复。
- 边界值：`max_chars_per_segment=0` 时 `max_segments` 仍生效。

### Changed

- 对齐补切段数预算与已有分段联动，总段数尽量不超 `max_segments`。
- 内部：删除死代码、抽取公共函数。
- 配置缺失键的兜底默认值与 `_conf_schema.json` 统一。

## [1.3.6] - 2026-08-21

修复段数上限、锁泄漏与补发丢失。

### Fixed

- `max_segments` 被原子段绕过：动作括号、JSON、Markdown 等原子段过多时，兜底合并仍保证总段数不超上限，并保留原文。
- 补发挂起时同会话锁泄漏：装饰前清理过期待发任务，并增加后台清理；`after_message_sent` 未触发时也会释放。
- 补发失败导致后续消息丢失：单条失败有限重试，最终失败后继续补发其余分段。
- 装饰阶段缓存失配导致整条重切：预分段结果绑定当前事件，其他插件追加内容时只补切后缀。

### Changed

- 恢复 heavy Markdown 清洗：优先用 `markdown-it-py` 与 `mdit-plain`；依赖缺失时改用保留内容的兜底清洗。
- 配置默认值与推荐配置同步：`provider_id`、`min_length`、`max_segments`、`timeout_seconds` 与补发延迟。

## [1.3.5] - 2026-08-18

修复空行分隔句子被重新合并。

### Fixed

- 模型用空行 `\n\n` 分隔完整句子时，补切阶段保留段落边界，不再把两句粘回同一条。

### Changed

- 目标长度改为按实际句长动态计算；短句切得短、长句保持完整，仍受 `max_segments` 约束。

## [1.3.4] - 2026-08-16

修复段首标点归属。

### Fixed

- 模型分段段首的引号、省略号、`？！` 等前导标点归属后续内容段，不再残留在段尾。
- 本地补切产生的纯前导标点同样随后续内容发送，不再粘回普通前段。

## [1.3.3] - 2026-08-16

修复动作括号被切碎。

### Fixed

- 跨行动作括号整体独立成段，不再与台词粘连。
- 长动作括号与 JSON、Markdown 块一样不可拆分，超长也保持完整。
- 模型在括号内插入 `|` 时按原文顶层括号区间重建边界，不再把动作与说话粘连。

### Added

- 本地切分新增问号、感叹号硬分段边界：句末 `？`、`！` 及其组合视为强制分条，不再因短句合并粘在同一条。

## [1.3.2] - 2026-08-15

修复模型短句被重新合并。

### Fixed

- 模型已用 `|` 分出的短段不再被后续打包重新合并；仅拆分超长段，超过段数上限时才合并。

## [1.3.1] - 2026-08-14

移除动作括号长度限制。

### Fixed

- 长动作括号（超过 30 字符）也独立成段，不再与后续台词粘连。

## [1.3.0] - 2026-08-14

全语法原子块保护与 Prompt 精简。

### Added

- 全语法原子块保护：代码围栏、JSON、Markdown 表格、列表、引用、行内代码、链接、图片与短动作括号统一保护；块级原子独立成段，行内原子随文本打包但不拆。
- 管道符前置掩码：分段前把正文内联 `|` 替换为占位符，解析后回填，避免与 Linux 管道、数学绝对值冲突。

### Changed

- 分段 Prompt 精简，保留 3 条 Few-Shot，降低小模型注意力稀释与复读。
- 动作括号长度限制：仅不超过 30 字符的括号动作独立成段，更长括号保持正文不拆。

### Fixed

- `>` 开头颜文字不再被误判为 Markdown 引用。
- `max_segments` 作为全局上限生效；修复中文段间插入空格导致长度膨胀。
- heavy Markdown 清洗不再丢失链接 URL，宿主与容器行为一致。

## [1.2.2] - 2026-08-13

前导标点归属规则调整。

### Changed

- 前导标点归属按「动作括号段 > 说话/正文段 > 前导标点」处理，覆盖中文省略号、ASCII `...`、中黑点 `·`、`？！` 等段首标点，不新增配置。

### Fixed

- 模型给出 `（动作） | ……说话` 边界时，前导标点不再粘回动作段。

## [1.2.1] - 2026-08-13

移除括号补齐自愈。

### Changed

- 移除逐段补齐 `()`、`[]`、`{}` 的自愈逻辑；原文标点重建已能恢复模型丢掉的括号。

### Fixed

- 模型在括号内部切错时不再复制改写括号（如 `（开 | 心）` 不再补成 `（开）（心）`），保留原文切片边界。

## [1.2.0] - 2026-08-12

新增 Markdown 保护与清洗模式。

### Added

- 动态目标段长：按总字数、`max_segments`、`max_chars_per_segment` 计算，用平衡打包替代尾部合并，消除长文末段堆积。
- JSON 引号感知保护：跳过字符串与转义后定位最外层 `{}`、`[]`，JSON 块整体输出；无成对括号时回退普通分段。
- Markdown 块级保护：代码围栏、连续表格、列表、引用整块保护；围栏未闭合时保护到文本末尾。
- 重度 Markdown 整篇放行；混合文本只保护 Markdown 块，块外正文正常分段。
- `markdown_clean_mode`：`off`、`light`、`heavy`；light 清洗轻量标记并跳过重度结构，heavy 清洗全部标记并保留正文；依赖缺失时降级为保留内容的兜底清洗。

### Changed

- 分段 Prompt 增加分段数量限制，目标长度由代码层决定。
- 本地切分与模型兜底共用 JSON、Markdown 保护与平衡打包。
- `requirements.txt` 新增 `markdown-it-py` 与 `mdit-plain` 依赖。

### Fixed

- 检测到 JSON、代码围栏、表格、列表或引用时跳过 LLM 分段，直接使用本地保护切分，避免小模型改写或吞块。
- 模型输出的 `|` 只作消息边界，不再按内部换行二次拆分。
- 正文与 JSON、代码块混排时不再因特殊字符密度被整条判为非自然语言。
- 连续标题、列表、引用、表格、分割线、围栏合并为整块；仅无语言标签的纯三反引号行作为围栏闭合，避免嵌套语言标签误判。

## [1.1.1] - 2026-08-10

修复模型漏切导致句子粘连。

### Changed

- 分段 Prompt 明确同一条消息不得粘连多个完整句子；句号、感叹号、问号结尾的句子之间必须拆条。

### Fixed

- 模型漏切后对自然语言段再应用本地句末边界补齐，完整句子不再挤在同一条。

## [1.1.0] - 2026-08-09

新增本地切分、标点重建与长度上限。

### Added

- 纯本地高精度切分模式（`use_local_only`）：不调用分段模型，直接用本地标点、括号规则切分。
- 原文标点重建：模型输出只作切分边界，消息文本从原文恢复，标点不再被小模型吞掉。
- `max_chars_per_segment`：单条消息长度上限，默认 180；超长分段按句末标点与括号规则二次拆分。
- 负向结果缓存：超时、异常、无 provider 与模型返回单段时写入缓存，发送阶段不再强行本地切分。

### Changed

- 分段 Prompt 重构：要求保留全部标点、动作单独一条、话语另起一条、单条长度适中，并强化最大分段数约束。
- 缓存匹配升级为内容签名容错：忽略空白、大小写与标点；Markdown 清洗或 Tool Loop 合并后的文本不再每次降级本地切分。
- 本地兜底先按括号拆动作段，再对剩余话语做句末切分。
- 移除半角括号强制全角化，避免代码、数学、链接被改写。

### Fixed

- 分段后标点被吞：通过内容签名对齐与原文重建，最终文本保留原始标点。
- Markdown 内嵌 JSON 未被识别为重度 Markdown：补充 JSON、配置块特征识别。
- 连续消息并发导致补发交错或新回复被跳过分段：同会话后续回复等待前一条补发完成后再处理。

## [1.0.2] - 2026-07-17

修复 QQ/微信颜文字被吞噬。

### Added

- 尖括号安全转义：仅对普通自然语言与颜文字段落的半角尖括号做全角化（`<` → `＜`、`>` → `＞`），避开代码块、JSON/YAML 与 Markdown 引用。

### Fixed

- QQ/微信平台把 `>_<` 等颜文字误判为 HTML 标签并整段吞噬、截断。

## [1.0.1] - 2026-07-12

修复中文括号切分与代码块误删。

### Changed

- 非自然语言判定与本地兜底顺序重构：Markdown 段落解析先于非自然语言判定；增加 JSON 解析尝试与汉字密度判定，避免结构化文档整条放行或正常中文被误判。
- 颜文字与纯标点判定：含中文字符的短句不再判为颜文字，避免正常中文被向前合并。

### Fixed

- 中文括号切分失效：括号前紧邻中文字符（如「了（」）不再被误判为代码函数调用。
- 通用 Markdown 代码块过度抹除：清洗规则不再拦截通用三反引号闭合代码块，避免合法代码块被提前过滤。

## [1.0.0] - 2026-07-12

首个版本：非自然语言拦截与本地兜底。

### Added

- 非自然语言拦截与本地切分兜底：发送 LLM 前拦截 JSON、代码块；超时或缓存失配时降级为本地标点、括号切分。
- Markdown 重度文本保护：识别标题、列表等复杂排版，改为按双换行分段，不破坏原文。
- 控制符过滤与空气泡消除：清洗 `---`、`***` 等无意义分割符，避免空消息气泡与风控。
- 大模型 Few-Shot 幻觉阻断：检测模型复读示例（如「今天发工资了」）时熔断并降级本地切分。
- 颜文字与连续标点粘连保护：`？！` 等连续标点不切碎；`qwq`、`(>_<)` 等提供粘连保护，避免孤立符号段。

### Changed

- 消息还原与三级降级检索：精确匹配 → 顺序组合 → 本地兜底。
- 协议由 JSON 改为轻量管道符 `|`，减少小模型格式崩溃与 token 消耗。
- 括号平衡改为单段内缺省强制补齐，避免后续段落被无限吸入合并。
- 超限截断合并时词语间改用空格拼接，防止物理粘连。
- Prompt 增加显式负面约束并扩充 Few-Shot，降低小模型指令遵循门槛。

### Fixed

- 推理过程泄露：非闭合容错匹配可剥离未闭合的 `<thinking>` 标签。
- 单引号与非标 JSON 解析容错：多级保底解析与尾部逗号清洗。
- 代码函数避让：括号左侧紧邻字母、数字时判定为代码语法，防止切碎 `split()`。
- 多轮 Tool Call 并发时不再残留旧分段。

[1.0.0]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.0.0
[1.0.1]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.0.1
[1.0.2]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.0.2
[1.1.0]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.1.0
[1.1.1]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.1.1
[1.2.0]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.2.0
[1.2.1]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.2.1
[1.2.2]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.2.2
[1.3.0]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.3.0
[1.3.1]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.3.1
[1.3.2]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.3.2
[1.3.3]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.3.3
[1.3.4]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.3.4
[1.3.5]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.3.5
[1.3.6]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.3.6
[1.4.0]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.4.0
[1.4.1]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.4.1
[1.5.0]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.5.0
[1.5.1]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.5.1
[1.5.2]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.5.2
[1.5.3]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.5.3
[1.5.4]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.5.4
[1.5.5]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.5.5
[1.5.6]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.5.6
[1.5.7]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.5.7
[1.5.8]: https://github.com/EVOL233awa/astrbot_plugin_smart_segmentation_enhanced/releases/tag/v1.5.8
