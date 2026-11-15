"""Git 对象格式原语：对象哈希、tree/标签/提交解析、LFS 指针识别。

只处理对象格式本身，不发起任何网络请求，也不执行对象里的任何内容。
Git SHA-1 对象标识为 sha1(b"<类型> <十进制长度>\\0<原内容>")，
与“对下载字节直接做普通哈希”不同；调用方必须按类型与长度核对。
"""
import hashlib
import re

SHA1_RE = re.compile(r'^[0-9a-f]{40}$')
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')

# tree 条目模式（八进制），只允许资料采集需要的四类，其余模式一律拒绝。
MODE_BLOB = '100644'
MODE_EXEC = '100755'
MODE_LINK = '120000'
MODE_TREE = '040000'
MODE_GITLINK = '160000'
KNOWN_MODES = frozenset((MODE_BLOB, MODE_EXEC, MODE_LINK, MODE_TREE, MODE_GITLINK))

# 模式 -> 条目对象类型（符号链接在对象层仍是 blob，区别只在模式）。
MODE_TO_TYPE = {
    MODE_BLOB: 'blob', MODE_EXEC: 'blob', MODE_LINK: 'blob',
    MODE_TREE: 'tree', MODE_GITLINK: 'commit',
}
TYPE_TO_MODES = {
    'blob': frozenset((MODE_BLOB, MODE_EXEC, MODE_LINK)),
    'tree': frozenset((MODE_TREE,)),
    'commit': frozenset((MODE_GITLINK,)),
}


def is_sha1(value):
    return isinstance(value, str) and SHA1_RE.match(value) is not None


def is_sha256(value):
    return isinstance(value, str) and SHA256_RE.match(value) is not None


def git_object_id(type_name, content):
    """按 Git 规则计算对象 ID，并返回应入库的完整对象字节（头+原内容）。"""
    if type_name not in ('blob', 'tree', 'commit', 'tag'):
        raise ValueError('Git 对象类型无效: %r' % (type_name,))
    if not isinstance(content, (bytes, bytearray)):
        raise ValueError('Git 对象内容必须是字节')
    header = ('%s %d\0' % (type_name, len(content))).encode('ascii')
    stored = bytes(header) + bytes(content)
    return hashlib.sha1(stored).hexdigest(), stored


def parse_object(stored, expected_type=None, expected_id=None):
    """拆解一份完整 Git 对象字节，校验头、长度与对象 ID。

    返回 (类型, 原内容)。任何一项不符都抛 ObjectError，
    保证调用方不会把传输封装或损坏字节当成可信对象。
    """
    nul = stored.find(b'\0')
    if nul < 0:
        raise ObjectError('对象缺少 Git 头')
    head = stored[:nul]
    try:
        type_name, length_text = head.decode('ascii').split(' ', 1)
        length = int(length_text)
    except (UnicodeDecodeError, ValueError):
        raise ObjectError('Git 对象头无效')
    if type_name not in ('blob', 'tree', 'commit', 'tag'):
        raise ObjectError('Git 对象类型无效: %r' % (type_name,))
    content = stored[nul + 1:]
    if length != len(content):
        raise ObjectError('对象头长度 %d 与实际内容 %d 不一致' % (length, len(content)))
    actual_id = hashlib.sha1(stored).hexdigest()
    if expected_id is not None and actual_id != expected_id:
        raise ObjectError('对象 ID 不匹配: 期望 %s 实际 %s' % (expected_id, actual_id))
    if expected_type is not None and type_name != expected_type:
        raise ObjectError('对象类型不匹配: 期望 %s 实际 %s' % (expected_type, type_name))
    return type_name, content


class ObjectError(ValueError):
    """Git 对象格式或哈希核对失败。"""


def normalize_mode(mode):
    """规范 API 展示形式的六位模式；目录在磁盘上写作 40000，这里统一 040000。"""
    if not isinstance(mode, str):
        raise ObjectError('文件模式不是字符串: %r' % (mode,))
    digits = mode
    if digits == '40000':
        digits = MODE_TREE
    if digits not in KNOWN_MODES:
        raise ObjectError('不支持的文件模式: %r' % (mode,))
    return digits


def parse_tree(content):
    """解析 tree 原内容为有序条目列表 (mode, path, oid)。

    磁盘格式：<模式> <路径>\\0<20 字节二进制 SHA-1>，逐条拼接。
    """
    entries = []
    pos = 0
    while pos < len(content):
        sp = content.find(b' ', pos)
        if sp < 0:
            raise ObjectError('tree 条目缺少模式分隔符')
        nul = content.find(b'\0', sp)
        if nul < 0 or nul - sp <= 1:
            raise ObjectError('tree 条目缺少路径')
        mode_text = content[pos:sp].decode('ascii', 'strict')
        path = content[sp + 1:nul].decode('utf-8', 'strict')
        if mode_text == '40000':
            mode = MODE_TREE
        elif mode_text in KNOWN_MODES:
            mode = mode_text
        else:
            raise ObjectError('tree 含不支持的模式: %r' % (mode_text,))
        oid_bytes = content[nul + 1:nul + 21]
        if len(oid_bytes) != 20:
            raise ObjectError('tree 条目对象 ID 长度不足')
        entries.append((mode, path, oid_bytes.hex()))
        pos = nul + 21
    if pos != len(content):
        raise ObjectError('tree 内容尾部有多余字节')
    return entries


def _disk_mode(mode):
    """Git 磁盘上的 tree 模式：目录省略前导零。"""
    return '40000' if mode == MODE_TREE else mode


def tree_sort_key(entry):
    """Git 排序规则：目录按名称加 '/' 参与比较。"""
    mode, path, _oid = entry
    return path + ('/' if mode == MODE_TREE else '')


def build_tree(entries):
    """把条目列表重建为规范 tree 原内容，供对象 ID 复核。

    会复制 Git 的模式写法与目录排序；重建结果的哈希必须等于来源 tree ID，
    否则说明传输层给的清单不完整或被改动（truncated 清单在此处即暴露）。
    """
    normalized = []
    seen = set()
    for mode, path, oid in entries:
        mode = normalize_mode(mode)
        if not is_sha1(oid):
            raise ObjectError('tree 条目对象 ID 无效: %r' % (oid,))
        if (not isinstance(path, str) or not path or '\x00' in path
                or path.startswith('/') or path.endswith('/')
                or any(part in ('', '.', '..') for part in path.split('/'))):
            raise ObjectError('tree 条目路径无效: %r' % (path,))
        if path in seen:
            raise ObjectError('tree 条目路径重复: %r' % (path,))
        seen.add(path)
        normalized.append((mode, path, oid))
    normalized.sort(key=tree_sort_key)
    out = bytearray()
    for mode, path, oid in normalized:
        out.extend(_disk_mode(mode).encode('ascii'))
        out.append(0x20)
        out.extend(path.encode('utf-8'))
        out.append(0x00)
        out.extend(bytes.fromhex(oid))
    return bytes(out)


def _parse_header_block(content):
    """解析 commit/tag 的头部键值块，支持 gpgsig 一类缩进行续行。"""
    headers = []
    lines = content.split(b'\n')
    i = 0
    while i < len(lines):
        line = lines[i]
        if line == b'':
            break
        if line[:1] == b' ':
            # 上一个头的续行（如 PGP 签名），原样并入但不单独成头。
            if headers:
                key, value = headers[-1]
                headers[-1] = (key, value + b'\n' + line)
            i += 1
            continue
        if b' ' not in line:
            raise ObjectError('对象头部缺少分隔符: %r' % (line[:32]))
        key, value = line.split(b' ', 1)
        headers.append((key.decode('ascii'), value))
        i += 1
    message = b'\n'.join(lines[i + 1:]) if i < len(lines) else b''
    return headers, message


def parse_commit(content):
    """解析提交：tree、父提交列表、作者/提交者与消息。"""
    headers, message = _parse_header_block(content)
    tree = None
    parents = []
    author = committer = None
    for key, value in headers:
        if key == 'tree':
            if tree is not None:
                raise ObjectError('提交含多个 tree 头')
            tree = value.decode('ascii')
        elif key == 'parent':
            parents.append(value.decode('ascii'))
        elif key == 'author':
            author = value
        elif key == 'committer':
            committer = value
    if not is_sha1(tree):
        raise ObjectError('提交缺少有效的 tree 引用')
    if any(not is_sha1(p) for p in parents):
        raise ObjectError('提交父对象 ID 无效')
    return {'tree': tree, 'parents': tuple(parents),
            'author': author, 'committer': committer, 'message': message}


def parse_tag(content):
    """解析带注释标签：目标对象、目标类型、标签名等。"""
    headers, message = _parse_header_block(content)
    target = target_type = name = tagger = None
    for key, value in headers:
        if key == 'object':
            if target is not None:
                raise ObjectError('标签含多个 object 头')
            target = value.decode('ascii')
        elif key == 'type':
            target_type = value.decode('ascii')
        elif key == 'tag':
            name = value.decode('utf-8')
        elif key == 'tagger':
            tagger = value
    if not is_sha1(target):
        raise ObjectError('标签缺少有效目标对象')
    if target_type not in ('commit', 'tag', 'blob', 'tree'):
        raise ObjectError('标签目标类型无效: %r' % (target_type,))
    return {'object': target, 'type': target_type, 'tag': name,
            'tagger': tagger, 'message': message}


LFS_VERSION = b'version https://git-lfs.github.com/spec/v1'


def parse_lfs_pointer(content):
    """识别 LFS v1 指针；不是指针时返回 None。

    指针是小型键值文本，oid 必须为 sha256、size 为十进制字节数。
    识别成功只代表“这是指针”，绝不代表资料本身已经领取。
    """
    if not content or len(content) > 1024 or not content.startswith(LFS_VERSION + b'\n'):
        return None
    lines = content.split(b'\n')
    if lines[0] != LFS_VERSION:
        return None
    oid = None
    size = None
    for line in lines[1:]:
        if line == b'':
            break
        if line.startswith(b'oid sha256:'):
            candidate = line[len(b'oid sha256:'):].decode('ascii', 'strict')
            if not is_sha256(candidate):
                return None
            oid = candidate
        elif line.startswith(b'size '):
            text = line[len(b'size '):].decode('ascii', 'strict')
            if not text.isdigit():
                return None
            size = int(text)
        # 其余自定义键（如 x-*）允许出现，不影响识别。
    if oid is None or size is None:
        return None
    return {'oid': oid, 'size': size}
