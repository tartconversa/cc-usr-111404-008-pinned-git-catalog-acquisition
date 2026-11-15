"""合成 Git 对象库与回环目录 API，供固定领取测试使用。

对象全部在内存中按真实 Git 格式构造，并用系统 git 命令交叉核对哈希；
不连接任何真实企业或个人。服务只绑定 127.0.0.1。
"""
import base64
import hashlib
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app import gitobj


def git_hash_object(type_name, content):
    """系统 git 的权威对象 ID，用于核对自实现的哈希。"""
    proc = subprocess.run(['git', 'hash-object', '-t', type_name, '--stdin'],
                          input=content, capture_output=True, check=True)
    return proc.stdout.decode().strip()


class ObjectDB:
    """内存 Git 对象库：oid -> (type, content)。"""

    def __init__(self):
        self.objects = {}
        self.refs = {}                 # 完整 ref 名 -> (oid, 类型)
        self.lfs = {}                  # sha256 oid -> 真实字节

    def add(self, type_name, content, *, expect_id=None):
        oid, stored = gitobj.git_object_id(type_name, content)
        # 与系统 git 的实现交叉验证，确保类型/长度/哈希口径一致。
        assert git_hash_object(type_name, content) == oid
        assert gitobj.parse_object(stored, expected_type=type_name, expected_id=oid) \
            == (type_name, content)
        self.objects[oid] = (type_name, content)
        if expect_id is not None:
            assert oid == expect_id
        return oid

    def add_blob(self, content):
        return self.add('blob', content)

    def add_tree(self, entries):
        """entries: [(mode, path, oid)]；自动规范排序后建对象。"""
        content = gitobj.build_tree(entries)
        return self.add('tree', content)

    def add_commit(self, tree, parents=(), message=b'test\n'):
        lines = [b'tree ' + tree.encode()]
        for parent in parents:
            lines.append(b'parent ' + parent.encode())
        lines.append(b'author Tester <t@example.test> 1700000000 +0000')
        lines.append(b'committer Tester <t@example.test> 1700000000 +0000')
        content = b'\n'.join(lines) + b'\n\n' + message
        return self.add('commit', content)

    def add_tag(self, target_oid, target_type, name='v1'):
        content = (b'object ' + target_oid.encode() + b'\n'
                   + b'type ' + target_type.encode() + b'\n'
                   + b'tag ' + name.encode() + b'\n'
                   + b'tagger Tester <t@example.test> 1700000000 +0000\n\n'
                   + b'release\n')
        oid = self.add('tag', content)
        return oid

    def set_ref(self, full_ref, oid, ref_type):
        self.refs[full_ref] = (oid, ref_type)

    def add_lfs(self, data):
        oid = hashlib.sha256(data).hexdigest()
        self.lfs[oid] = data
        return oid

    @staticmethod
    def lfs_pointer(oid, size):
        return (b'version https://git-lfs.github.com/spec/v1\n'
                + b'oid sha256:' + oid.encode() + b'\n'
                + b'size ' + str(size).encode() + b'\n')


class FakeGitServer:
    """实现 pinned.GitDirectoryClient 约定的回环目录 API。

    可通过 fault 开关模拟：truncated 清单、缺对象、LFS 缺对象、
    引用被强推（改 refs）/删除。
    """

    def __init__(self, db, *, truncated_trees=(), missing_objects=(),
                 missing_lfs=(), serve_raw_objects=True):
        self.db = db
        self.truncated_trees = set(truncated_trees)
        self.missing_objects = set(missing_objects)
        self.missing_lfs = set(missing_lfs)
        self.serve_raw_objects = serve_raw_objects
        self.requests = []
        self.lock = threading.Lock()

    def __enter__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, data=b'', content_type='application/json'):
                self.send_response(code)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(data)

            def do_GET(self):
                with owner.lock:
                    owner.requests.append(self.path)
                parts = [p for p in self.path.split('/') if p]
                try:
                    if len(parts) >= 2 and parts[0] == 'refs':
                        ref = '/'.join(parts)
                        if ref in owner.db.refs:
                            sha, ref_type = owner.db.refs[ref]
                            self._send(200, json.dumps(
                                {'sha': sha, 'type': ref_type}).encode())
                            return
                        # 允许直接以 40 位 SHA 查询，返回对象自身类型。
                        if len(parts) == 2 and gitobj.is_sha1(parts[1]) \
                                and parts[1] in owner.db.objects:
                            obj_type = owner.db.objects[parts[1]][0]
                            self._send(200, json.dumps(
                                {'sha': parts[1], 'type': obj_type}).encode())
                            return
                        self._send(404, b'{}'); return
                    elif len(parts) == 2 and parts[0] == 'objects':
                        oid = parts[1]
                        if not owner.serve_raw_objects or oid in owner.missing_objects \
                                or oid not in owner.db.objects:
                            self._send(404, b'{}', 'application/octet-stream'); return
                        type_name, content = owner.db.objects[oid]
                        _id, stored = gitobj.git_object_id(type_name, content)
                        self._send(200, stored, 'application/octet-stream')
                    elif len(parts) == 2 and parts[0] == 'trees':
                        oid = parts[1]
                        if oid not in owner.db.objects:
                            self._send(404, b'{}'); return
                        type_name, content = owner.db.objects[oid]
                        assert type_name == 'tree'
                        entries = gitobj.parse_tree(content)
                        truncated = oid in owner.truncated_trees
                        # API 清单使用六位模式（与 GitHub/GitLab 口径一致）。
                        rows = [{'path': p, 'mode': m,
                                 'type': gitobj.MODE_TO_TYPE[m], 'sha': o}
                                for m, p, o in entries]
                        if truncated:
                            rows = rows[:1]
                        self._send(200, json.dumps(
                            {'sha': oid, 'truncated': truncated, 'tree': rows}).encode())
                    elif len(parts) == 2 and parts[0] == 'blobs':
                        oid = parts[1]
                        if oid in owner.missing_objects or oid not in owner.db.objects:
                            self._send(404, b'{}'); return
                        type_name, content = owner.db.objects[oid]
                        assert type_name == 'blob'
                        self._send(200, json.dumps(
                            {'content_base64': base64.b64encode(content).decode()}).encode())
                    elif len(parts) == 2 and parts[0] == 'lfs':
                        oid = parts[1]
                        if oid in owner.missing_lfs or oid not in owner.db.lfs:
                            self._send(404, b'{}', 'application/octet-stream'); return
                        self._send(200, owner.db.lfs[oid], 'application/octet-stream')
                    else:
                        self._send(404, b'{}')
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.base_url = 'http://127.0.0.1:%d' % self.server.server_port
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError('回环来源未停止')


def build_sample_db():
    """构造含普通文件、可执行、链接、子树、子模块和 LFS 指针的对象库。"""
    db = ObjectDB()
    manual = db.add_blob('设备说明书正文 v1\n'.encode())
    exec_blob = db.add_blob('#!/bin/sh\necho ok\n'.encode())
    link_blob = db.add_blob(b'manuals/device.md')
    lfs_real = b'PDF-DATA' * 100
    lfs_oid = db.add_lfs(lfs_real)
    pointer = db.add_blob(db.lfs_pointer(lfs_oid, len(lfs_real)))
    sub_blob = db.add_blob(b'{"sub":"module-file"}\n')
    sub_tree = db.add_tree([(gitobj.MODE_BLOB, 'nested.json', sub_blob)])
    sub_commit = db.add_commit(sub_tree)
    gitmodules = db.add_blob(
        b'[submodule "vendor"]\n\tpath = vendor\n'
        + b'\turl = https://vendor.example/test.git\n')
    inner_tree = db.add_tree([(gitobj.MODE_BLOB, 'device.md', manual)])
    root = db.add_tree([
        (gitobj.MODE_BLOB, '.gitmodules', gitmodules),
        (gitobj.MODE_TREE, 'manuals', inner_tree),
        (gitobj.MODE_BLOB, 'manuals/device.pdf', pointer),
        (gitobj.MODE_EXEC, 'tools/check.sh', exec_blob),
        (gitobj.MODE_LINK, 'docs-link', link_blob),
        (gitobj.MODE_GITLINK, 'vendor', sub_commit),
    ])
    commit = db.add_commit(root)
    db.set_ref('refs/heads/main', commit, 'commit')
    tag = db.add_tag(commit, 'commit', name='v1.0')
    db.set_ref('refs/tags/v1.0', tag, 'tag')
    # 标签指向标签的两层结构。
    outer_tag = db.add_tag(tag, 'tag', name='release-1')
    db.set_ref('refs/tags/release-1', outer_tag, 'tag')
    return db, {'root': root, 'commit': commit, 'tag': tag, 'outer_tag': outer_tag,
                'sub_commit': sub_commit, 'sub_tree': sub_tree, 'sub_blob': sub_blob,
                'manual': manual, 'pointer': pointer, 'lfs_oid': lfs_oid,
                'lfs_size': len(lfs_real), 'link_blob': link_blob,
                'exec_blob': exec_blob, 'gitmodules': gitmodules}
