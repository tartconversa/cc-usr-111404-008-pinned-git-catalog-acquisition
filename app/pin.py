"""固定提交领取的命令行入口。

用法：
    python -m app.pin <配置JSON> <引用> <对象库目录> [来源身份]

配置 JSON 字段与 PinConfig.build 对应：
    base_url, allowed_refs(列表), config_path(可选),
    allow_lfs(可选), lfs_base_url(可选), submodule_sources(可选),
    workers(可选), token(可选，仅用于传输头，不写入收据)

只输出收据 JSON 到标准输出并把对象与收据落到对象库目录；
来源凭据不出现在收据或资料目录中。
"""
import json
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.pinned import PinConfig, ObjectStore, Transport, collect_pinned
else:
    from .pinned import PinConfig, ObjectStore, Transport, collect_pinned


def run(config_path, requested_ref, store_dir, source_id='device-doc-source'):
    raw = json.loads(Path(config_path).read_text(encoding='utf-8'))
    token = raw.pop('token', None)
    config = PinConfig.build(**raw)
    store = ObjectStore(store_dir)
    transport = Transport(token=token)
    receipt, receipt_path = collect_pinned(
        config, store, source_id, requested_ref, transport=transport)
    output = receipt.to_dict()
    output['receipt_path'] = str(receipt_path)
    output['object_store'] = str(store.root)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    # 未满足项作为进程失败信号，便于调用方区分“领到全部”与“保留缺口”。
    return 0 if not receipt.unsatisfied else 2


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) not in (3, 4):
        raise SystemExit('用法: python -m app.pin <配置JSON> <引用> <对象库目录> [来源身份]')
    raise SystemExit(run(*argv))


if __name__ == '__main__':
    main()
