"""固定提交领取的端到端核对：引用解析、truncated、模式保真、LFS、子模块、恢复。"""
import base64
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app import gitobj
from app.gitobj import (MODE_BLOB, MODE_EXEC, MODE_LINK, MODE_TREE, MODE_GITLINK,
                        git_object_id, parse_lfs_pointer, parse_tag, build_tree)
from app.pinned import (PinConfig, PinError, ObjectMissing, ObjectStore, TransportError,
                        PinnedCollector, collect_pinned, normalize_ref,
                        resolve_pinned_commit)
from tests.fakegit import FakeGitServer, build_sample_db


class PinnedTestBase(unittest.TestCase):
    def setUp(self):
        self.db, self.ids = build_sample_db()
        self.tmp = Path(tempfile.mkdtemp(prefix='pin-test-'))
        self.store = ObjectStore(self.tmp / 'store')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def config(self, **overrides):
        params = dict(
            base_url='http://placeholder',
            allowed_refs=['heads/main', 'tags/v1.0', 'tags/release-1', self.ids['commit']],
            workers=2)
        params.update(overrides)
        return PinConfig.build(**params)

    def collector(self, server, **overrides):
        cfg = self.config(**overrides)
        object.__setattr__(cfg, 'base_url', server.base_url)
        return PinnedCollector(cfg, self.store, 'source-a')

    def entry_by_path(self, receipt, path):
        rows = [e for e in receipt.entries if e.path == path]
        self.assertEqual(len(rows), 1, path)
        return rows[0]


class RefResolutionTests(PinnedTestBase):
    def test_branch_resolves_to_commit_and_tag_sha_is_not_commit(self):
        with FakeGitServer(self.db) as server:
            c = self.collector(server)
            receipt = c.collect('heads/main')
        self.assertEqual(receipt.commit, self.ids['commit'])
        self.assertEqual(receipt.tree_root, self.ids['root'])
        self.assertEqual(receipt.resolved_ref, 'refs/heads/main')
        # 标签对象 SHA 与提交 SHA 必须不同，证明没有把标签当提交。
        self.assertNotEqual(self.ids['tag'], self.ids['commit'])

    def test_annotated_tag_dereferences_through_tag_to_commit(self):
        with FakeGitServer(self.db) as server:
            c = self.collector(server)
            receipt = c.collect('tags/v1.0')
        self.assertEqual(receipt.commit, self.ids['commit'])
        # 两层标签：release-1 -> tag 对象 -> tag 对象 -> commit。
        with FakeGitServer(self.db) as server:
            c2 = self.collector(server)
            receipt2 = c2.collect('tags/release-1')
        self.assertEqual(receipt2.commit, self.ids['commit'])
        self.assertEqual(receipt2.resolved_ref, 'refs/tags/release-1')

    def test_tag_cycle_is_rejected(self):
        # 两个互相指向的标签构成解引用环；对象本身按真实格式构造，
        # 用客户端对象表覆盖引用关系，避免环导致哈希无法自洽。
        oid_a = 'a' * 40
        oid_b = 'b' * 40
        content_a = (b'object ' + oid_b.encode() + b'\ntype tag\ntag a\n\nx\n')
        content_b = (b'object ' + oid_a.encode() + b'\ntype tag\ntag b\n\nx\n')
        self.db.set_ref('refs/tags/loop', oid_a, 'tag')
        with FakeGitServer(self.db) as server:
            c = self.collector(server)
            c.config = PinConfig.build(server.base_url,
                                       ['heads/main', 'tags/loop'], workers=1)
            c.client.get_object = lambda oid: (
                ('tag', content_a) if oid == oid_a else ('tag', content_b))
            with self.assertRaisesRegex(PinError, '环路'):
                c.collect('tags/loop')

    def test_tag_chain_depth_limit(self):
        # 超长标签链（无环）也要在深度上限处失败，不能无限跟随。
        chain = {}
        first = '0' * 40
        current = first
        for i in range(25):
            nxt = ('%040x' % (i + 1))
            chain[current] = ('tag', b'object ' + nxt.encode()
                              + b'\ntype tag\ntag t\n\nx\n')
            current = nxt
        self.db.set_ref('refs/tags/long', first, 'tag')
        with FakeGitServer(self.db) as server:
            c = self.collector(server)
            c.config = PinConfig.build(server.base_url,
                                       ['heads/main', 'tags/long'], workers=1)
            c.client.get_object = lambda oid: chain[oid]
            with self.assertRaisesRegex(PinError, '超过'):
                c.collect('tags/long')

    def test_ref_not_in_allow_list_rejected(self):
        self.db.set_ref('refs/heads/secret', self.ids['commit'], 'commit')
        with FakeGitServer(self.db) as server:
            c = self.collector(server)
            with self.assertRaisesRegex(PinError, '允许列表'):
                c.collect('heads/secret')

    def test_force_push_and_branch_deletion_keep_pinned_version(self):
        with FakeGitServer(self.db) as server:
            c = self.collector(server)
            first = c.collect('heads/main')
            # 来源侧强推：分支指向另一棵树的新提交。
            other_blob = self.db.add_blob(b'new content after force-push\n')
            other_tree = self.db.add_tree([(MODE_BLOB, 'devices.json', other_blob)])
            new_commit = self.db.add_commit(other_tree)
            self.db.set_ref('refs/heads/main', new_commit, 'commit')
            second = c.collect('heads/main')
            # 已固定收据不变；新一轮另记，提交不同。
            self.assertEqual(first.commit, self.ids['commit'])
            self.assertNotEqual(second.commit, first.commit)
            self.assertEqual(second.commit, new_commit)
            # 删除分支后，直接按提交 SHA 仍可还原旧版本。
            del self.db.refs['refs/heads/main']
            receipt_by_sha = c.collect(self.ids['commit'])
            self.assertEqual(receipt_by_sha.commit, self.ids['commit'])

    def test_normalize_ref_forms(self):
        self.assertEqual(normalize_ref('heads/x'), 'refs/heads/x')
        self.assertEqual(normalize_ref('tags/x'), 'refs/tags/x')
        self.assertEqual(normalize_ref('refs/tags/x'), 'refs/tags/x')
        sha = 'a' * 40
        self.assertEqual(normalize_ref(sha), sha)
        with self.assertRaises(ValueError):
            normalize_ref('refs/notes/x')


class TreeWalkTests(PinnedTestBase):
    def test_paths_modes_and_types_are_preserved(self):
        with FakeGitServer(self.db) as server:
            receipt = self.collector(server).collect('heads/main')
        paths = {e.path: e for e in receipt.entries}
        docs = paths['manuals/device.md']
        self.assertEqual((docs.mode, docs.object_type, docs.oid),
                         (MODE_BLOB, 'blob', self.ids['manual']))
        link = paths['docs-link']
        self.assertEqual(link.mode, MODE_LINK)
        self.assertEqual(link.link_target, 'manuals/device.md')
        self.assertIsNone(link.lfs)
        exe = paths['tools/check.sh']
        self.assertEqual(exe.mode, MODE_EXEC)
        gitlink = paths['vendor']
        self.assertEqual((gitlink.mode, gitlink.object_type), (MODE_GITLINK, 'commit'))

    def test_symlink_is_link_record_only(self):
        with FakeGitServer(self.db) as server:
            receipt = self.collector(server).collect('heads/main')
        link = self.entry_by_path(receipt, 'docs-link')
        self.assertEqual(link.status, 'satisfied')
        # 链接目标只是字符串，没有在链接路径下落任何“真实资料”。
        on_disk = list((self.store.root / 'lfs').glob('**/*')) if (self.store.root / 'lfs').exists() else []
        self.assertEqual(on_disk, [])

    def test_truncated_tree_api_falls_back_to_full_object(self):
        with FakeGitServer(self.db, truncated_trees=(self.ids['root'],)) as server:
            c = self.collector(server)
            receipt = c.collect('heads/main')
        # 清单仍完整（6 个根条目对应内容全部在收据里）。
        root_paths = {e.path.split('/')[0] for e in receipt.entries}
        self.assertIn('tools', root_paths)
        self.assertTrue(any('truncated' in n for n in receipt.notes))

    def test_truncated_without_object_endpoint_is_a_hard_failure(self):
        # 对象端点只对 tree 关闭：提交仍可解析，根清单 truncated 且无法补全，
        # 必须整体失败，不能把截断后的首条列表当完整清单。
        with FakeGitServer(self.db, truncated_trees=(self.ids['root'],)) as server:
            cfg = PinConfig.build(server.base_url, ['heads/main'], workers=1)
            c = PinnedCollector(cfg, self.store, 'source-a')
            real_get_object = c.client.get_object

            def no_raw_trees(oid):
                type_name, content = real_get_object(oid)
                if type_name == 'tree':
                    raise ObjectMissing(oid)
                return type_name, content
            c.client.get_object = no_raw_trees
            with self.assertRaisesRegex(PinError, 'truncated'):
                c.collect('heads/main')

    def test_api_listing_disagreement_is_detected(self):
        # 模拟目录 API 对根 tree 谎报清单：条目集与真实对象内容不同，
        # 交叉核对必须报错，不允许让 API 的残缺/伪造列表覆盖对象内容。
        with FakeGitServer(self.db) as server:
            cfg = PinConfig.build(server.base_url, ['heads/main'], workers=1)
            lying = PinnedCollector(cfg, self.store, 'source-a')
            root_oid = self.ids['root']
            real_get_object = lying.client.get_object

            def split_personality(oid):
                type_name, content = real_get_object(oid)
                if oid == root_oid:
                    lying.client.get_tree_listing = lambda tree_oid: (
                        False, [(MODE_BLOB, 'ghost', 'f' * 40)])
                return type_name, content
            lying.client.get_object = split_personality
            with self.assertRaises((PinError, gitobj.ObjectError)):
                lying.collect('heads/main')

    def test_config_path_prefix_filter(self):
        with FakeGitServer(self.db) as server:
            cfg = PinConfig.build(server.base_url, ['heads/main'],
                                  config_path='manuals', workers=1)
            receipt = PinnedCollector(cfg, self.store, 'source-a').collect('heads/main')
        paths = [e.path for e in receipt.entries]
        self.assertTrue(all(p == 'manuals' or p.startswith('manuals/') for p in paths))
        self.assertIn('manuals/device.md', paths)
        self.assertNotIn('tools/check.sh', paths)


class HashVerificationTests(PinnedTestBase):
    def test_git_hash_uses_type_and_length_not_plain_sha1(self):
        content = b'{"device":"a"}\n'
        plain = __import__('hashlib').sha1(content).hexdigest()
        oid, stored = git_object_id('blob', content)
        self.assertNotEqual(oid, plain)
        self.assertEqual(oid, 'f520ae85995f86179e4518441dd7740855db6968')
        # 与 fixtures 中真实 Git 对象口径一致。
        tree_bytes = Path(__file__).resolve().parents[1].joinpath(
            'fixtures/raw-tree.bin').read_bytes()
        tree_id, _ = git_object_id('tree', tree_bytes)
        self.assertEqual(tree_id, 'e449fd2cb01fed8bbd7a2334198b2cd8c335e2f2')

    def test_corrupt_object_is_rejected(self):
        with FakeGitServer(self.db) as server:
            # 来源对象端点对某个 blob 返回损坏字节（哈希对不上 ID）。
            original_handle = None
            c = self.collector(server)

            def corrupt_get_object(oid):
                if oid == self.ids['manual']:
                    return 'blob', b'tampered content'
                return c.client.__class__.get_object(c.client, oid)
            c.client.get_object = corrupt_get_object
            with self.assertRaises(gitobj.ObjectError):
                c.collect('heads/main')

    def test_missing_object_is_unsatisfiable_not_guessed(self):
        with FakeGitServer(self.db, missing_objects=(self.ids['manual'],)) as server:
            c = self.collector(server)
            with self.assertRaises(ObjectMissing):
                c.collect('heads/main')

    def test_wrapped_blob_hash_mismatch_rejected(self):
        # 对象端点只对 blob 关闭（commit/tree 仍可取），封装 blob 又返回错内容。
        with FakeGitServer(self.db) as server:
            cfg = PinConfig.build(server.base_url, ['heads/main'], workers=1)
            c = PinnedCollector(cfg, self.store, 'source-a')
            real_get_object = c.client.get_object

            def hide_blobs(oid):
                type_name, content = real_get_object(oid)
                if type_name == 'blob':
                    raise ObjectMissing(oid)
                return type_name, content
            c.client.get_object = hide_blobs
            c.client.get_blob_wrapped = lambda oid: b'not the real bytes'
            with self.assertRaises(gitobj.ObjectError):
                c.collect('heads/main')


class LFSTests(PinnedTestBase):
    def test_lfs_pointer_stays_unsigned_when_not_authorized(self):
        with FakeGitServer(self.db) as server:
            receipt = self.collector(server, allow_lfs=False).collect('heads/main')
        rec = self.entry_by_path(receipt, 'manuals/device.pdf')
        self.assertEqual(rec.status, 'unsatisfied')
        self.assertIn('manuals/device.pdf', receipt.unsatisfied)
        self.assertEqual(rec.lfs['pointer_oid_sha256'], self.ids['lfs_oid'])
        self.assertEqual(rec.lfs['state'], 'pointer-only')
        # 指针仍是 blob 对象，可解析但没被当成说明书正文。
        _type, content = self.store.load(rec.oid)
        self.assertIsNotNone(parse_lfs_pointer(content))

    def test_lfs_resolved_when_authorized_and_oid_size_verified(self):
        with FakeGitServer(self.db) as server:
            cfg_kwargs = dict(allow_lfs=True, lfs_base_url=server.base_url + '/lfs')
            receipt = self.collector(server, **cfg_kwargs).collect('heads/main')
        rec = self.entry_by_path(receipt, 'manuals/device.pdf')
        self.assertEqual(rec.status, 'satisfied')
        self.assertEqual(rec.lfs['state'], 'resolved')
        self.assertEqual(rec.lfs['lfs_oid_sha256'], self.ids['lfs_oid'])
        self.assertEqual(rec.lfs['lfs_size'], self.ids['lfs_size'])
        stored = Path(self.store.root) / rec.lfs['stored_at']
        self.assertTrue(stored.is_file())
        # 真实资料与指针 blob 在对象库里是两个不同对象。
        self.assertNotEqual(rec.oid, rec.lfs['content_git_blob'])

    def test_lfs_missing_object_keeps_unsigned(self):
        with FakeGitServer(self.db, missing_lfs=(self.ids['lfs_oid'],)) as server:
            c = self.collector(server, allow_lfs=True,
                               lfs_base_url=server.base_url + '/lfs')
            receipt = c.collect('heads/main')
        rec = self.entry_by_path(receipt, 'manuals/device.pdf')
        self.assertEqual(rec.status, 'unsatisfied')
        self.assertEqual(rec.lfs['state'], 'pointer-only')
        self.assertIn('缺失', rec.detail)

    def test_lfs_size_mismatch_keeps_unsigned(self):
        # 让 LFS 端点返回长度不对的字节：oid 与 size 两项核对都应拦截。
        self.db.lfs[self.ids['lfs_oid']] = b'wrong length'
        with FakeGitServer(self.db) as server:
            c = self.collector(server, allow_lfs=True,
                               lfs_base_url=server.base_url + '/lfs')
            receipt = c.collect('heads/main')
        rec = self.entry_by_path(receipt, 'manuals/device.pdf')
        self.assertEqual(rec.status, 'unsatisfied')
        self.assertTrue('oid' in rec.detail or 'size' in rec.detail)

    def test_pointer_text_is_never_served_as_manual(self):
        with FakeGitServer(self.db) as server:
            receipt = self.collector(server).collect('heads/main')
        rec = self.entry_by_path(receipt, 'manuals/device.pdf')
        # 未满足时没有 lfs 落地文件，只有指针 blob 本身。
        self.assertFalse((self.store.root / 'lfs').exists()
                         and any((self.store.root / 'lfs').rglob('*')))
        self.assertEqual(rec.object_type, 'blob')


class SubmoduleTests(PinnedTestBase):
    def submodule_config(self, server, allowed=True):
        mapping = {
            'https://vendor.example/test.git': {
                'base_url': server.base_url,
                'allowed_refs': [self.ids['sub_commit']] if allowed else ['deadbeef' * 5],
            }
        }
        return dict(submodule_sources=mapping)

    def test_unmapped_submodule_is_unsigned_and_not_fetched(self):
        with FakeGitServer(self.db) as server:
            receipt = self.collector(server).collect('heads/main')
        rec = self.entry_by_path(receipt, 'vendor')
        self.assertEqual(rec.status, 'unsatisfied')
        self.assertIn('映射', rec.detail)
        # 子模块里的对象没有被领取。
        self.assertFalse(self.store.has(self.ids['sub_tree']))
        self.assertFalse(self.store.has(self.ids['sub_commit']))

    def test_mapped_submodule_follows_pinned_commit_only(self):
        with FakeGitServer(self.db) as server:
            c = self.collector(server, **self.submodule_config(server))
            receipt = c.collect('heads/main')
        rec = self.entry_by_path(receipt, 'vendor')
        self.assertEqual(rec.status, 'satisfied')
        self.assertEqual(rec.submodule_receipt,
                         'https://vendor.example/test.git@%s' % self.ids['sub_commit'])
        # 子模块固定提交与其中对象都已落库并通过哈希核对。
        self.assertTrue(self.store.has(self.ids['sub_commit']))
        self.assertTrue(self.store.has(self.ids['sub_tree']))
        nested = self.entry_by_path(receipt, 'vendor/nested.json')
        self.assertEqual(nested.oid, self.ids['sub_blob'])
        self.assertTrue(nested.path.startswith('vendor/'))

    def test_submodule_commit_not_allowed_is_unsigned(self):
        with FakeGitServer(self.db) as server:
            c = self.collector(server, **self.submodule_config(server, allowed=False))
            receipt = c.collect('heads/main')
        rec = self.entry_by_path(receipt, 'vendor')
        self.assertEqual(rec.status, 'unsatisfied')
        self.assertIn('允许列表', rec.detail)
        self.assertFalse(self.store.has(self.ids['sub_commit']))


class ResumeAndReceiptTests(PinnedTestBase):
    def test_resume_reuses_objects_and_reproduces_receipt(self):
        with FakeGitServer(self.db) as server:
            cfg = PinConfig.build(server.base_url, ['heads/main'], workers=1)
            first, path1 = collect_pinned(cfg, self.store, 'source-a', 'heads/main')
            request_count_after_first = len(server.requests)
            # 新 collector、同一对象库：已落盘对象不应重复下载。
            second, path2 = collect_pinned(cfg, ObjectStore(self.store.root),
                                           'source-a', 'heads/main')
        self.assertLess(len(server.requests), request_count_after_first * 2)
        self.assertEqual([e.to_dict() for e in first.entries],
                         [e.to_dict() for e in second.entries])
        self.assertEqual(first.commit, second.commit)
        data = json.loads(Path(path1).read_text(encoding='utf-8'))
        self.assertEqual(data['commit'], self.ids['commit'])
        self.assertEqual(data['source_id'], 'source-a')
        self.assertIn('entries', data)

    def test_receipt_round_trip_is_json_verifiable(self):
        with FakeGitServer(self.db) as server:
            cfg = PinConfig.build(server.base_url, ['heads/main'], workers=2)
            receipt, path = collect_pinned(cfg, self.store, 'source-b', 'heads/main')
        raw = json.loads(Path(path).read_text(encoding='utf-8'))
        self.assertEqual(raw['resolved_ref'], 'refs/heads/main')
        for row in raw['entries']:
            if row.get('oid'):
                self.assertTrue(gitobj.is_sha1(row['oid']))

    def test_each_object_on_disk_rehashes_to_its_id(self):
        with FakeGitServer(self.db) as server:
            self.collector(server).collect('heads/main')
        for oid, (typ, content) in self.db.objects.items():
            if self.store.has(oid):
                stored = self.store.path_for(oid).read_bytes()
                parsed_type, parsed_content = gitobj.parse_object(
                    stored, expected_id=oid, expected_type=typ)
                self.assertEqual(parsed_content, content)


class HardeningTests(PinnedTestBase):
    def test_config_path_rejects_traversal_segments(self):
        for bad in ('../etc', 'a/./b', 'a//b', '/abs', 'a/../b'):
            with self.subTest(bad=bad):
                with FakeGitServer(self.db) as server:
                    with self.assertRaises(ValueError):
                        PinConfig.build(server.base_url, ['heads/main'],
                                        config_path=bad)

    def test_receipt_filename_cannot_escape_store(self):
        from app.pinned import collect_pinned, _safe_source_id
        nasty = '../../etc/passwd'
        self.assertEqual(_safe_source_id(nasty), '.._.._etc_passwd')
        with FakeGitServer(self.db) as server:
            cfg = PinConfig.build(server.base_url, ['heads/main'], workers=1)
            _receipt, path = collect_pinned(cfg, self.store, nasty, 'heads/main')
        self.assertTrue(str(path).startswith(str(self.store.receipts_dir)))
        self.assertTrue(path.is_file())

    def test_lfs_local_tamper_is_detected(self):
        # 先正常领取一次 LFS，再篡改本地副本，重新采集必须报未满足。
        with FakeGitServer(self.db) as server:
            c = self.collector(server, allow_lfs=True,
                               lfs_base_url=server.base_url + '/lfs')
            first = c.collect('heads/main')
            rec = self.entry_by_path(first, 'manuals/device.pdf')
            target = self.store.root / rec.lfs['stored_at']
            target.write_bytes(b'tampered')
            second = c.collect('heads/main')
        rec2 = self.entry_by_path(second, 'manuals/device.pdf')
        self.assertEqual(rec2.status, 'unsatisfied')
        self.assertIn('损坏', rec2.detail)

    def test_unknown_mode_in_api_listing_rejected(self):
        with FakeGitServer(self.db) as server:
            cfg = PinConfig.build(server.base_url, ['heads/main'], workers=1)
            c = PinnedCollector(cfg, self.store, 'source-a')

            def bad_listing(oid):
                return False, [('123456', 'weird', 'e' * 40)]
            c.client.get_tree_listing = bad_listing
            with self.assertRaises(gitobj.ObjectError):
                c.collect('heads/main')

    def test_parallel_and_sequential_collect_match(self):
        with FakeGitServer(self.db) as server:
            seq_cfg = PinConfig.build(server.base_url, ['heads/main'], workers=1)
            par_cfg = PinConfig.build(server.base_url, ['heads/main'], workers=4)
            seq = PinnedCollector(seq_cfg, ObjectStore(self.tmp / 's1'),
                                  'source-a').collect('heads/main')
            par = PinnedCollector(par_cfg, ObjectStore(self.tmp / 's2'),
                                  'source-a').collect('heads/main')
        self.assertEqual([e.to_dict() for e in seq.entries],
                         [e.to_dict() for e in par.entries])


if __name__ == '__main__':
    unittest.main()
