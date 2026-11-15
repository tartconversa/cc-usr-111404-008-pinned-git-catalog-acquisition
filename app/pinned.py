"""按固定提交从 Git 目录 API 领取配置资料。

设计目标是“来源提交、路径与实际内容一一可核验”：

1. 只接受配置允许的分支或标签（或允许的提交 SHA），先把引用解析到唯一提交；
   带注释标签是 tag 对象，可能指向另一个标签，必须逐层解到 commit，
   不能把标签对象自身的 SHA 当成提交。
2. 沿该提交的 tree 与对象逐目录领取。tree API 返回 truncated=true 时清单
   不完整，必须改用对象端点取完整 tree，不能拿残缺列表当结果。
3. 每个对象都按 Git 规则（类型+长度+原内容）重算 SHA-1 并与来源 ID 核对，
   不是对下载字节做普通哈希。
4. 保留路径、文件模式与对象类型；符号链接只登记链接目标；子模块只按显式
   仓库映射继续，且只做对象读取，不执行任何仓库代码。
5. LFS 指针与真实资料分开：未授权、缺对象或核对失败时保留“未满足”状态，
   绝不把指针文本当作说明书内容。
6. 分支被强推或删除都不影响已固定的版本；新一轮采集另写一份收据。
"""
import base64
import concurrent.futures
import configparser
import hashlib
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import gitobj
from .gitobj import (ObjectError, git_object_id, is_sha1, normalize_mode,
                     parse_commit, parse_lfs_pointer,
                     parse_tag, parse_tree, build_tree,
                     MODE_LINK, MODE_TREE, MODE_GITLINK)

MAX_TAG_DEPTH = 20          # 标签解引用最大层数，防止标签环
MAX_SUBMODULE_DEPTH = 8     # 子模块嵌套深度
DEFAULT_WORKERS = 4
MAX_HTTP_BYTES = 64 * 1024 * 1024


class PinError(ValueError):
    """固定领取流程无法满足（引用不允许、清单不完整、对象缺失等）。"""


class TransportError(PinError):
    """来源 HTTP 通信失败或返回了不可接受的状态。"""


# ---------------------------------------------------------------------------
# 传输层：一次请求、不跟跳转、有读取预算，与 app.source.fetch 同一安全口径。
# ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Transport:
    """最小 HTTP 传输：禁用代理与自动跳转，限制单次响应字节数。"""

    def __init__(self, *, timeout=5, token=None, user_agent='device-doc-pin/1'):
        self.timeout = timeout
        self.token = token
        self.user_agent = user_agent
        self.request_log = []

    def get(self, url, *, accept='application/vnd.git+json', max_bytes=MAX_HTTP_BYTES):
        headers = {'User-Agent': self.user_agent, 'Accept': accept}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        request = urllib.request.Request(url, headers=headers, method='GET')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self.request_log.append(url)
        try:
            stream = opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            stream = exc
        with stream:
            data = stream.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise TransportError('响应超过读取预算: %s' % url)
            return stream.code, dict(stream.headers.items()), data


# ---------------------------------------------------------------------------
# 来源配置与本地对象库
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PinConfig:
    """一次固定领取的授权范围。

    base_url            目录 API 根地址
    allowed_refs        允许的分支/标签/提交，形如 refs/heads/x、refs/tags/y、
                        简写 heads/x、tags/x，或 40 位提交 SHA
    config_path         只领取该目录前缀（不含前导/）；None 表示整棵树
    allow_lfs           是否允许把 LFS 指针兑现为真实资料
    submodule_sources   {子模块仓库 URL: 该子模块的来源配置 dict}，
                        未出现在映射里的 gitlink 一律保持未满足
    lfs_base_url        LFS 直取基址（测试/受控来源用），None 表示不兑现
    workers             并行遍历线程数
    """
    base_url: str
    allowed_refs: frozenset
    config_path: str = None
    allow_lfs: bool = False
    submodule_sources: dict = field(default_factory=dict)
    lfs_base_url: str = None
    workers: int = DEFAULT_WORKERS

    @staticmethod
    def build(base_url, allowed_refs, **kwargs):
        if not isinstance(allowed_refs, (list, tuple, frozenset, set)) or not allowed_refs:
            raise ValueError('允许引用列表不能为空')
        normalized = frozenset(normalize_ref(r) for r in allowed_refs)
        config_path = kwargs.get('config_path')
        if config_path is not None:
            config_path = _normalize_config_path(config_path)
            kwargs['config_path'] = config_path
        return PinConfig(base_url=base_url.rstrip('/'), allowed_refs=normalized, **kwargs)


def _normalize_config_path(path):
    """规范配置目录前缀：拒绝对根/空路径、空段与 . / ..，防止越界路径。"""
    if not isinstance(path, str) or not path or path.startswith('/'):
        raise ValueError('配置目录路径无效')
    cleaned = path.strip('/')
    parts = cleaned.split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise ValueError('配置目录路径含非法段: %r' % path)
    return cleaned


def normalize_ref(ref):
    """把简写引用规范为完整 ref 名；40 位 SHA 原样保留。"""
    if not isinstance(ref, str) or not ref:
        raise ValueError('引用名无效')
    if is_sha1(ref):
        return ref
    if ref.startswith('refs/'):
        if not (ref.startswith('refs/heads/') or ref.startswith('refs/tags/')):
            raise ValueError('只允许分支或标签引用: %s' % ref)
        return ref
    if ref.startswith('heads/'):
        return 'refs/' + ref
    if ref.startswith('tags/'):
        return 'refs/' + ref
    # 裸名称按分支处理，避免与标签名混淆时由完整名显式声明。
    return 'refs/heads/' + ref


class ObjectStore:
    """按对象 ID 去重的本地对象库，支持断点恢复与并行写入。

    布局：<root>/objects/<前2位>/<其余38位> 存完整 Git 对象（头+内容）；
    收据与清单写在 <root>/receipts/ 下。已存在且哈希复核通过的对象不重复下载。
    """

    def __init__(self, root):
        self.root = Path(root)
        self.objects_dir = self.root / 'objects'
        self.receipts_dir = self.root / 'receipts'
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.receipts_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def path_for(self, oid):
        return self.objects_dir / oid[:2] / oid[2:]

    def has(self, oid):
        path = self.path_for(oid)
        if not path.is_file():
            return False
        # 已落盘对象仍要重算哈希，防止本地字节被替换。
        return hashlib.sha1(path.read_bytes()).hexdigest() == oid

    def load(self, oid):
        stored = self.path_for(oid).read_bytes()
        type_name, content = gitobj.parse_object(stored, expected_id=oid)
        return type_name, content

    def put(self, oid, type_name, content):
        """原子写入对象；写入前后都核对 Git 对象 ID。"""
        actual, stored = git_object_id(type_name, content)
        if actual != oid:
            raise ObjectError('对象哈希与来源 ID 不一致: 期望 %s 实际 %s' % (oid, actual))
        target = self.path_for(oid)
        with self._lock:
            if target.is_file():
                existing = target.read_bytes()
                if hashlib.sha1(existing).hexdigest() != oid:
                    raise ObjectError('本地对象已损坏: %s' % oid)
                return False
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix('.tmp-%d' % os.getpid())
            tmp.write_bytes(stored)
            os.replace(tmp, target)
            return True


# ---------------------------------------------------------------------------
# Git 目录 API 客户端
# ---------------------------------------------------------------------------

class GitDirectoryClient:
    """只做对象读取的目录 API 客户端。

    URL 约定（可由受控来源实现，app.lab 风格的回环服务即可联调）：
      GET <base>/refs/heads/<name> 等完整 ref 路径
                                  -> {"sha": "...", "type": "tag|commit"}
      GET <base>/objects/<oid>     -> 完整 Git 对象（application/octet-stream）
      GET <base>/trees/<oid>       -> {"truncated": bool,
                                       "tree": [{"path","mode","type","sha"}]}
      GET <base>/blobs/<oid>       -> {"content_base64": "..."}（传输封装）
      GET <lfs_base_url>/<sha256>  -> LFS 真实资料字节
    任何 4xx/5xx 都作为可记录的失败，不猜内容、不静默跳过。
    """

    def __init__(self, config, transport=None):
        self.config = config
        self.transport = transport or Transport()

    def _url(self, *parts):
        return self.config.base_url + '/' + '/'.join(
            urllib.parse.quote(p, safe='') for p in parts)

    def get_ref(self, ref):
        # ref 已是 refs/heads/x 形式的完整引用名，直接作为路径后缀。
        ref_path = urllib.parse.quote(ref, safe='/')
        code, _headers, body = self.transport.get(
            self.config.base_url + '/' + ref_path)
        if code != 200:
            raise TransportError('引用 %s 读取失败: HTTP %d' % (ref, code))
        try:
            payload = json.loads(body)
            sha = payload['sha']
            ref_type = payload.get('type')
        except (ValueError, KeyError, TypeError) as exc:
            raise TransportError('引用 %s 响应结构无效' % ref) from exc
        if not is_sha1(sha):
            raise TransportError('引用 %s 返回的对象 ID 无效' % ref)
        if ref_type not in ('commit', 'tag'):
            raise TransportError('引用 %s 类型不受支持: %r' % (ref, ref_type))
        return sha, ref_type

    def get_object(self, oid):
        """取完整 Git 对象字节并按来源 ID 核对，返回 (类型, 原内容)。"""
        code, _headers, body = self.transport.get(
            self._url('objects', oid), accept='application/octet-stream')
        if code == 404:
            raise ObjectMissing(oid)
        if code != 200:
            raise TransportError('对象 %s 读取失败: HTTP %d' % (oid, code))
        return gitobj.parse_object(body, expected_id=oid)

    def get_tree_listing(self, oid):
        """取目录 API 的 tree 清单；truncated 时明确返回，由调用方降级。"""
        code, _headers, body = self.transport.get(self._url('trees', oid))
        if code != 200:
            raise TransportError('tree %s 读取失败: HTTP %d' % (oid, code))
        try:
            payload = json.loads(body)
            truncated = bool(payload['truncated'])
            rows = payload['tree']
        except (ValueError, KeyError, TypeError) as exc:
            raise TransportError('tree %s 响应结构无效' % oid) from exc
        entries = []
        for row in rows:
            try:
                mode = normalize_mode(row['mode'])
                path = row['path']
                entry_type = row['type']
                entry_sha = row['sha']
            except (KeyError, TypeError) as exc:
                raise TransportError('tree %s 条目字段缺失' % oid) from exc
            if not is_sha1(entry_sha):
                raise TransportError('tree %s 条目对象 ID 无效' % oid)
            if entry_type not in gitobj.TYPE_TO_MODES or mode not in gitobj.TYPE_TO_MODES[entry_type]:
                raise ObjectError('tree 条目类型与模式不一致: %s %s %s' % (path, mode, entry_type))
            if not isinstance(path, str) or not path or path.startswith('/') or '\x00' in path:
                raise ObjectError('tree 条目路径无效: %r' % (path,))
            entries.append((mode, path, entry_sha))
        return truncated, entries

    def get_blob_wrapped(self, oid):
        """取 JSON 封装的 blob 原内容并核对哈希。"""
        code, _headers, body = self.transport.get(self._url('blobs', oid))
        if code == 404:
            raise ObjectMissing(oid)
        if code != 200:
            raise TransportError('blob %s 读取失败: HTTP %d' % (oid, code))
        try:
            payload = json.loads(body)
            content = base64.b64decode(payload['content_base64'], validate=True)
        except (ValueError, KeyError, TypeError) as exc:
            raise TransportError('blob %s 封装结构无效' % oid) from exc
        actual, _ = git_object_id('blob', content)
        if actual != oid:
            raise ObjectError('blob 传输封装哈希不符: 期望 %s 实际 %s' % (oid, actual))
        return content


class ObjectMissing(PinError):
    """来源缺少该对象（HTTP 404 或 LFS 缺对象）。"""

    def __init__(self, oid):
        super().__init__('来源缺少对象: %s' % oid)
        self.oid = oid


# ---------------------------------------------------------------------------
# 引用解析：允许列表 -> 唯一提交
# ---------------------------------------------------------------------------

def resolve_pinned_commit(client, ref_name):
    """把允许的引用解析到固定提交 SHA。

    带注释标签是 tag 对象，其 SHA 是标签对象而非提交；标签还可能指向另一个
    标签，因此循环解引用并检测环路，最终类型必须是 commit。
    分支头直接就是 commit。一旦解析完成，后续只认这个提交 SHA，
    分支被强推或删除都不会改变它。
    """
    full_ref = normalize_ref(ref_name)
    if is_sha1(full_ref):
        # 直接固定提交：优先经引用端点核对（来源可能只开放 API），
        # 拿不到时再取对象核对类型，全程不依赖当前分支状态。
        try:
            sha, ref_type = client.get_ref(full_ref)
        except TransportError:
            type_name, _content = client.get_object(full_ref)
            if type_name != 'commit':
                raise PinError('固定 SHA %s 不是提交对象（实际 %s）'
                               % (full_ref, type_name))
            return full_ref, 'commit-sha'
        if sha != full_ref or ref_type != 'commit':
            raise PinError('固定提交 SHA 与来源引用结果不一致: %s' % full_ref)
        return full_ref, 'commit-sha'
    sha, ref_type = client.get_ref(full_ref)
    if ref_type == 'commit':
        type_name, _ = client.get_object(sha)
        if type_name != 'commit':
            raise PinError('引用 %s 声明 commit 但对象类型为 %s' % (full_ref, type_name))
        return sha, full_ref
    # tag 对象：逐层解引用。
    seen = set()
    current = sha
    for depth in range(MAX_TAG_DEPTH + 1):
        if current in seen:
            raise PinError('标签解引用出现环路: %s' % full_ref)
        seen.add(current)
        type_name, content = client.get_object(current)
        if type_name == 'commit':
            return current, full_ref
        if type_name != 'tag':
            raise PinError('引用 %s 最终指向 %s，不是提交' % (full_ref, type_name))
        tag = parse_tag(content)
        current = tag['object']
    raise PinError('标签解引用超过 %d 层: %s' % (MAX_TAG_DEPTH, full_ref))


def select_ref(config, requested_ref):
    """校验调用方请求的引用确实在允许列表内。"""
    full = normalize_ref(requested_ref)
    if full not in config.allowed_refs:
        raise PinError('引用不在允许列表内: %s' % requested_ref)
    return full


# ---------------------------------------------------------------------------
# 固定领取主流程
# ---------------------------------------------------------------------------

@dataclass
class EntryRecord:
    """一条资料的可核验记录：路径、模式、类型与对象/链接状态一一对应。"""
    path: str
    mode: str
    object_type: str
    oid: str = None
    link_target: str = None
    lfs: dict = None
    status: str = 'satisfied'          # satisfied | unsatisfied
    detail: str = None
    submodule_repo: str = None
    submodule_receipt: str = None

    def to_dict(self):
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Receipt:
    source_id: str
    requested_ref: str
    resolved_ref: str
    commit: str
    tree_root: str
    config_path: str
    entries: list
    unsatisfied: list
    object_count: int
    notes: list

    def to_dict(self):
        return {
            'source_id': self.source_id,
            'requested_ref': self.requested_ref,
            'resolved_ref': self.resolved_ref,
            'commit': self.commit,
            'tree_root': self.tree_root,
            'config_path': self.config_path,
            'object_count': self.object_count,
            'satisfied': len(self.entries) - len(self.unsatisfied),
            'unsatisfied_count': len(self.unsatisfied),
            'entries': [e.to_dict() for e in self.entries],
            'unsatisfied': list(self.unsatisfied),
            'notes': list(self.notes),
        }


class PinnedCollector:
    """按固定提交遍历 tree 领取资料，输出可核验收据。"""

    def __init__(self, config, store, source_id, transport=None):
        self.config = config
        self.store = store
        self.source_id = source_id
        self.client = GitDirectoryClient(config, transport)
        self.entries = []
        self.unsatisfied = []
        self.notes = []
        self._seen_oids = set()
        self._lock = threading.Lock()
        self.root_oid = None      # 本次固定提交的根 tree，供仓库内路径查找

    # -- 对象获取：优先本地库（恢复/复用），否则向来源领取并核对落盘 -------

    def _fetch_object(self, oid):
        # 本地库已有且哈希复核通过就直接复用（断点恢复/对象复用），不再回源；
        # 这样来源删对象或暂时不可用也不影响已固定版本的还原。
        if self.store.has(oid):
            type_name, content = self.store.load(oid)
            with self._lock:
                self._seen_oids.add(oid)
            return type_name, content
        type_name, content = self.client.get_object(oid)
        self.store.put(oid, type_name, content)
        with self._lock:
            self._seen_oids.add(oid)
        return type_name, content

    def _fetch_blob(self, oid):
        """blob 支持两种来源：原始对象端点，或 JSON 封装端点；都要过哈希。"""
        try:
            type_name, content = self._fetch_object(oid)
        except ObjectMissing:
            content = self.client.get_blob_wrapped(oid)
            self.store.put(oid, 'blob', content)
            type_name = 'blob'
        if type_name != 'blob':
            raise ObjectError('对象 %s 不是 blob（实际 %s）' % (oid, type_name))
        return content

    def _load_tree(self, oid):
        """取完整 tree：truncated 清单绝不能当完整结果。

        顺序：先读目录 API 清单判断 truncated；完整清单经规范重建并核对
        哈希后可直接使用；一旦 truncated，必须回到对象端点取完整 tree，
        对象端点也缺失时直接失败，而不是接受残缺列表。
        """
        truncated, listed = self.client.get_tree_listing(oid)
        if not truncated:
            rebuilt = build_tree(listed)
            actual, _ = git_object_id('tree', rebuilt)
            if actual != oid:
                raise ObjectError('tree 清单重建哈希不符: 期望 %s 实际 %s' % (oid, actual))
            # 对象端点可用时再做一次双来源交叉核对；不可用则以已核验清单入库。
            try:
                type_name, content = self._fetch_object(oid)
            except ObjectMissing:
                self.store.put(oid, 'tree', rebuilt)
                self.notes.append('tree %s 无对象端点，采用已核验的完整 API 清单' % oid)
                return listed
            if type_name != 'tree':
                raise ObjectError('对象 %s 不是 tree（实际 %s）' % (oid, type_name))
            raw_entries = parse_tree(content)
            if sorted(raw_entries) != sorted(listed):
                raise ObjectError('tree %s 的 API 清单与对象内容不一致' % oid)
            return raw_entries
        self.notes.append('tree %s 目录 API 标记 truncated，改用完整对象清单' % oid)
        try:
            type_name, content = self._fetch_object(oid)
        except ObjectMissing:
            raise PinError('tree %s 标记 truncated 且对象端点缺失，无法取得完整清单' % oid)
        if type_name != 'tree':
            raise ObjectError('对象 %s 不是 tree（实际 %s）' % (oid, type_name))
        return parse_tree(content)

    # -- 主入口 -------------------------------------------------------------

    def collect(self, requested_ref):
        # 每次领取使用独立的结果容器：同一 collector 复跑不会累积旧条目，
        # 对象库本身仍可复用以支持断点恢复。
        self.entries = []
        self.unsatisfied = []
        self.notes = []
        self._seen_oids = set()
        ref = select_ref(self.config, requested_ref)
        commit_sha, resolved_ref = resolve_pinned_commit(self.client, ref)
        type_name, commit_content = self._fetch_object(commit_sha)
        if type_name != 'commit':
            raise PinError('固定结果不是提交: %s' % commit_sha)
        commit = parse_commit(commit_content)
        self.root_oid = commit['tree']
        self._walk(commit['tree'], prefix='', depth=0,
                   submodule_repo=None, submodule_config=self.config)
        # 统一按路径排序：并行下载与子模块条目的加入顺序都不影响收据可复现性。
        self.entries.sort(key=lambda r: r.path)
        self.unsatisfied.sort()
        receipt = Receipt(
            source_id=self.source_id, requested_ref=requested_ref,
            resolved_ref=resolved_ref, commit=commit_sha,
            tree_root=commit['tree'], config_path=self.config.config_path,
            entries=self.entries, unsatisfied=self.unsatisfied,
            object_count=len(self._seen_oids), notes=self.notes)
        return receipt

    def _walk(self, tree_oid, prefix, depth, submodule_repo, submodule_config):
        if depth > MAX_SUBMODULE_DEPTH:
            raise PinError('子模块嵌套超过 %d 层' % MAX_SUBMODULE_DEPTH)
        entries = self._load_tree(tree_oid)
        # 固定顺序让收据可复现，并行只用于下载，不影响输出顺序。
        entries = sorted(entries, key=gitobj.tree_sort_key)
        jobs = []
        for mode, name, oid in entries:
            path = name if not prefix else prefix + '/' + name
            if not self._under_config_path(path, mode):
                continue
            jobs.append((mode, path, oid))
        if submodule_config.workers > 1 and len(jobs) > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=submodule_config.workers) as pool:
                list(pool.map(lambda job: self._handle_entry(job, depth, submodule_repo, submodule_config), jobs))
        else:
            for job in jobs:
                self._handle_entry(job, depth, submodule_repo, submodule_config)

    def _under_config_path(self, path, mode):
        base = self.config.config_path
        if not base:
            return True
        if mode == MODE_TREE:
            return path == base or path.startswith(base + '/') or base.startswith(path + '/')
        return path == base or path.startswith(base + '/')

    def _handle_entry(self, job, depth, submodule_repo, submodule_config):
        mode, path, oid = job
        if mode == MODE_TREE:
            self._walk(oid, path, depth, submodule_repo, submodule_config)
            return
        if mode == MODE_LINK:
            # 符号链接只作为链接资料：登记目标字符串，不展开、不落真实内容。
            content = self._fetch_blob(oid)
            record = EntryRecord(path=path, mode=mode, object_type='blob', oid=oid,
                                 link_target=content.decode('utf-8', 'strict'),
                                 status='satisfied')
            self._append(record)
            return
        if mode == MODE_GITLINK:
            self._handle_submodule(path, oid, depth)
            return
        # 普通文件（100644/100755）：取内容，区分 LFS 指针与真实资料。
        content = self._fetch_blob(oid)
        record = EntryRecord(path=path, mode=mode, object_type='blob', oid=oid)
        pointer = parse_lfs_pointer(content)
        if pointer is not None:
            self._resolve_lfs(record, pointer, content)
        self._append(record)

    def _prefix_submodule_entries(self, parent_path, records):
        """把子模块收据里的相对路径改成主仓库视角的 gitlink 前缀路径。"""
        prefixed = []
        for record in records:
            cloned = EntryRecord(
                path=parent_path + '/' + record.path,
                mode=record.mode, object_type=record.object_type, oid=record.oid,
                link_target=record.link_target, lfs=record.lfs,
                status=record.status, detail=record.detail,
                submodule_repo=record.submodule_repo,
                submodule_receipt=record.submodule_receipt)
            prefixed.append(cloned)
        return prefixed

    def _append(self, record):
        with self._lock:
            self.entries.append(record)
            if record.status != 'satisfied' and record.path not in self.unsatisfied:
                self.unsatisfied.append(record.path)

    # -- LFS：指针与真实资料严格区分 ---------------------------------------

    def _resolve_lfs(self, record, pointer, pointer_bytes):
        """尝试把 LFS 指针兑现为真实资料。

        未授权、未配置 LFS 来源、缺对象、oid/size 不符：全部保留未满足状态，
        指针文本继续作为指针登记，绝不冒充说明书正文。
        """
        record.lfs = {'pointer_oid_sha256': pointer['oid'],
                      'pointer_size': pointer['size'],
                      'state': 'pointer-only'}
        if not self.config.allow_lfs:
            record.status = 'unsatisfied'
            record.detail = 'LFS 未授权领取，保留指针'
            return
        if not self.config.lfs_base_url:
            record.status = 'unsatisfied'
            record.detail = '已授权 LFS 但未配置 LFS 来源'
            return
        url = self.config.lfs_base_url.rstrip('/') + '/' + pointer['oid']
        try:
            code, _headers, data = self.client.transport.get(
                url, accept='application/octet-stream')
        except TransportError as exc:
            record.status = 'unsatisfied'
            record.detail = 'LFS 传输失败: %s' % exc
            return
        if code == 404:
            record.status = 'unsatisfied'
            record.detail = 'LFS 对象缺失: %s' % pointer['oid']
            return
        if code != 200:
            record.status = 'unsatisfied'
            record.detail = 'LFS 读取失败: HTTP %d' % code
            return
        # 核对指针承诺的 sha256 与字节数；两者任一不符即未满足。
        actual_oid = hashlib.sha256(data).hexdigest()
        if actual_oid != pointer['oid']:
            record.status = 'unsatisfied'
            record.detail = 'LFS oid 不符: 指针 %s 实际 %s' % (pointer['oid'], actual_oid)
            return
        if len(data) != pointer['size']:
            record.status = 'unsatisfied'
            record.detail = 'LFS size 不符: 指针 %d 实际 %d' % (pointer['size'], len(data))
            return
        lfs_blob_oid, _ = git_object_id('blob', data)
        lfs_dir = self.store.root / 'lfs'
        target = lfs_dir / pointer['oid'][:2] / pointer['oid'][2:]
        if target.exists():
            # 已落地的 LFS 资料同样重算 sha256，被替换就视为未满足。
            if hashlib.sha256(target.read_bytes()).hexdigest() != pointer['oid']:
                record.status = 'unsatisfied'
                record.detail = '本地 LFS 资料已损坏: %s' % pointer['oid']
                return
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix('.tmp-%d' % os.getpid())
            tmp.write_bytes(data)
            os.replace(tmp, target)
        record.lfs.update({'state': 'resolved',
                           'lfs_oid_sha256': actual_oid,
                           'lfs_size': len(data),
                           'content_git_blob': lfs_blob_oid,
                           'stored_at': str(target.relative_to(self.store.root))})
        record.detail = '指针 %d 字节已与真实资料分开登记' % len(pointer_bytes)

    # -- 子模块：只按显式映射继续，不执行仓库代码 --------------------------

    def _handle_submodule(self, path, gitlink_oid, depth):
        repo_url = self._submodule_url_for(path)
        record = EntryRecord(path=path, mode=MODE_GITLINK, object_type='commit',
                             oid=gitlink_oid, submodule_repo=repo_url)
        mapping = self.config.submodule_sources.get(repo_url) if repo_url else None
        if repo_url is None or mapping is None:
            record.status = 'unsatisfied'
            record.detail = '子模块未在显式仓库映射中，不继续领取'
            self._append(record)
            return
        sub_config = PinConfig.build(mapping['base_url'], mapping['allowed_refs'],
                                     config_path=mapping.get('config_path'),
                                     allow_lfs=mapping.get('allow_lfs', False),
                                     submodule_sources=mapping.get('submodule_sources', {}),
                                     lfs_base_url=mapping.get('lfs_base_url'),
                                     workers=mapping.get('workers', DEFAULT_WORKERS))
        sub_collector = PinnedCollector(sub_config, self.store,
                                        self.source_id + '|' + repo_url,
                                        transport=self.client.transport)
        # gitlink 条目记录的就是子模块仓库中被固定的提交，直接按提交 SHA 领取。
        if gitlink_oid not in sub_config.allowed_refs:
            record.status = 'unsatisfied'
            record.detail = '子模块固定提交 %s 不在该映射的允许列表内' % gitlink_oid
            self._append(record)
            return
        sub_receipt = sub_collector.collect(gitlink_oid)
        record.submodule_receipt = '%s@%s' % (repo_url, gitlink_oid)
        record.detail = '子模块按固定提交领取 %d 个对象，未满足 %d 个' % (
            sub_receipt.object_count, len(sub_receipt.unsatisfied))
        if sub_receipt.unsatisfied:
            record.status = 'unsatisfied'
        prefixed = self._prefix_submodule_entries(path, sub_receipt.entries)
        with self._lock:
            self.entries.extend(prefixed)
            self.unsatisfied.extend(path + '/' + u for u in sub_receipt.unsatisfied)
            self.notes.extend('子模块 %s: %s' % (repo_url, n) for n in sub_receipt.notes)
            self._seen_oids.update(sub_collector._seen_oids)
        self._append(record)

    def _submodule_url_for(self, path):
        """从已领取的 .gitmodules 内容找该 gitlink 的仓库 URL。

        .gitmodules 只被当作配置文本读取；任何 URL 都还要过显式映射这一关。
        """
        modules_path = '.gitmodules'
        try:
            type_name, content = self._fetch_blob_by_path(modules_path)
        except (ObjectMissing, KeyError):
            return None
        if type_name != 'blob':
            return None
        parser = configparser.ConfigParser()
        try:
            parser.read_string(content.decode('utf-8'))
        except (UnicodeDecodeError, configparser.Error):
            return None
        for section in parser.sections():
            if parser.get(section, 'path', fallback=None) == path:
                return parser.get(section, 'url', fallback=None)
        return None

    def _fetch_blob_by_path(self, rel_path):
        """按本次（子）仓库已固定的根 tree 解析一个仓库内路径。"""
        _type, root_content = self._fetch_object(self.root_oid)
        current = parse_tree(root_content)
        parts = rel_path.split('/')
        for part in parts[:-1]:
            match = next((o for m, p, o in current if p == part and m == MODE_TREE), None)
            if match is None:
                raise KeyError(rel_path)
            _t, subtree = self._fetch_object(match)
            current = parse_tree(subtree)
        oid = next((o for m, p, o in current if p == parts[-1]), None)
        if oid is None:
            raise KeyError(rel_path)
        return 'blob', self._fetch_blob(oid)


def _safe_source_id(source_id):
    """收据文件名用的来源标识：只保留安全字符，杜绝路径穿越。"""
    if not isinstance(source_id, str) or not source_id:
        raise ValueError('来源身份无效')
    cleaned = ''.join(ch if ch.isalnum() or ch in '._-' else '_' for ch in source_id)
    return cleaned[:128] or 'source'


def collect_pinned(config, store, source_id, requested_ref, transport=None):
    """便捷入口：执行一次固定领取并把收据原子写入对象库。

    同一提交+来源的收据按固定文件名覆盖（内容可由对象库复现）；
    不同提交天然落到不同文件，新一轮采集另记。
    """
    collector = PinnedCollector(config, store, source_id, transport)
    receipt = collector.collect(requested_ref)
    receipt_name = '%s-%s.json' % (receipt.commit, _safe_source_id(source_id))
    receipt_path = store.receipts_dir / receipt_name
    tmp = receipt_path.with_name(receipt_name + '.tmp')
    tmp.write_text(json.dumps(receipt.to_dict(), ensure_ascii=False, indent=2),
                   encoding='utf-8')
    os.replace(tmp, receipt_path)
    return receipt, receipt_path
