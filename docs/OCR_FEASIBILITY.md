# OCR 可行性调查

调查日期：2026-10-29。范围是本次 macOS 开发环境和现有 df-mcp Python 运行环境；不代表其他电脑、Python 环境或 Windows/Linux 的可用性。

## 实际检查

| 组件 | 观察结果 | 能证明什么 |
| --- | --- | --- |
| Poppler `pdftoppm` | 可执行，版本 26.09.0 | 具备将 PDF 页面渲染为图片的工具；不提供文字识别 |
| `pypdf` | 现有运行环境可导入 | 可以读取 PDF 文本层；不等于识别扫描图片 |
| Tesseract、OCRmyPDF、Ollama | 当前 PATH 未找到，Homebrew 安装目录也未见对应组件 | 本次没有可以直接调用的这些本地服务；没有安装或下载 |
| Python OCR 相关包 | 当前环境未检测到 pytesseract、EasyOCR、PaddleOCR、Paddle、PyMuPDF、Pillow、torch、transformers、onnxruntime、OCRmyPDF | 尚不能在当前 Python 环境中直接验证这些方案 |
| macOS Vision、Swift | Swift 可编译并加载相关框架；实际识别请求失败 | 不能据框架存在宣称 OCR 已可用或稳定 |

## macOS Vision 合成样本尝试

仅在临时目录生成三张图片：清晰中文、清晰英文、小字中文，包含金额、千位分隔符、小数、百分号和日期。图片已目视检查，确实包含预期文字。没有读取团队材料。

使用 `VNRecognizeTextRequest`，设置 accurate、中英文语言、关闭语言纠正。三组请求都返回 `Foundation._GenericObjCError` 的 `nilError`，错误码为 0，没有识别文本。CPU-only 复试结果相同。现有证据不足以确定是系统服务、执行环境还是其他条件导致失败。

**本次未取得可评分的 OCR 输出，因此不报告 OCR 准确率，也不能确认可以稳定接入。** 调查脚本与样本未进入生产解析流程。

## 后续验证条件

OCR 暂不启用，不改变当前解析器。若后续批准实施，应先选定并配置可运行的本地识别方案，再使用具有人工标准答案的代表性扫描资料验证：中文与英文、低清与旋转页面、金额与百分数、表格及跨页注释。分别统计文字、关键数字和定位错误，以及失败页与耗时；几张合成样本通过不构成生产稳定性证明。

未来 OCR 结果需要标明识别来源、原文件版本与页码，保留失败和缺失范围，并与原生文本及模型生成描述区分。关键事实仍需回核原件。
