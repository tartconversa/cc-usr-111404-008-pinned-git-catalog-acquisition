# 来源和当前接口

## 原始响应

fixtures/sample.json含source_id、url、status、headers、body_base64和可选metadata，headers为有序二元列表，保留重复字段；body_base64解码成真实原始实体字节，原件也存在fixtures/source-body.bin。
app.source.from_packet校验状态、头与字节编码，生成只读Response。header(name)不区分字段名大小写并按顺序逗号连接重复值，headers原元组始终保留。
sha256只证明当前实体字节，不证明来源业务身份或采集完整。fetch执行一次HTTP请求，保留错误响应，不自动跳转，不替来源解释缓存、分页或业务错误。
fetch(url,source_id,method,headers,body,max_bytes)保留方法和请求体，读取超过预算即失败；适配器现有超时为5秒。app.service.collect_one调用该来源接口，inspect(packet)供已有响应读取，二者输出source_id、response_sha256和summary。
实际访问资格由上层来源配置控制，本地来源服务只绑定127.0.0.1；其/source路径返回指定响应，其他路径404。服务器记录请求方法、路径、头与body_base64；现有记录只用于合成来源联调，不包含真实凭据。

## 业务来源口径

来源API返回Git SHA-1对象格式，hash=sha1(type+空格+十进制字节长+NUL+原内容)。允许ref列表由配置给出，tag对象需逐层解析并检测循环，最终必须commit。tree模式支持100644/100755普通文件、120000链接、040000子树、160000子模块；递归API的truncated=true不能作完整清单。来源响应JSON仅为对象传输封装，保留raw对象内容或可重建的规范二进制tree，摘要验证基于真实Git对象。LFS v1指针的oid为sha256，与Git SHA-1不同。

## 当前能力与配置

当前app.baseline.analyze只产生一份响应的概览，兼容入口继续保留；新采集可增加入口，不能把字节摘要等同于协议或业务完整性。
metadata={}。读取预算、授权范围和来源凭据属于调用配置，真实密钥不放在资料目录或交付证据中。

## 固定提交领取（app.gitobj / app.pinned / app.pin）

为解决“采集中分支更新、索引与说明书版本错位”的问题，固定领取只认允许引用解析出的唯一提交，路径、模式、类型与内容逐项可核验。

引用解析：allowed_refs由配置给出，接受refs/heads/x、refs/tags/y、简写heads/x、tags/x或40位提交SHA，请求引用不在允许列表即拒绝。分支头是commit；标签头可能是tag对象，且tag可指向另一个tag，必须逐层解引用、检测环路并限制深度，最终类型必须是commit——标签对象自身的SHA绝不能当提交。直接固定的提交SHA不依赖分支当前状态，强推或删除分支不改变已固定版本，新一轮采集另写收据。

tree与truncated：先读目录API的tree清单并规范重建核对SHA-1；truncated=true表示清单不完整，必须回对象端点取完整tree，对象端点也缺失时整体失败，禁止把截断列表当完整清单。对象端点可用时与API清单双来源交叉核对，任何条目增删、模式或类型不一致都判失败。

对象哈希：Git对象ID为sha1(b"<type> <十进制长度>\0<原内容>")，按blob/tree/commit/tag分别计算，与对下载字节直接sha1不同；每个对象落盘前后都重算核对，封装JSON只作传输，摘要一律基于真实Git对象。tree模式仅接受100644、100755、120000、040000、160000，模式与声明类型必须一致。

条目语义：120000符号链接只登记链接目标字符串，不展开、不在链接路径落真实内容；160000子模块（gitlink）记录被固定的子模块提交，仅当该仓库URL出现在显式submodule_sources映射且该提交在其允许列表内才继续领取，全程只做对象读取，不执行任何仓库代码或钩子；子模块条目路径加gitlink前缀避免与主仓库冲突。

LFS：v1指针（version头、oid sha256、十进制size）与真实资料严格分开。allow_lfs=false、未配置LFS来源、404缺对象、oid或size核对不符、本地副本被篡改，一律保持unsatisfied，指针blob照常入库但绝不充当说明书正文；兑现时真实资料另存store/lfs并记录其Git blob哈希。

恢复与并行：对象按objects/<前2位>/<后38位>去重存储，原子替换，已存在且哈希复核通过的对象不重复下载，来源暂时不可用也能还原已固定版本；线程池并行下载，收据在采集结束时按路径统一排序，并行与串行结果一致。收据写receipts/<commit>-<来源>.json，来源身份做文件名净化，凭据不入库。

CLI：python -m app.pin <配置JSON> <引用> <对象库目录> [来源身份]，全部满足退出0，存在unsatisfied退出2，配置或协议错误退出1。
