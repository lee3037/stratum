<div align="center">

<img src="web/public/favicon.svg" alt="Stratum logo" width="88">

# Stratum 层析

**把 PDF 析成可检索的知识块**

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](requirements.txt)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](app/server.py)
[![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black)](web/package.json)
[![PyMuPDF](https://img.shields.io/badge/PyMuPDF-AGPL--3.0-6366f1)](docs/deployment.md#许可证)

[特性](#特性) · [快速开始](#快速开始) · [文档](#文档)

<img src="docs/assets/poster.png" alt="Stratum 层析宣传海报" width="100%">

</div>

Stratum 层析把文本型和扫描型 PDF 转换为按语义分块的 Markdown。每块带有章节路径和页码，块之间用分隔符隔开。转换结果可以作为 RAG 知识库的前处理产物，导入任何支持自定义分隔符的知识库。

## 特性

- **本机解析**：使用 PyMuPDF + RapidOCR，只用 CPU，文件不离开服务器。
- **扫描件 OCR**：自动识别没有文字层或文字乱码的页。
- **标题层级**：系统根据编号体例、字号和书签目录还原章节结构。
- **跨页续接**：跨页、跨栏的段落和表格自动合并，页眉、页脚、页码自动删除。
- **语义分块**：系统按章节和段落分块，每块第一行写出章节路径和页码。
- **逐块校对**：导入前，你可以逐块编辑结果，并对照原 PDF。
- **知识库**：PDF 上传到服务器，按知识库分目录保存；每次解析生成新版本，保存在 PDF 旁边。
- **批量处理**：批量解析整个文件夹，结果自动保存；打包下载 zip，附汇总表。
- **导入 FastGPT**：每个知识库绑定一个 FastGPT 知识库，一键导入或批量导入，新版本自动替换旧集合。
- **多人共用**：单个解析优先于批量解析，同时解析的文件轮流执行；修改冲突检测、进程崩溃自动恢复、磁盘空间保护。

## 快速开始

前置条件：Python 3.12。浏览器不限。

```bash
./run.sh            # 默认端口 8000；第一次运行时自动创建 .venv 并安装依赖
./run.sh 9000       # 指定端口
```

1. 在浏览器中打开 `http://<服务器>:8000/`。
2. 选择一个知识库，或新建一个。
3. 点击左上的上传按钮，上传 PDF 或整个文件夹。
4. 在左侧列表中点击一个 PDF，在抽屉中点击「开始解析分块」。
5. 确认解析参数，点击「开始解析」。
6. 在「分块」视图中检查结果。
7. 点击「保存到知识库」。

文件保存在服务器的 `STRATUM_DATA_DIR` 目录中。服务器部署见[部署文档](docs/deployment.md)。

## 文档

| 文档 | 内容 |
| --- | --- |
| [功能详解](docs/features.md) | 解析与分块规则、输出示例、已知局限 |
| [使用指南](docs/guide.md) | 转换流程、知识库与版本、批量处理、导入 FastGPT |
| [解析配置](docs/configuration.md) | 解析方案、识别参数与分块参数 |
| [部署](docs/deployment.md) | Ubuntu 安装、环境变量、FastGPT、多人使用、运维、性能参考 |
| [开发](docs/development.md) | 目录结构、并发模型、测试、前端构建、品牌素材 |

> [!WARNING]
> 服务没有登录鉴权。依赖的 PyMuPDF 使用 AGPL-3.0 许可证。部署前阅读[部署文档](docs/deployment.md)。
