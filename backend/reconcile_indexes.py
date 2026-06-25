"""幂等补齐数据库二级索引。

背景：生产 MySQL 上有些表是用 `Base.metadata.create_all` 建的，而历史上部分
模型未声明二级索引，导致这些表只有主键、缺失 init.sql 里定义的索引（全表扫描）。
本脚本对照 ORM 模型现在声明的全部索引，给实际库补上缺失的那些。

幂等性：按「列组合」判断索引是否已存在（忽略索引名差异），因此
- 不会重复创建（无论历史是 init.sql 的 idx_* 还是 create_all 的 ix_* 命名）；
- 已有索引/唯一约束/主键覆盖了相同列组合时自动跳过。

用法（容器内，使用应用自身的数据库连接）：
    docker exec cr_backend python reconcile_indexes.py
本地：
    DB_HOST=sqlite DB_NAME=foo python reconcile_indexes.py
"""
import init_sqlite  # noqa: F401  导入以触发全部 ORM 模型注册到 Base.metadata
from sqlalchemy import inspect, text

from app.core.database import Base, engine


def _existing_colsets_and_names(insp, table_name):
    """收集某表已存在的『列组合』集合与『索引名』集合（含唯一约束、主键）。"""
    colsets = set()
    names = set()
    for ix in insp.get_indexes(table_name):
        cols = tuple(ix.get("column_names") or [])
        if cols:
            colsets.add(cols)
        if ix.get("name"):
            names.add(ix["name"])
    for uc in insp.get_unique_constraints(table_name):
        cols = tuple(uc.get("column_names") or [])
        if cols:
            colsets.add(cols)
        if uc.get("name"):
            names.add(uc["name"])
    pk = insp.get_pk_constraint(table_name).get("constrained_columns") or []
    if pk:
        colsets.add(tuple(pk))
    return colsets, names


def reconcile() -> int:
    insp = inspect(engine)
    actual_tables = set(insp.get_table_names())
    created = skipped = 0
    for table_name, table in Base.metadata.tables.items():
        if table_name not in actual_tables:
            print(f"  · 跳过(表不存在): {table_name}")
            continue
        colsets, names = _existing_colsets_and_names(insp, table_name)
        for index in table.indexes:
            cols = tuple(c.name for c in index.columns)
            if cols in colsets or index.name in names:
                skipped += 1
                continue
            colspec = ", ".join(f"`{c}`" for c in cols)
            uniq = "UNIQUE " if index.unique else ""
            sql = f"CREATE {uniq}INDEX `{index.name}` ON `{table_name}` ({colspec})"
            try:
                with engine.begin() as conn:
                    conn.execute(text(sql))
                print(f"  + 已创建 {table_name}.{index.name} ({', '.join(cols)})")
                colsets.add(cols)
                names.add(index.name)
                created += 1
            except Exception as exc:  # noqa: BLE001 单条失败不影响其它
                print(f"  ! 失败 {table_name}.{index.name}: {exc}")
    print(f"\n完成：新建 {created} 个，跳过(已存在) {skipped} 个。")
    return created


if __name__ == "__main__":
    reconcile()
