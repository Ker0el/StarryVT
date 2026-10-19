# Substance 3D Designer
Adobe 的程序化纹理制作软件（原 Allegorithmic）。中文教程里常说 SD 或「Designer」。

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
- [本名] Substance 3D Designer / Substance 3D Designer
- [收录范围] 只收 Designer（程序化纹理）的节点名与工作流；Painter 另有一条。
- 节点名一律用官方中文的写法；英文节点名第一次出现时保留英文原名，方便对上教程画面。
- [别名] Substance Designer、SD、Designer

## 节点与工作流

- Substance 3D Designer|Substance 3D Designer|SD、Substance Designer|程序化纹理软件；ASR 常听成 Substance Design / Substand Designer
- Graph|图表|节点图|节点网络，官方中文名「图表」
- Node|节点||官方中文名「节点」
- Generator|生成器||黑盒节点，参数化生成图案
- Filter|滤镜|过滤器|处理输入的节点，官方中文名「滤镜」
- Blend|混合||把两路输入合起来
- Smart Material|智能材质||Painter 里可复用的材质预设
- Smart Mask|智能遮罩||Painter 里可复用的遮罩预设
- Mask|遮罩|蒙版|官方中文名「遮罩」
- Baker|烘焙器|烘焙|把模型信息烘成贴图，官方中文名「烘焙器」
- Mesh Map|网格贴图|模型贴图|从模型烘出来的信息图（法线、AO、曲率…）
- Normal Map|法线贴图||标准叫法
- Roughness|粗糙度|Roughness 粗糙度|PBR 通道，别译成「粗糙」
- Metallic|金属度|金属性|PBR 通道
- Ambient Occlusion|环境光遮蔽|AO|PBR 通道
- Curvature|曲率||曲面起伏信息，常用作遮罩
- Height|高度|高度图|高度通道
- UV|UV|UV 坐标|保留英文
- Tiling|平铺|重复|图案无缝重复
- Tile Sampler|平铺采样器|Tile Sampler 节点|撒砖块/石块的常用节点
- Transform|变换|Transform 节点|位移旋转缩放
- Levels|色阶|Levels 节点|对比度调整
- Histogram|直方图|直方图扫描|Histogram Scan 节点用于重映射
- Parameter|参数||公开给上层的输入
- Expose|公开|暴露参数|官方中文名「公开」
- Instance|实例||参数化复用节点
- Graph Instance|图表实例||把整张子图当节点用
- Pixel Processor|像素处理器|像素处理|逐像素写逻辑的节点
- FX-Map|FX-Map|FX Map|用分形/循环控制图块分布的节点，保留英文
- Flood Fill|洪水填充|Flood Fill 节点|按坡度/连通区上色
- Splatter|散布|散布圆|撒圆形图案
- Polygon|多边形||程序化多边形形状
- Shape|形状|Shape 节点|基础形状节点
- Resolution|分辨率|图幅|图表分辨率，别译成「解决办法」
- Output|输出|输出节点|最终输出节点
- Substance Archive|Substance 归档|.sbsar、sbsar|打包好的参数化文件，后缀 .sbsar

## 元数据

最近更新日期: 2026-10-13
