# Milvus 向量库评估与设计

> 结论先行:**现在不把 Milvus 接入生产运行时**;交付"随时可切"的设计与开箱即用的本地/演示 compose profile(`deploy/docker-compose.milvus.yml`)。
> 评估日期:2026-08-29 · 代码基线:生产 `eccab68`

## 1. 现状基线(为什么现在不需要)

| 事实 | 出处 |
| --- | --- |
| 嵌入:OpenAI 兼容 `/embeddings`,无 Key 降级本地哈希词袋向量(256 维,L2 归一化) | `backend/app/services/embedding_service.py:98-118`、`core/config.py` `embedding_dim=256` |
| 存储:向量以 JSON 文本存 `knowledge_chunk` 与 `agent_knowledge_chunk`(LONGTEXT) | `models/knowledge_chunk.py:28-31`、`models/agent_governance.py` |
| 检索:**全量拉取 + Python 余弦**,无 ANN 索引 | `knowledge_service.retrieve`、`unified_retrieve` |
| **仓库内已有显式反向量库决策**:"避免引入重型向量库(生产服务器内存仅 ~1.9G)" | `models/knowledge_chunk.py:1-7` docstring |
| 生产 compose 内存护栏:mysql 900m / clamav 1400m / redis 128m / backend 768m | `deploy/docker-compose.yml:12,59,79,101` |
| Milvus standalone 最小形态 = etcd + MinIO + milvus 三容器,常态内存 ≥2G | Milvus 官方部署要求 |

判断依据:当前个人 KB + Agent KB 切片为千级,O(N×D) 全扫描在毫秒级,无性能痛;而生产 2C2G 内存已被既有服务占满,Milvus 三件套会直接挤爆(历史上 MySQL OOM 已发生过)。**引入的运维成本(digest 对齐、版本升级、备份)远大于收益**。

## 2. 什么时候该切

满足任一条件再启用:

1. 切片总量 > **10 万**(全扫描进入百毫秒级,开始拖慢小菱 recall_knowledge);
2. 多租户高并发检索(QPS 型瓶颈);
3. 需要标量过滤 + 向量混合检索(如"某项目的渗透历史发现"语义检索)。

## 3. 目标架构(切换时按此落地)

### Collection / Partition / Segment / 索引设计

| 层 | 设计 | 理由 |
| --- | --- | --- |
| Collection | `kb_personal`(个人KB)、`kb_manual`(手册/Playbook)、`pentest_knowledge`(渗透知识库,新) | 按域隔离,与现有 `unified_retrieve` 的"手册/个人各取 top_k"语义对齐;检索半径天然收窄 |
| Partition | `kb_personal` 以 `user_id` 为 partition key;`kb_manual`/`pentest_knowledge` 按 `source` 分区 | 延续"user_id 隔离连管理员也不放行"红线,Milvus partition key 过滤在存储层完成,不会漏过滤 |
| Segment | 无需干预:Milvus 自动管理 growing→sealed→flushed 段;配合 `consistency_level="Bounded"`(写后秒级可见即可,不必 Strong) | 自建 segment 管理无收益 |
| 索引 | `HNSW`(M=16, efConstruction=200, metric=`IP`) | 向量已 L2 归一化,IP≈cosine;HNSW 召回/延迟均衡,数据量百万内最稳 |
| 标量字段 | `user_id`(partition key)、`agent_code`、`doc_id`、`embed_model`、`created_at` | `embed_model` 必须存:维度/模型切换时按标记批量重建,避免现状"旧向量静默失配"问题 |

### 分布式角色(规模化路径,单机不需要)

```
接入层: proxy(无状态多副本) →
协调层: root coord(元数据/时间戳) · data coord(段管理) · query coord(负载均衡) · index coord(索引构建调度) →
工作层: query node(检索) · data node(写 ingestion) · index node(异步建索引)
基础设施: etcd(元数据) · Pulsar/Kafka(消息队列, WAL) · MinIO/S3(段对象存储)
```

单机 standalone 把上述角色全部折叠进一个 milvus 进程(仍需 etcd+MinIO);数据量/并发到瓶颈时逐层拆出 worker 节点即可水平扩,**collection schema 不用改**。这也是"先 standalone 后分布式"平滑路径的依据。

### 切换点清单(动这几个函数,其余不动)

1. 写入:`knowledge_service` 入库与 `agent_knowledge_service` 入库 → 追加 `upsert(collection, partition, id, vector, scalar)`;
2. 读取:`knowledge_service.retrieve` 与 `unified_retrieve` 的全量扫描段 → 换 `search(collection, expr=user_id==N, top_k)`;
3. 删除:文档删除 → `delete(expr=doc_id==X)`(保持按 user_id 先过滤的语义);
4. 降级:Milvus 不可用时回落现有 JSON 余弦路径(保留为 fallback 实现),检索失败显式标注 degraded,不伪装成功——对齐平台"引擎不可用绝不伪装 clean"的既有原则;
5. 迁移:一次性回填脚本按 `embed_model` 分批重嵌入,写双读验证一周后切单写。

## 4. 本次交付物

- 本评估文档(决策记录,推翻/确认须更新本页);
- `deploy/docker-compose.milvus.yml`:`--profile milvus` 启动的 standalone(etcd+minio+milvus+attu 管理台),资源上限已按本机演示调低,**不接入生产 compose**;
- 不引入 `pymilvus` 依赖、不改任何检索代码(避免在无收益期增加生产构建/运行风险)。

## 5. 决策复核条件

下次满足第 2 节任一条件,或生产服务器扩内存(≥4G 富余)时,重开评估并按第 3 节实施。
