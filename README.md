# fastbuild

通过已有 STEP 器件、安装接口和参数化模板生成带固定件的外壳及装配 CAD。
底层复用 CadQuery / OpenCascade 与 cq_warehouse，不训练 CAD 模型。
外壳复用 cq_enclosure_builder；自然语言经本地模型或人工转成可审阅设计意图，
程序自动推导器件坐标和外壳尺寸。

## 快速开始

需要 Python 3.11+、Git；建议使用独立虚拟环境。

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/fastbuild demo --output build/demo
```

示例使用程序生成的**合成器件**，不是供应商实物数模。
命令生成 STEP 输入、YAML 参数、安装底板、装配 STEP 和几何检查报告。
输出目录必须不存在，避免覆盖已有成果或把旧文件误认为新结果。

## 推荐：外壳设计入口

```bash
.venv/bin/python tools/prepare_enclosure.py
.venv/bin/fastbuild enclose examples/enclosure-brief.md \
  --components examples/components.yaml \
  --plan examples/enclosure-intent.json --accept-plan \
  --output build/enclosure
```

上面使用已经人工整理的中文需求及对应计划，不需要本地模型。
依赖前面的 demo 生成合成 STEP；真实输入只需替换器件清单及接口。
CSV 准备脚本是上游库要求的安装步骤，只写入当前虚拟环境且不覆盖不同数据。

产物包含盒体、带定位裙边的可拆盖、安装柱、底部六角螺栓限位孔、
盖板安装耳、标准 M3 螺栓/螺母和通风/矩形开孔。器件不需要手填 `position`。
打开 `build/enclosure/preview.html` 查看静态装配/开盖线框图，
或用 FreeCAD 打开 `assembly.step` / `exploded.step` 交互查看。

任意中文需求通过 `fastbuild plan` 调用**本地 Ollama** 转换：

```bash
.venv/bin/fastbuild plan my-brief.md --components examples/components.yaml \
  --model YOUR_LOCAL_MODEL --output build/my-plan.json
```

必须先安装并启动本地模型，审阅计划后再用 `enclose --accept-plan` 执行；
不会默认上传云端或把一句话直接当成可靠工程方案。
完整说明见 [docs/外壳与自然语言.md](docs/外壳与自然语言.md)。

## 低层底板入口（保留兼容）

使用自己的模型：

```bash
.venv/bin/fastbuild build examples/design.yaml --output build/my-design
```

先将 `examples/design.yaml` 中的模型路径和安装参数替换为真实数据。
模型路径相对于 YAML 文件；所有尺寸为 mm，器件安装底面必须位于局部 `Z=0`，
安装孔沿 Z 贯穿。当前仅支持 XY 平移和绕 Z 旋转，不自动猜孔位或搜索布局。

## 当前范围

- YAML / JSON Schema 校验；STEP 实体、安装孔和支撑落脚面检查。
- 按显式布局生成一体式底板、安装柱与标准间隙孔。
- 检查实体有效性、非预期重叠、器件间隙、底板边距和禁入空间。
- 导出独立底板和保留组件的装配 STEP，并重新导入核对实体数、体积及位置。
- 输出参数快照、输入文件摘要与检查报告。

**不是生产放行工具**：外壳入口已建模标准紧固连接及局部工具空间，但尚未验证
完整装配路径、预紧力、强度、打印工艺、FEA 或原生 FreeCAD 特征树。
低层底板入口仍只有间隙孔。报告会明确列出未执行项。

完整调研、选型和分阶段方案见 [docs/方案与实施.md](docs/方案与实施.md)。
MCP 和 FreeCAD 使用方法见 [docs/使用指南.md](docs/使用指南.md)。
