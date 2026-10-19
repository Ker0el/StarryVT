# Houdini
SideFX 的程序化特效软件。没有官方中文界面，术语一律保留英文——中文社区（包括官方文档的翻译）都是直接用 SOP / Vellum / Karma 这些原词。

<!-- 以下自由节（节名自取）统一按这条规则收录——每节不再重复：
     用途：作品内的固定名词：ASR 易错、且有官方或约定中文定名的表层。节名自取（如「角色」「地区」「组织」）。
     不收：基础战斗名词与常驻通用词；不收版本性叙事（写进描述会很快过期，应改写成不随版本变化的一句话身份）。
     行式：可选 [标记] + 术语行 源语言|中文定名|别名|一句话描述——别名在第三列，无则留空；描述在最后，可含竖线。标记不是必须的，无标记行同样合法。 -->

## 档案

<!-- 用途：作品身份与本条目的收录边界。
     不收：剧情梗概与攻略。
     行式：可选 [标记] + 一句话内容。标记不是必须的，无标记行同样合法。
     标记（缺了完整预览会留一个空槽）：
       [本名] 源语言全名 / 中文定名 / 英文定名（源语言定名已是英文则留空），斜杠分隔；条目的 index 行由它派生
       [别名] 顿号分隔：昵称、简称、全称、俗称，以及源语言的特殊读音。覆盖社区常用写法（崩坏星穹铁道 → 崩铁、星铁），至少 2 字符；误听变体不算别名。这一行渲染自 items，删掉整行不等于清空别名
       [收录范围] 这个条目只收哪一部分——写清楚，它是 agent 的收录纪律；本地口径，不随条目外发
     登记的其他标记（可用，不提醒）：
       [当前版本] 易腐信息；只在「收录范围」需要它界定时才写 -->
- [本名] Houdini / Houdini
- [收录范围] 只收 Houdini 的节点上下文、解算器与渲染器名；不含具体 VEX 语法。
- 所以：这些词在译文里保持英文，不要音译成「胡迪尼」「维勒姆」。只做一件事——把 ASR 听错的拼写纠正回来。
- [别名] 火迪尼、Houdini FX、SideFX

## 节点与解算器

- Houdini|Houdini|SideFX Houdini|软件名，**保留英文**，别音译成「胡迪尼」
- SOP|SOP|几何节点|Surface Operator，几何上下文
- DOP|DOP|动力学|Dynamics Operator，动力学解算上下文
- CHOP|CHOP|通道节点|Channel Operator，通道/动画数据上下文
- TOP|TOP|任务节点|Task Operator，PDG 任务调度上下文
- COP|COP|合成节点|Compositing Operator；Houdini 20.5 起是 Copernicus
- VOP|VOP|VEX 节点|VEX Operator，连线写 VEX
- LOP|LOP|Solaris 节点|Lighting Operator，USD 上下文
- VEX|VEX||Houdini 的表达式语言，保留英文
- HDA|HDA|数字资产|Houdini Digital Asset，打包成可复用节点
- Vellum|Vellum|布料解算|布料/软体/颗粒解算器，保留英文；ASR 常听成 Velum / Vellam
- Karma|Karma|Karma 渲染器|Houdini 20 起的主渲染器，保留英文
- Mantra|Mantra||旧版主渲染器，保留英文
- Pyro|Pyro|烟火解算|烟火/爆炸解算器
- FLIP|FLIP|流体解算|流体解算器，保留大写
- RBD|RBD|刚体解算|Rigid Body Dynamics，刚体
- POP|POP|粒子节点|Particle Operator，粒子
- Attribute Wrangle|Attribute Wrangle|Wrangle|写 VEX 的节点，保留英文
- Attribute|属性|attrib|几何体上的数据（P / N / Cd…）
- Primitive|图元|prim|面/多边形图元
- Vertex|顶点||顶点
- Point|点||点
- Group|组||几何分组
- Copy to Points|复制到点|复制到点的节点|常用节点名
- Scatter|散布|撒点|撒点节点
- Remesh|重构网格|重网格|网格重构节点
- Boolean|布尔|布尔运算|布尔节点
- Solver|解算器||循环解算节点
- Cache|缓存||把解算结果存盘
- USD|USD||Universal Scene Description，保留英文
- Solaris|Solaris||Houdini 的 USD 光照与布局环境，保留英文
- Copernicus|Copernicus|COP 新架构|Houdini 20.5 的新合成系统
- Redshift|Redshift|RS|第三方 GPU 渲染器，保留英文
- Octane|Octane||第三方 GPU 渲染器，保留英文
- Geometry Spreadsheet|几何电子表格|表格视图|看属性数值的面板
- Network View|网络视图|节点网络|节点编辑器

## 元数据

最近更新日期: 2026-10-13
