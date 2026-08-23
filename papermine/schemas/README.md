# Schema 规范

- 每个结构对象一个 JSON Schema 文件：`papermine/schemas/<name>.schema.json`
- 文件内 `version` 与 `$id` 同步，改动必须 bump 并在 `papermine/storage.py` 的 `SCHEMA_MIGRATIONS` 注册迁移函数。
- 运行时数据文件内嵌 `_schema` 与 `_schema_version`，读取时沿迁移链自动升级。
