# 开放设备资料Git目录的对象固定与引用保真采集

现有组件读取一份来源响应，输出当前概览及字节摘要。尚不具备持续领取或跨响应业务归并能力。
app/source.py保留响应状态、重复头和实体原字节，一次请求不自动跟随重定向；app/baseline.py解释当前单响应，app/service.py将概览绑定到来源身份及摘要。
app/lab.py提供回环HTTP来源，支持GET、POST、HEAD、PROPFIND和REPORT，记录收到的请求，用于在可控来源上联调。fixtures为合成资料，不连接真实企业或个人。
来源协议、字段身份及当前边界见[来源契约](docs/source-contract.md)。Python 3.14，现有代码仅使用标准库。

读取当前响应概览：

    python -m app.service fixtures/sample.json

运行现有模块检查：

    python -m unittest discover -s tests -v

检查源码编译：

    python -m compileall -q app tests

如需单独启动本地来源，运行下列命令后读取打印的回环地址；结束时按Ctrl+C：

    python -m app.lab fixtures/sample.json

## 按固定提交领取资料

app/gitobj.py实现Git对象原语（按类型与长度计算SHA-1、tree/tag/commit解析、规范tree重建、LFS v1指针识别）；app/pinned.py在目录API上完成固定领取：先校验允许的分支/标签/提交并解析到唯一提交（带注释标签逐层解到commit、检测环路），再沿该提交的tree与对象取回配置目录。tree清单truncated时回对象端点取完整tree，模式与类型不一致、哈希不符、对象缺失都判失败或保留未满足。符号链接只登记链接目标；子模块只按显式仓库映射继续且不执行仓库代码；LFS未授权、缺对象或oid/size不符时保留指针与未满足状态，不把指针文本当说明书。对象按ID去重落盘支持断点恢复，收据记录来源提交、路径、模式、类型、对象ID与LFS状态，逐项可核验。

    python -m app.pin 配置.json refs/heads/main ./对象库目录 来源身份

配置字段：base_url、allowed_refs（必填）、config_path、allow_lfs、lfs_base_url、submodule_sources、workers、可选token（只用于请求头）。全部满足退出0，存在未满足项退出2。tests/fakegit.py提供合成Git对象库与回环目录API，对象ID均经系统git交叉核对。
