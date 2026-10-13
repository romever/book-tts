# Design QA

- Source visual truth: `/Users/liguoyang/.codex/visualizations/2026/09/22/01a0c8cb-2b99-7dc1-bb11-94219f43517e/book-tts-concept.png`
- Implementation screenshot: `/Users/liguoyang/.codex/visualizations/2026/09/22/01a0c8cb-2b99-7dc1-bb11-94219f43517e/book-tts-implementation.png`
- Combined comparison: `/Users/liguoyang/.codex/visualizations/2026/09/22/01a0c8cb-2b99-7dc1-bb11-94219f43517e/book-tts-comparison.png`
- Mobile screenshot: `/Users/liguoyang/.codex/visualizations/2026/09/22/01a0c8cb-2b99-7dc1-bb11-94219f43517e/book-tts-mobile.png`
- Viewport: 1487 × 1058 CSS px，deviceScaleFactor 1；移动端为 390 × 844 CSS px。
- Pixel dimensions: source 1487 × 1058；implementation 1487 × 1058；不需要密度归一化。
- State: 已登录，书稿解析完成，按章节选择第 1 章，范围尚未锁定。

## Full-view comparison evidence

- 信息架构：实现保持了设计稿的左侧四步流程、中央范围选择、右侧范围确认三栏结构。
- 布局节奏：桌面端三栏宽度约为 240 / 907 / 340 px，与设计稿的主要分区比例一致；页面无横向溢出。
- 字体与排版：使用系统中文字体栈，标题、正文、表头、表格行和操作按钮均设置了明确字号与字重；长书名在顶栏截断，不挤压操作区。
- 颜色与视觉令牌：白色主界面、浅蓝灰侧栏、深蓝正文、单一蓝色主操作与浅蓝选中态均与设计稿一致；未引入渐变或额外装饰。
- 图像质量：选定方案没有业务必需的图片资产；实现未使用占位插画、CSS 图标或内联 SVG 替代资产。
- 文案与内容：主标题、范围模式、步骤命名和锁定操作与设计稿一致；章节标题、字数、分段数和预计时长使用真实数据，属于必要的产品内容替换。

## Focused region comparison evidence

- 范围模式控件、章节表头、选中行、复选框、行高和分隔线已在聚焦对照中检查。
- 右侧范围摘要的数字层级、分隔线、主按钮宽度与辅助说明已检查。
- 左侧当前步骤选中态、未解锁步骤弱化和次级入口分组已检查。
- 核心按钮、输入和章节列表均有键盘焦点样式；原生复选框保持可访问语义。

## Interaction verification

- 章节范围从第 1 章扩展至第 2 章后，汇总正确更新为 2 章、51,632 字和 60 个分段；再次取消末章后恢复单章范围。
- 点击“锁定范围并继续”后，范围保持不变并进入“检查朗读稿”，后续每一步只处理锁定范围。
- “章节内容”可打开真实章节抽屉；第 1 章显示 22 个分段，其中 6 个已有音频播放器。
- 分段音频接口返回 HTTP 200；封面接口返回 HTTP 200。
- 桌面端和移动端均无页面横向溢出；浏览器捕获到的脚本错误为 0。

## Comparison history

1. 首轮发现实现顶部多出独立书籍摘要，导致工作区下移；已将书名收回顶栏并移除额外摘要区。
2. 首轮发现桌面工作区被包在居中卡片中，与设计稿的全宽应用框架不一致；已改为 240 / 1fr / 340 的全宽三栏布局。
3. 首轮发现章节列表正文略小；已把章节行和章节名称提升到 14px，并重新完成同尺寸截图对照。
4. 修复后重新捕获 1487 × 1058 实现图，与同尺寸设计稿并排检查，未发现仍需修复的 P0、P1 或 P2 问题。

## Remaining intentional deviations

- 概念图中的章节标题和统计是示例；实现使用当前书稿的真实数据。
- 概念图中的装饰性线性图标没有引入；现有项目没有匹配图标库，而核心操作不依赖这些图标，避免使用文本符号或手绘替代品。
- 顶栏保留“退出”，这是现有产品的真实会话操作。

## Follow-up polish

- P3：移动端制作步骤横向滚动，当前步骤完整可见；后续可根据真实手机使用频率决定是否改为更紧凑的两行步骤导航。

final result: passed
