# 第三方组件（随镜像分发）

| 组件 | 版本 | 许可 | 用途 | 源码 |
|---|---|---|---|---|
| LibreDWG（`dwg2dxf`、`dxf2dwg`、`libredwg.so`） | 0.14 | GPL-3.0-or-later | 上传 DWG 时转换为 DXF（P5-S51）；参数化出图的实验性 DWG 输出（P6-S1，用户勾选才启用） | https://ftp.gnu.org/gnu/libredwg/libredwg-0.14.tar.gz（sha256 `cb6ee0b078c6d9e0f09d66f1feac33ba6342df88ae544e9f9335fab475218351`）；镜像内 `/opt/libredwg/share/COPYING`、`SOURCE` |

LibreDWG 以**独立进程**方式调用（`documents/dwg_convert.py`），没有链接进本项目的 Python 进程。分发镜像即分发了该 GPL 程序的二进制，因此附上其许可文本与对应源码的获取地址。
