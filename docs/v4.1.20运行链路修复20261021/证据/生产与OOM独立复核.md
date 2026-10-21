# OOM与发布证据独立复核

候选：920ce618a9b1b2b52ba65c91cefdf285a5ae0605。
只读本地已提供证据及候选源码；未连接生产、执行SQL、复跑测试或修改项目。

## 发布核对

版本4.1.20、完整SHA、账本、后端环境、镜像ID、公开health/ready、Alembic064一致。采集记录deployment_exit=0，日志摘录19:42:47Z发布完成。前端资源路径为采集器按完整SHA检索命中的结果，不代表登录业务验收。
备份记录1,001,131,984字节，SHA256=31884cfd197e56dc61ae6604d967b915437bd38339e91c19215d1166920f72cc；校验记录true，元数据109表/064，19:38:05Z日志记录在cr_testdb内恢复验证通过。未下载备份或重新计算备份本体哈希，不证明逐行业务等价。
最终运行值、持久值均为temptable_max_ram=67108864与temptable_max_mmap=0，路径位于/mysql_dynamic_variables。初稿运行值已正确、持久提取为空，stderr为AssertionError；最终递归提取正确且stderr为空。旧稿不包含原始持久JSON或旧提取代码，不能仅凭旧稿重建具体错误键名。
后端/前端healthy且restart0，mysql healthy且restart1，内存上限1342177280。默认维护锁路径不存在、prism_verify_%恢复库列表为空；锁检查只覆盖采集器写死的默认路径，未证明自定义覆盖路径。
定时备份最近服务Result=success、ExecMainStatus=0，UTC18:28:49至18:32:32；不同于19:31:08发布前备份。

## 资源快照

2026-10-22T03:44:21+08:00：根盘84%，可用32,272,199,680字节；宿主MemAvailable=3,926,077,440字节；swap已用3,210,825,728/空闲11,440,128字节，使用率99.644966%。mysql容器651.9MiB/1.25GiB；clamav容器948.7MiB/2.148GiB，不是单独clamd进程RSS。ops-check can_continue=true、blocking_checks为空；不能据此描述资源宽裕。

## OOM证据和源码风险

内核摘录两行均对应2026-10-21T19:30:59Z、PID1993489、与最终mysql一致的完整cgroup ID，明确CONSTRAINT_MEMCG并杀死mysqld。截至19:49:20Z采集的筛选记录只见这一次；OOMKilled=false不推翻该内核事件。未取得当时SQL、客户端身份、执行计划或进程内存分解。
prism_ops_executor.py:900-940对mysql.general_log读取完整argument并排序，默认4000行、执行器上限5000行；160字SQL文本截断及100000字stdout截尾都在取回之后。未显式--quick，存在默认客户端缓冲风险；LIMIT不是扫描、排序或字节预算。
同文件113-135与908-915的90秒subprocess超时仅显式管理宿主docker CLI；无容器内timeout或服务端执行预算，不能保证容器内mysql及SQL随之结束。未观察到实际悬挂客户端。
security_monitor_service.py:778-785/918-919调用可达，源码默认开关true、1小时/4000条；未证明OOM时生产实际调用。指定范围未发现JSON_ARRAYAGG/JSON_OBJECTAGG。
ops-check只直接查询迁移版本；db_health只读docker状态、资源及日志。common.sh的JSON检查是EXISTS配JSON_VALID/JSON_TYPE/JSON_CONTAINS_PATH，调用者为rollback.sh，不属于正常deploy预检。备份源码采用独立512m客户端容器与mysqldump --quick。

## 护栏范围

64MiB/0符合MySQL8.0官方变量与TempTable机制，是缓解方向，不证明具体查询触发TempTable或唯一OOM根因。64MiB不是mysqld/容器总内存上限，线程本地块、其他排序/连接及客户端内存仍需区分。溢出改走InnoDB磁盘临时表，可能增加IO、磁盘占用与延迟；观察窗口不足，不能声称稳定性已长期验证。
后续建议：SQL层文本限长、显式流式读取、服务端时间预算与容器内超时收尾；本轮未实施。

10份相关源码均与候选blob一致，输入文件哈希及本地一致性断言见review.json。
